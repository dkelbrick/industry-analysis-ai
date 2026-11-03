"""BA900 helpers: formula parsing, internal consistency checks, headline series."""
from __future__ import annotations

import re

import polars as pl

from .config import TIDY

_FORMULA = re.compile(r"\((?:total\s+)?(?:of\s+)?items?\s+(?P<body>[^)]*)\)", re.I)


def parse_formula(desc: str | None) -> list[tuple[int, int]] | None:
    """Return [(item, sign)] from e.g. '(total of items 111, 117 to 120, less item 194)'."""
    if not desc:
        return None
    m = _FORMULA.search(desc)
    if not m or not re.search(r"\d", m.group("body")):
        return None
    body = m.group("body")
    plus, _, minus = _split_less(body)
    out = [(i, 1) for i in _expand(plus)] + [(i, -1) for i in _expand(minus)]
    return out or None


def _split_less(body: str) -> tuple[str, str, str]:
    parts = re.split(r"\bless\b", body, maxsplit=1, flags=re.I)
    return (parts[0], "", parts[1] if len(parts) > 1 else "")


def _expand(s: str) -> list[int]:
    s = re.sub(r"(?i)\b(and|of|items?|total)\b", ",", s)
    out: list[int] = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if m := re.fullmatch(r"(\d+)\s+to\s+(\d+)", tok):
            out += list(range(int(m[1]), int(m[2]) + 1))
        else:
            out += [int(x) for x in re.findall(r"\d+", tok)]
    return out


def load() -> pl.DataFrame:
    return pl.read_parquet(TIDY / "ba900.parquet")


def formula_table(df: pl.DataFrame) -> pl.DataFrame:
    """One row per (period, parent item, child item, sign) as stated in that period's form."""
    d = df.select("month_end", "item_number", "item_description").unique()
    rows = []
    for me, item, desc in d.iter_rows():
        if f := parse_formula(desc):
            rows += [{"month_end": me, "parent_item": item, "child_item": c, "sign": s} for c, s in f]
    return pl.DataFrame(rows)


def total_columns(df: pl.DataFrame) -> pl.DataFrame:
    """The 'TOTAL' column of each BA900 table (first column whose header starts with 'total')."""
    return (
        df.select("table_number", "column_number", "column_description").unique()
        .filter(pl.col("column_description").str.strip_chars().str.contains(r"(?i)^total"))
        .group_by("table_number").agg(pl.col("column_number").min().alias("total_column"))
    )


def internal_checks(df: pl.DataFrame, abs_tol: float = 5.0, rel_tol: float = 1e-6) -> pl.DataFrame:
    """Compare each stated subtotal with the sum of its components (R'000), plus A = E + L.

    Same-table formulas are checked column by column. Formulas whose components sit in a
    table with a different column layout are checked on the table TOTAL columns only.
    Tolerance allows rounding of R'000 figures: |diff| <= abs_tol or |diff| <= rel_tol * |reported|.
    """
    f = formula_table(df)
    tc = total_columns(df)
    # Tables sharing a column count share a column layout (1-2, 3-4, 5, 6-14, ...).
    ncol = df.group_by("table_number").agg(pl.col("column_number").n_unique().alias("layout"))
    item_tab = df.select("item_number", "table_number").unique(subset="item_number").join(ncol, on="table_number")
    vals = df.select("bank", "month_end", "item_number", "table_number", "column_number", "value")
    f = (f.join(item_tab.rename({"item_number": "parent_item", "table_number": "parent_table", "layout": "parent_layout"}), on="parent_item")
          .join(item_tab.rename({"item_number": "child_item", "table_number": "child_table", "layout": "child_layout"}), on="child_item"))
    # A formula is checked column-by-column only when every component shares the parent's layout.
    f = f.with_columns((pl.col("parent_layout") != pl.col("child_layout")).any().over("month_end", "parent_item").alias("mixed"))
    same = f.filter(~pl.col("mixed"))
    cross = (f.filter(pl.col("mixed"))
              .join(tc.rename({"table_number": "parent_table", "total_column": "column_number"}), on="parent_table")
              .join(tc.rename({"table_number": "child_table", "total_column": "child_column"}), on="child_table"))
    v = vals.drop("table_number")
    same_c = same.join(v.rename({"item_number": "child_item"}), on=["month_end", "child_item"])
    cross_c = cross.join(v.rename({"item_number": "child_item", "column_number": "child_column"}),
                         on=["month_end", "child_item", "child_column"])
    cols = ["bank", "month_end", "parent_item", "column_number", "child_item", "sign", "value"]
    child = (
        pl.concat([same_c.select(cols), cross_c.select(cols)])
        .group_by("bank", "month_end", "parent_item", "column_number")
        .agg((pl.col("value").fill_null(0) * pl.col("sign")).sum().alias("components_sum"),
             pl.len().alias("n_components"))
    )
    parent = v.rename({"item_number": "parent_item", "value": "reported"})
    chk = parent.join(child, on=["bank", "month_end", "parent_item", "column_number"]).filter(
        pl.col("reported").is_not_null()).with_columns(pl.lit("stated_formula").alias("check_type"))
    ta = v.filter(pl.col("item_number") == 277, pl.col("column_number") == 5).select("bank", "month_end", pl.col("value").alias("components_sum"))
    tel = v.filter(pl.col("item_number") == 102, pl.col("column_number") == 1).select("bank", "month_end", pl.col("value").alias("reported"))
    bal = tel.join(ta, on=["bank", "month_end"]).with_columns(
        pl.lit(102, pl.Int64).alias("parent_item"), pl.lit(1, pl.Int64).alias("column_number"),
        pl.lit(1, pl.UInt32).alias("n_components"), pl.lit("balance_identity").alias("check_type"))
    chk = pl.concat([chk, bal.select(chk.columns)])
    desc = df.select("month_end", pl.col("item_number").alias("parent_item"), pl.col("item_description").alias("parent_description")).unique(subset=["month_end", "parent_item"])
    return (chk.join(desc, on=["month_end", "parent_item"], how="left")
            .with_columns((pl.col("reported") - pl.col("components_sum")).alias("difference"))
            .with_columns(((pl.col("difference").abs() <= abs_tol) | (pl.col("difference").abs() <= rel_tol * pl.col("reported").abs())).alias("passed"))
            .sort("bank", "month_end", "parent_item", "column_number"))


