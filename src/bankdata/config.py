from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
TIDY = DATA / "tidy"
REPORTS = DATA / "reports"
REFERENCE = DATA / "reference"

SARB_BASE = "https://custom.resbank.co.za/SarbWebApi/SarbData/IFData"

# SARB institution codes, stable across the full BA900 history (2008 to date).
BANKS = {
    "333107": {"bank": "capitec", "name": "Capitec Bank Limited"},
    "34118": {"bank": "absa", "name": "Absa Bank Limited"},
    "416088": {"bank": "nedbank", "name": "Nedbank Limited"},
    "416061": {"bank": "sbsa", "name": "The Standard Bank of South Africa Limited"},
}
BA900_TOTAL_ID = "TOTAL"
INDUSTRY_TOTAL_ID = "I9999999"  # BA100/BA110/BA120/BA200/BA700 publish only the all-banks total
# BA200 (credit risk summary) carries the industry impaired-advances (NPL) ratio
INDUSTRY_RETURNS = ["BA100", "BA110", "BA120", "BA200", "BA700"]

# Analysis window: financial years from MIN_FISCAL_YEAR onwards.
# A bank's financial year is named after the calendar year in which it ends:
# Capitec's FY2014 runs Mar 2013 - Feb 2014; the other banks' FY2014 is calendar 2014.
MIN_FISCAL_YEAR = 2014
FYE_MONTH = {"capitec": 2, "absa": 12, "nedbank": 12, "sbsa": 12, "industry_total": 12}
# First month needed by any bank (Capitec's FY2014 starts in March 2013)
MIN_MONTH = (MIN_FISCAL_YEAR - 1, 3)


def fiscal_year_expr(bank_col: str = "bank", date_col: str = "month_end"):
    """Polars expression: financial year of a month-end date for the bank in ``bank_col``."""
    import polars as pl

    fye = pl.col(bank_col).replace_strict(FYE_MONTH, default=12, return_dtype=pl.Int32)
    d = pl.col(date_col)
    return pl.when(d.dt.month() > fye).then(d.dt.year() + 1).otherwise(d.dt.year()).cast(pl.Int32)
