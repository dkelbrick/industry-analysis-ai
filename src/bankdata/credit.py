"""Asset mix by product and credit-quality ratios.

Outputs (``data/tidy``, Parquet and CSV):

* ``ba900_products``: bank x month x product, from BA900 column 5 (total = domestic + foreign).
  The products partition item 277 (total assets) exactly, with credit impairments (item 194)
  as a negative line, so every month adds back to total assets.
* ``industry_npl``: industry impaired advances (non-performing loans) and specific impairments
  from BA200. BA200 is published for the all-banks total only.
* ``credit_quality``: bank x financial year. The credit loss ratio (CLR) is the income-statement
  credit impairment charge divided by average gross customer loans from BA900. The BA900
  impairment allowance ratio (item 194 / gross customer loans) is a bank-level proxy for
  credit quality. Bank-level NPL ratios are not in BA900, and BA200 is industry-only.
"""
from __future__ import annotations

import sys

import polars as pl

from .config import FYE_MONTH, MIN_FISCAL_YEAR, TIDY

# BA900 asset items grouped into products. Each item counts once, and the groups add to item 277:
# 277 = 103 + 110 + 195 + 258 + 267, and
# 110 = 111 + 117 + 118 + 126 + 135 + 139 + 150 + 166 + 171 + 180 - 194, with 126 = 127..130, 133, 134.
PRODUCTS: dict[str, dict] = {
    "Residential mortgages": {"items": [155], "loan": True},
    "Commercial and farm mortgages": {"items": [151, 159], "loan": True},
    "Instalment sales and leases": {"items": [139], "loan": True},
    "Credit cards": {"items": [166], "loan": True},
    "Overdrafts": {"items": [181], "loan": True},
    "Term and other loans": {"items": [187, 188], "loan": True},  # factoring and other loans and advances
    "Public-sector loans": {"items": [171], "loan": True},
    "Foreign-currency loans": {"items": [130, 134], "loan": True},  # to SA corporates and the foreign non-bank sector
    "Interbank and repo": {"items": [111, 117, 118, 129, 133], "loan": False},
    "Cash and central bank": {"items": [103, 127, 128], "loan": False},
    "Investments and securities": {"items": [195, 135], "loan": False},  # including redeemable preference shares
    "Other and non-financial assets": {"items": [258, 267], "loan": False},
    "Credit impairments": {"items": [194], "loan": False, "sign": -1},
}
PRODUCT_ORDER = list(PRODUCTS)
TOTAL_COLUMN = 5  # BA900 column 5: total assets (domestic plus foreign)

# Income-statement line holding the credit impairment charge, by the labels the banks have used
IMPAIRMENT_LINE = (
    r"(?i)^(credit impairment charges?|credit impairments|impairment losses( on loans and advances)?"
    r"|impairments charge on (loans and advances|financial instruments)"
    r"|net impairment charge on loans and advances( to clients)?|net provision for doubtful debts charge)$"
)
BASIS_PREFERENCE = {"separate": 0, "unspecified": 1, "consolidated": 2}  # bank solo first, comparable to BA900

# BA200 items (stable numbering since 2008)
BA200_ITEMS = {
    27: "impaired_advances",
    28: "specific_impairments",
    29: "specific_coverage_pct",  # specific impairments / impaired advances
    30: "specific_to_loans_pct",
    31: "npl_ratio_pct",  # impaired advances / total loans on balance sheet
    32: "npl_to_credit_exposure_pct",
    12: "gross_credit_exposure",
}


def _fye(bank: str) -> int:
    return FYE_MONTH.get(bank, 12)


def products(ba900: pl.DataFrame) -> pl.DataFrame:
    rows = [{"item_number": i, "product": p, "is_customer_loan": spec["loan"], "sign": spec.get("sign", 1)}
            for p, spec in PRODUCTS.items() for i in spec["items"]]
    m = pl.DataFrame(rows)
    df = (
        ba900.filter(pl.col("column_number") == TOTAL_COLUMN)
        .join(m, on="item_number")
        .group_by("bank", "month_end", "fiscal_year", "product", "is_customer_loan")
        .agg((pl.col("value").fill_null(0) * pl.col("sign")).sum().alias("value"))
        .with_columns(
            (pl.col("value") / 1000).alias("value_rm"),
            pl.col("product").replace_strict({p: i for i, p in enumerate(PRODUCT_ORDER)}).alias("product_order"),
            (pl.col("month_end").dt.month() == pl.col("bank").replace_strict(FYE_MONTH, default=12)).alias("is_fy_end"),
        )
    )
    total = ba900.filter((pl.col("column_number") == TOTAL_COLUMN) & (pl.col("item_number") == 277)).select(
        "bank", "month_end", (pl.col("value") / 1000).alias("total_assets_rm"))
    df = df.join(total, on=["bank", "month_end"], how="left").with_columns(
        (pl.col("value_rm") / pl.col("total_assets_rm")).alias("share_of_assets"))
    loans = df.filter(pl.col("is_customer_loan")).group_by("bank", "month_end").agg(
        pl.col("value_rm").sum().alias("gross_customer_loans_rm"))
    df = df.join(loans, on=["bank", "month_end"], how="left").with_columns(
        pl.when(pl.col("is_customer_loan")).then(pl.col("value_rm") / pl.col("gross_customer_loans_rm"))
        .alias("share_of_customer_loans"))
    return df.sort("bank", "month_end", "product_order")