# Headline lines used for reconciliation and slides. (item, column) refer to the BA900 form.
HEADLINES = {
    "total_assets": (277, 5),
    "total_assets_prior_to_netting": (284, 5),
    "cash_and_central_bank": (103, 5),
    "loans_and_advances_net": (110, 5),
    "credit_impairments": (194, 5),
    "investments_and_bills": (195, 5),
    "non_financial_assets": (258, 5),
    "other_assets": (267, 5),
    "deposits": (1, 7),
    "liabilities_to_public": (78, 4),
    "other_liabilities": (80, 4),
    "total_liabilities": (95, 4),
    "total_equity": (96, 1),
    "share_capital": (97, 1),
    "other_reserves": (101, 1),
    "total_equity_and_liabilities": (102, 1),
    "obs_guarantees": (285, 1),
    "obs_letters_of_credit": (286, 1),
    "obs_bankers_acceptances": (287, 1),
    "obs_committed_undrawn": (288, 1),
    "obs_underwriting": (289, 1),
    "obs_credit_derivatives": (290, 1),
    "obs_committed_capex": (291, 1),
    "obs_operating_leases": (292, 1),
    "obs_other_contingent": (293, 1),
    "obs_uncommitted_undrawn": (294, 1),
}


def headlines(df: pl.DataFrame) -> pl.DataFrame:
    keys = pl.DataFrame([{"metric": k, "item_number": i, "column_number": c} for k, (i, c) in HEADLINES.items()])
    h = df.join(keys, on=["item_number", "column_number"]).select(
        "bank", "institution_id", "month_end", "metric", "item_number", "column_number", "value", "unit")
    # Gross loans = net loans + impairments (BA900 item 110 is stated net of item 194)
    w = h.pivot(on="metric", index=["bank", "institution_id", "month_end", "unit"], values="value")
    w = w.with_columns((pl.col("loans_and_advances_net") + pl.col("credit_impairments").fill_null(0)).alias("loans_and_advances_gross"))
    return w.unpivot(index=["bank", "institution_id", "month_end", "unit"], variable_name="metric").sort("bank", "metric", "month_end")


def build() -> None:
    df = load()
    chk = internal_checks(df)
    chk.write_parquet(TIDY / "ba900_internal_checks.parquet")
    chk.write_csv(TIDY / "ba900_internal_checks.csv")
    h = headlines(df)
    h.write_parquet(TIDY / "ba900_headlines.parquet")
    h.write_csv(TIDY / "ba900_headlines.csv")
    print("checks", chk.shape, "pass rate", round(chk["passed"].mean(), 5))
    print("headlines", h.shape)


if __name__ == "__main__":
    build()
