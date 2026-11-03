"""Build the tidy report datasets and the BA900 reconciliation.

Outputs (data/tidy):
  report_statements.parquet   every value extracted from report tables (all vintages, all tables)
  financial_statements.parquet canonical audited statements: one value per
                               bank x basis x statement x period x line, as first published
  rwa.parquet                 every value from capital adequacy / RWA tables
  rwa_headline.parquet        standardised RWA and capital metrics per bank x period
  recon_ba900_vs_reports.parquet  BA900 month-end headline items vs published statements
"""
from __future__ import annotations

import re
import sys
import unicodedata
from datetime import date

import polars as pl

from .config import BANKS, TIDY

FYE_MONTH = {"capitec": 2, "absa": 12, "nedbank": 12, "sbsa": 12}
DEFAULT_UNIT = {"capitec": "R'000", "absa": "Rm", "nedbank": "Rm", "sbsa": "Rm"}
TO_RM = {"R'000": 1e-3, "Rm": 1.0, "Rbn": 1e3}
SUPPLEMENTARY = re.compile(
    r"\breview\b|\bsummary\b|\banalysis\b|annexure|reconciliation|not covered by the independent|liquidity risk|"
    r"risk management|finance review|financial review|\bour performance\b|segment|nedbank corporate",
    re.I,
)
ENTITY_NAMES = {b["bank"]: b["name"] for b in BANKS.values()}
UNIT_HEADING = re.compile(r"^(notes?|rm|r|r.?000|%|\s)+$", re.I)
FOOTER = re.compile(r"annual (consolidated )?(financial statements|report|integrated report)|integrated annual report", re.I)
NOTE_SUFFIX = re.compile(r"\s+((?:[A-Z]\d+(?:\.\d+)*|\d+\.\d+(?:\.\d+)*)\.?)$")