def check_products(prod: pl.DataFrame) -> pl.DataFrame:
    """Products must add back to BA900 total assets (item 277) every month."""
    return prod.group_by("bank", "month_end").agg(
        pl.col("value_rm").sum().alias("sum_rm"), pl.col("total_assets_rm").first()).with_columns(
        (pl.col("sum_rm") - pl.col("total_assets_rm")).alias("difference_rm"))


def industry_npl(ba200: pl.DataFrame) -> pl.DataFrame:
    df = ba200.filter(pl.col("item_number").is_in(list(BA200_ITEMS)) & (pl.col("column_number") == 1)).with_columns(
        pl.col("item_number").replace_strict(BA200_ITEMS).alias("metric"))
    df = df.with_columns(
        pl.when(pl.col("unit") == "%").then(pl.col("value")).otherwise(pl.col("value") / 1000).alias("value"),
        pl.when(pl.col("unit") == "%").then(pl.lit("%")).otherwise(pl.lit("Rm")).alias("unit"),
    )
    return df.select("bank", "month_end", "fiscal_year", "item_number", "item_description", "metric", "value",
                     "unit").sort("month_end", "item_number")


def impairment_charges(fs: pl.DataFrame) -> pl.DataFrame:
    x = fs.filter((pl.col("statement") == "income") & pl.col("line_label").str.strip_chars().str.contains(IMPAIRMENT_LINE))
    x = x.with_columns(pl.col("basis").replace_strict(BASIS_PREFERENCE, default=9).alias("_pref"))
    x = x.sort("bank", "fiscal_year", "_pref", "line_order").group_by("bank", "fiscal_year", maintain_order=True).first()
    return x.select(
        "bank", "fiscal_year",
        pl.col("value_rm").abs().alias("impairment_charge_rm"),  # banks present the charge as a negative
        pl.col("line_label").alias("impairment_line"),
        pl.col("basis").alias("impairment_basis"),
        pl.col("report_year").alias("impairment_report_year"),
        pl.col("page").alias("impairment_page"),
    )


def credit_quality(prod: pl.DataFrame, fs: pl.DataFrame, npl: pl.DataFrame) -> pl.DataFrame:
    monthly = prod.group_by("bank", "month_end", "fiscal_year").agg(
        pl.col("gross_customer_loans_rm").first(),
        (-pl.col("value_rm").filter(pl.col("product") == "Credit impairments").sum()).alias("credit_impairments_rm"),
    )
    # Average gross loans over the year: the 12 month-ends plus the prior year-end (13 points where available)
    prior = monthly.filter(pl.col("month_end").dt.month() == pl.col("bank").replace_strict(FYE_MONTH, default=12)).with_columns(
        (pl.col("fiscal_year") + 1).alias("fiscal_year"))
    avg = pl.concat([monthly, prior]).group_by("bank", "fiscal_year").agg(
        pl.col("gross_customer_loans_rm").mean().alias("avg_gross_customer_loans_rm"),
        pl.len().alias("months_in_average"))
    fy_end = monthly.filter(pl.col("month_end").dt.month() == pl.col("bank").replace_strict(FYE_MONTH, default=12)).select(
        "bank", "fiscal_year", pl.col("month_end").alias("fy_end_month"),
        pl.col("gross_customer_loans_rm").alias("fy_end_gross_customer_loans_rm"),
        pl.col("credit_impairments_rm").alias("fy_end_credit_impairments_rm"))
    out = fy_end.join(avg, on=["bank", "fiscal_year"], how="left").join(
        impairment_charges(fs), on=["bank", "fiscal_year"], how="left")
    out = out.with_columns(
        (100 * pl.col("impairment_charge_rm") / pl.col("avg_gross_customer_loans_rm")).alias("clr_pct"),
        (100 * pl.col("fy_end_credit_impairments_rm") / pl.col("fy_end_gross_customer_loans_rm")).alias("allowance_ratio_pct"),
    )
    ind = npl.filter((pl.col("metric") == "npl_ratio_pct") & (pl.col("month_end").dt.month() == 12)).select(
        "fiscal_year", pl.col("value").alias("industry_npl_ratio_pct"))
    out = out.join(ind, on="fiscal_year", how="left")
    return out.filter(pl.col("fiscal_year") >= MIN_FISCAL_YEAR).sort("bank", "fiscal_year")


def _write(df: pl.DataFrame, name: str) -> None:
    df.write_parquet(TIDY / f"{name}.parquet")
    df.write_csv(TIDY / f"{name}.csv")
    print(name, df.shape)


def main() -> dict[str, pl.DataFrame]:
    ba900 = pl.read_parquet(TIDY / "ba900.parquet").filter(pl.col("fiscal_year") >= MIN_FISCAL_YEAR)
    prod = products(ba900)
    chk = check_products(prod)
    bad = chk.filter(pl.col("difference_rm").abs() > 0.05)
    print(f"product check: {chk.height - bad.height}/{chk.height} bank-months add back to total assets")
    npl = industry_npl(pl.read_parquet(TIDY / "industry_ba200.parquet"))
    cq = credit_quality(prod, pl.read_parquet(TIDY / "financial_statements.parquet"), npl)
    _write(prod, "ba900_products")
    _write(npl, "industry_npl")
    _write(cq, "credit_quality")
    return {"ba900_products": prod, "industry_npl": npl, "credit_quality": cq}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    out = main()
    with pl.Config(tbl_rows=80, tbl_width_chars=220, fmt_str_lengths=50):
        print(out["credit_quality"].select("bank", "fiscal_year", "impairment_charge_rm", "impairment_basis",
                                           "avg_gross_customer_loans_rm", "clr_pct", "allowance_ratio_pct",
                                           "industry_npl_ratio_pct"))
