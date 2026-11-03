from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
TIDY = DATA / "tidy"
REPORTS = DATA / "reports"
REFERENCE = DATA / "reference"

SARB_BASE = "https://custom.resbank.co.za/SarbWebApi/SarbData/IFData"

# SARB institution codes, stable across 2008-present BA900 history.
BANKS = {
    "333107": {"bank": "capitec", "name": "Capitec Bank Limited"},
    "34118": {"bank": "absa", "name": "Absa Bank Limited"},
    "416088": {"bank": "nedbank", "name": "Nedbank Limited"},
    "416061": {"bank": "sbsa", "name": "The Standard Bank of South Africa Limited"},
}
BA900_TOTAL_ID = "TOTAL"
INDUSTRY_TOTAL_ID = "I9999999"  # BA100/BA110/BA120/BA700 publish only the all-banks total
INDUSTRY_RETURNS = ["BA100", "BA110", "BA120", "BA700"]