def norm_label(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = NOTE_SUFFIX.sub("", s)  # trailing note references such as 'B5.1.' or '24.3'
    s = re.sub(r"\(\d\)|\b\d\b$|(?<=[a-z])\d+$", "", s)  # footnote markers
    return re.sub(r"\s+", " ", s).strip()


def _note_from_label(s: str) -> str | None:
    m = NOTE_SUFFIX.search(unicodedata.normalize("NFKC", s or ""))
    return m.group(1).rstrip(".") if m else None


def label_key(s: str) -> str:
    return re.sub(r"[^a-z]", "", norm_label(s).lower())


def period_end(bank: str, year: int) -> date:
    if FYE_MONTH[bank] == 2:
        return date(year, 3, 1).fromordinal(date(year, 3, 1).toordinal() - 1)
    return date(year, 12, 31)


def report_statements() -> pl.DataFrame:
    df = pl.read_parquet(TIDY / "report_statements_raw.parquet")
    df = df.with_columns(
        pl.when(pl.col("page_title").str.contains("(?i)" + SUPPLEMENTARY.pattern) | (pl.col("table_kind") == "review"))
        .then(pl.lit("supplementary")).otherwise(pl.lit("primary")).alias("table_kind"),
        pl.col("unit").is_null().alias("unit_inferred"),
        pl.coalesce(pl.col("unit"), pl.col("bank").replace_strict(DEFAULT_UNIT)).alias("unit"),
    ).with_columns(
        (pl.col("value") * pl.col("unit").replace_strict(TO_RM, return_dtype=pl.Float64)).alias("value_rm"),
        pl.col("line_label").map_elements(norm_label, return_dtype=pl.Utf8).alias("line_label_clean"),
        pl.col("line_label").map_elements(label_key, return_dtype=pl.Utf8).alias("line_key"),
        pl.struct("bank", "column_year").map_elements(lambda r: period_end(r["bank"], r["column_year"]) if r["column_year"] else None, return_dtype=pl.Date).alias("period_end"),
        pl.col("bank").replace_strict(ENTITY_NAMES).alias("entity"),
        pl.col("column_header").str.contains("(?i)restated").alias("restated"),
        pl.when(pl.col("section").str.contains("(?i)" + UNIT_HEADING.pattern)).then(None).otherwise(pl.col("section")).alias("section"),
        pl.coalesce(pl.col("note"), pl.col("line_label").map_elements(_note_from_label, return_dtype=pl.Utf8)).alias("note"),
    )
    # Page footers picked up as rows ('... ANNUAL FINANCIAL STATEMENTS ... 2025')
    df = df.filter(~(pl.col("line_label").str.contains("(?i)" + FOOTER.pattern) & pl.col("value").is_between(1990, 2035)))
    cols =["bank", "entity", "fiscal_year", "doc_type", "source", "path", "statement", "table_kind", "table_id", "page",
            "page_half", "page_title", "basis", "basis_raw", "column_index", "column_header", "column_year", "period_end",
            "restated", "line_order", "section", "line_label", "line_label_clean", "line_key", "note", "value", "value_raw",
            "unit", "unit_inferred", "value_rm"]
    df = df.select(cols).rename({"fiscal_year": "report_year"})
    df.write_parquet(TIDY / "report_statements.parquet")
    df.write_csv(TIDY / "report_statements.csv")
    return df


def financial_statements(rs: pl.DataFrame) -> pl.DataFrame:
    """Canonical primary statements. For each bank/basis/statement/period the earliest report
    that contains it wins (the figures as first published); later restatements remain in
    report_statements. Opening-balance columns (e.g. '1 March 2023') are excluded."""
    p = rs.filter(pl.col("table_kind") == "primary", pl.col("statement").is_in(["sfp", "income"]),
                  ~pl.col("column_header").str.contains(r"(?i)\b1 (march|january)\b"))
    # rank vintages: same-year report first, otherwise the nearest later report
    p = p.with_columns((pl.col("report_year") - pl.col("column_year")).alias("vintage_lag")).filter(pl.col("vintage_lag") >= 0)
    # 'unspecified' basis only where the bank-year has no consolidated/separate statement
    known = p.filter(pl.col("basis") != "unspecified").select("bank", "statement", "column_year").unique()
    p = pl.concat([p.filter(pl.col("basis") != "unspecified"),
                   p.filter(pl.col("basis") == "unspecified").join(known, on=["bank", "statement", "column_year"], how="anti")])
    best = (p.group_by("bank", "basis", "statement", "column_year")
             .agg(pl.col("vintage_lag").min().alias("vintage_lag")))
    p = p.join(best, on=["bank", "basis", "statement", "column_year", "vintage_lag"])
    # if several tables of the same vintage remain (e.g. continuation pages), keep the first table
    first_tbl = p.group_by("bank", "basis", "statement", "column_year").agg(pl.col("table_id").min())
    p = p.join(first_tbl, on=["bank", "basis", "statement", "column_year", "table_id"])
    # one value per line (first occurrence in the column)
    p = p.sort("line_order").unique(subset=["bank", "basis", "statement", "column_year", "line_order", "column_index"], keep="first")
    out = p.select("bank", "entity", "basis", "statement", "column_year", "period_end", "report_year", "vintage_lag",
                   "restated", "section", "line_order", "line_label_clean", "line_key", "note", "value", "unit", "value_rm",
                   "table_id", "page", "path").rename({"column_year": "fiscal_year", "line_label_clean": "line_label"})
    out = out.sort("bank", "basis", "statement", "fiscal_year", "line_order")
    out.write_parquet(TIDY / "financial_statements.parquet")
    out.write_csv(TIDY / "financial_statements.csv")
    return out


RWA_METRICS = {
    "total_rwa": r"^total(riskweightedassets|rwa)|^riskweightedassets(rm|rbn)?$|^rwa(rm)?$|^totalriskweightedassetsrwa",
    "credit_risk_rwa": r"^creditrisk(excludingcounterpartycreditrisk|ccr)?$|^creditrisk",
    "counterparty_credit_risk_rwa": r"^counterpartycreditrisk|^counterpartyrisk",
    "operational_risk_rwa": r"^operationalrisk",
    "market_risk_rwa": r"^marketrisk",
    "equity_risk_rwa": r"^equity(risk|investmentrisk|positions|investmentsinbankingbook)",
    "other_assets_rwa": r"^other(assets|riskweightedassets)|^nonassetobligations",
    "cet1_ratio_pct": r"^(commonequitytier|cet)(one)?(capital)?(adequacy)?ratio|^commonequitytier$",
    "tier1_ratio_pct": r"^tier(one)?(capital)?(adequacy)?ratio|^tier$",
    "total_car_pct": r"^total(capital)?(adequacy)?ratio|^totalcapitaladequacy|^capitaladequacyratio",
    "qualifying_capital": r"^(total)?qualifying(regulatory)?capital",
}


def rwa(rs: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    r = rs.filter(pl.col("statement") == "rwa")
    r.write_parquet(TIDY / "rwa.parquet")
    r.write_csv(TIDY / "rwa.csv")
    rows = []
    entity_title = {"sbsa": r"sbsa|standard bank of south africa", "nedbank": r"nedbank limited", "capitec": r"capitec", "absa": r"absa bank"}
    for rec in r.iter_rows(named=True):
        key = rec["line_key"]
        if re.search(r"adjust|returnon|capitalrequirement|density|average|growth|change", key):
            continue
        # Pillar 3 documents cover group and bank: keep only tables titled for the bank entity
        if rec["doc_type"] == "pillar3" and not re.search(entity_title[rec["bank"]], rec["page_title"] or "", re.I):
            continue
        for metric, pat in RWA_METRICS.items():
            if re.search(pat, key):
                pct = metric.endswith("_pct")
                v = rec["value"] if pct else rec["value_rm"]
                if v is None or v <= 0 or (pct and not (0 < v < 100)) or (not pct and v < 100):
                    continue
                rows.append({"bank": rec["bank"], "report_year": rec["report_year"], "fiscal_year": rec["column_year"],
                             "period_end": rec["period_end"], "metric": metric, "value": v, "unit": "%" if pct else "Rm",
                             "line_label": rec["line_label_clean"], "basis_raw": rec["basis_raw"], "table_id": rec["table_id"],
                             "page": rec["page"], "doc_type": rec["doc_type"], "path": rec["path"], "column_header": rec["column_header"]})
                break
    h = pl.DataFrame(rows, infer_schema_length=None).with_columns(
        (pl.col("report_year") - pl.col("fiscal_year")).alias("vintage_lag")).filter(pl.col("vintage_lag").is_between(0, 6))
    # Selection per bank x year x metric:
    #  1. bank-entity columns over holding-group columns (Capitec reports both CBH and Capitec Bank);
    #  2. the value most reports agree on (multi-year reviews repeat each year's figure);
    #  3. the earliest report (figure as first published).
    h = h.with_columns(
        pl.col("basis_raw").replace_strict({"bank": 0, "company": 0}, default=1, return_dtype=pl.Int8).alias("basis_rank"),
        pl.col("value").round(0).alias("_v"),
    )
    best_basis = h.group_by("bank", "fiscal_year", "metric").agg(pl.col("basis_rank").min().alias("basis_rank"))
    h = h.join(best_basis, on=["bank", "fiscal_year", "metric", "basis_rank"])
    h = h.with_columns(pl.len().over("bank", "fiscal_year", "metric", "_v").alias("n_reports_agreeing"))
    h = (h.sort(["n_reports_agreeing", "vintage_lag", "page"], descending=[True, False, False])
          .unique(subset=["bank", "fiscal_year", "metric"], keep="first")
          .drop("_v", "basis_rank")
          .sort("bank", "metric", "fiscal_year"))
    h = h.with_columns(pl.lit("annual_report").alias("source_table"))
    # Pillar 3 OV1 (bank entity) supplies the risk-type breakdown where available
    ov = ov1(rs)
    ovm = []
    for rec in ov.filter(pl.col("scope") == "bank", pl.col("measure") == "rwa").iter_rows(named=True):
        for metric, pat in OV1_METRICS.items():
            if re.search(pat, rec["line_key"]):
                ovm.append({"bank": rec["bank"], "fiscal_year": rec["column_year"], "metric": metric, "value": rec["value_rm"],
                            "report_year": rec["report_year"], "line_label": rec["line_label_clean"], "page": rec["page"],
                            "path": rec["path"], "column_header": rec["column_header"]})
                break
    if ovm:
        o = (pl.DataFrame(ovm).with_columns((pl.col("report_year") - pl.col("fiscal_year")).alias("vintage_lag"))
               .filter(pl.col("vintage_lag") >= 0).sort("vintage_lag").unique(subset=["bank", "fiscal_year", "metric"], keep="first"))
        o = o.with_columns(pl.lit("pillar3_ov1").alias("source_table"), pl.lit("Rm").alias("unit"), pl.lit("pillar3").alias("doc_type"))
        # OV1 wins for component metrics; for total RWA the annual-report figure is kept and OV1 is compared in recon
        o_comp = o.filter(pl.col("metric") != "total_rwa")
        h = pl.concat([h.join(o_comp.select("bank", "fiscal_year", "metric"), on=["bank", "fiscal_year", "metric"], how="anti"),
                       o_comp], how="diagonal_relaxed")
        tot = (h.filter(pl.col("metric") == "total_rwa").select("bank", "fiscal_year", pl.col("value").alias("annual_report_rm"))
                .join(o.filter(pl.col("metric") == "total_rwa").select("bank", "fiscal_year", pl.col("value").alias("pillar3_ov1_rm")),
                      on=["bank", "fiscal_year"], how="full", coalesce=True)
                .with_columns((pl.col("annual_report_rm") - pl.col("pillar3_ov1_rm")).alias("diff_rm"),
                              ((pl.col("annual_report_rm") / pl.col("pillar3_ov1_rm") - 1) * 100).alias("diff_pct"))
                .sort("bank", "fiscal_year"))
        tot.write_parquet(TIDY / "recon_rwa_sources.parquet")
        tot.write_csv(TIDY / "recon_rwa_sources.csv")
        # fill total RWA gaps from OV1
        h = pl.concat([h, o.filter(pl.col("metric") == "total_rwa").join(h.select("bank", "fiscal_year", "metric"),
                                                                           on=["bank", "fiscal_year", "metric"], how="anti")],
                      how="diagonal_relaxed")
    h = h.sort("bank", "metric", "fiscal_year")
    h.write_parquet(TIDY / "rwa_headline.parquet")
    h.write_csv(TIDY / "rwa_headline.csv")
    return r, h


OV1_METRICS = {
    "total_rwa": r"^total(riskweightedassets|rwa)?$",
    "credit_risk_rwa": r"^creditrisk(excludingcounterpartycreditrisk|ccr)?",
    "counterparty_credit_risk_rwa": r"^counterpartycreditrisk",
    "cva_rwa": r"^creditvaluationadjustment|^cva",
    "equity_risk_rwa": r"^equity(risk|positions|investmentsinfunds)",
    "securitisation_rwa": r"^securitisationexposures",
    "market_risk_rwa": r"^marketrisk|^maarpkpertoraisckh",
    "operational_risk_rwa": r"^operationalrisk",
    "threshold_deductions_rwa": r"^amountsbelowthethresholds",
    "other_assets_rwa": r"^otherassets",
    "floor_adjustment_rwa": r"^flooradjustment",
}


def ov1(rs: pl.DataFrame) -> pl.DataFrame:
    """Pillar 3 OV1 tables, tagged with entity scope (bank entity vs holding group) and measure."""
    o = rs.filter(pl.col("statement") == "ov1")

    def scope(rec) -> str:
        t = (rec["page_title"] or "").lower()
        bank, ncols = rec["bank"], rec["n_cols"]
        if bank == "capitec":
            return "group" if "holdings" in t else "bank"
        if bank == "sbsa":
            return "bank" if ("sbsa" in t or "standard bank of south africa" in t) else "group"
        if bank == "nedbank":
            m = re.search(r"ov1:\s*nedbank\s*(limited|group)", t)
            if m:
                return "bank" if m.group(1) == "limited" else "group"
            # side-by-side layout (2017/2018): Nedbank Group columns, then Nedbank Limited columns
            return "group" if rec["column_index"] < ncols // 2 else "bank"
        return "bank"

    o = o.with_columns(pl.col("column_index").max().over("report_year", "table_id", "bank").add(1).alias("n_cols"))
    o = o.with_columns(
        pl.struct("page_title", "bank", "column_index", "n_cols").map_elements(scope, return_dtype=pl.Utf8).alias("scope"),
        pl.when(pl.col("column_header").str.contains("(?i)minimum|mrc")).then(pl.lit("minimum_capital_requirement"))
          .otherwise(pl.lit("rwa")).alias("measure"),
    ).drop("n_cols")
    o.write_parquet(TIDY / "rwa_ov1.parquet")
    o.write_csv(TIDY / "rwa_ov1.csv")
    return o


# Published-statement lines matched to BA900 headline items (patterns on normalised labels)
RECON_LINES = {
    "total_assets": r"^totalassets$",
    "total_liabilities": r"^totalliabilities$",
    "total_equity": r"^totalequity(attributabletoequityholders)?$|^totalequityattributable|^equity$",
    "total_equity_and_liabilities": r"^total(equityandliabilities|liabilitiesandequity)",
}


def reconcile(fs: pl.DataFrame) -> pl.DataFrame:
    hb = pl.read_parquet(TIDY / "ba900_headlines.parquet").filter(pl.col("bank") != "industry_total")
    hb = hb.with_columns((pl.col("value") / 1000).alias("ba900_rm")).select("bank", "month_end", "metric", "ba900_rm")
    rows = []
    for rec in fs.filter(pl.col("statement") == "sfp").iter_rows(named=True):
        for metric, pat in RECON_LINES.items():
            if re.search(pat, rec["line_key"]):
                rows.append({"bank": rec["bank"], "basis": rec["basis"], "fiscal_year": rec["fiscal_year"],
                             "month_end": rec["period_end"], "metric": metric, "report_rm": rec["value_rm"],
                             "report_line": rec["line_label"], "report_year": rec["report_year"], "page": rec["page"]})
                break
    rp = pl.DataFrame(rows).unique(subset=["bank", "basis", "fiscal_year", "metric"], keep="first")
    prenet = hb.filter(pl.col("metric") == "total_assets_prior_to_netting").select(
        "bank", "month_end", pl.lit("total_assets").alias("metric"), pl.col("ba900_rm").alias("ba900_prior_to_netting_rm"))
    rec = rp.join(hb, on=["bank", "month_end", "metric"], how="left").join(prenet, on=["bank", "month_end", "metric"], how="left").with_columns(
        (pl.col("report_rm") - pl.col("ba900_rm")).alias("diff_rm"),
        ((pl.col("report_rm") - pl.col("ba900_rm")) / pl.col("ba900_rm") * 100).alias("diff_pct"),
    ).sort("bank", "metric", "basis", "fiscal_year")
    rec.write_parquet(TIDY / "recon_ba900_vs_reports.parquet")
    rec.write_csv(TIDY / "recon_ba900_vs_reports.csv")
    return rec


def vintage_consistency(rs: pl.DataFrame) -> pl.DataFrame:
    """Each line as first published (report year Y) vs the comparative in report Y+1."""
    p = rs.filter(pl.col("table_kind") == "primary", pl.col("statement").is_in(["sfp", "income"]))
    keys = ["bank", "basis", "statement", "line_key", "column_year"]
    a = (p.filter(pl.col("report_year") == pl.col("column_year"))
          .select(*keys, pl.col("line_label_clean").alias("line_label"), pl.col("value").alias("value_original"), "unit")
          .unique(subset=keys, keep="first"))
    b = (p.filter(pl.col("report_year") == pl.col("column_year") + 1)
          .select(*keys, pl.col("value").alias("value_comparative"), pl.col("restated").alias("flagged_restated"))
          .unique(subset=keys, keep="first"))
    j = a.join(b, on=keys).with_columns(
        (pl.col("value_comparative") - pl.col("value_original")).alias("diff"),
        (pl.col("value_comparative") == pl.col("value_original")).alias("identical"),
    ).rename({"column_year": "fiscal_year"}).sort("bank", "basis", "statement", "fiscal_year", "line_key")
    j.write_parquet(TIDY / "recon_vintage_consistency.parquet")
    j.write_csv(TIDY / "recon_vintage_consistency.csv")
    return j


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    rs = report_statements()
    fs = financial_statements(rs)
    _, h = rwa(rs)
    rec = reconcile(fs)
    vc = vintage_consistency(rs)
    print("report_statements", rs.shape, "financial_statements", fs.shape, "rwa_headline", h.shape,
          "recon", rec.shape, "vintage", vc.shape)


if __name__ == "__main__":
    main()
