"""Download and catalogue bank-entity annual reports listed in data/reference/report_catalogue.csv."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pdfplumber
import polars as pl

from .config import REFERENCE, REPORTS, ROOT, TIDY

CLIENT = httpx.Client(
    timeout=300, follow_redirects=True, headers={"User-Agent": "industry-analysis-ai/0.1 (research)"}
)

_YE = re.compile(r"(?:year|period)\s+ended\s+(?:3[01]|2[89])\s+(December|February)\s+(20\d\d)", re.I)


def _fname(row: dict) -> str:
    h = hashlib.sha1(row["url"].encode()).hexdigest()[:8]
    return f"{row['doc_type']}_{row['fy_hint'] or 'unknown'}_{h}.pdf"


def download(row: dict) -> dict:
    out = REPORTS / row["bank"] / _fname(row)
    status = "cached"
    if not out.exists():
        try:
            r = CLIENT.get(row["url"])
            if r.status_code != 200 or not r.content.startswith(b"%PDF"):
                return {**row, "status": f"failed:{r.status_code}", "path": None}
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(r.content)
            status = "downloaded"
        except httpx.HTTPError as e:
            return {**row, "status": f"failed:{type(e).__name__}", "path": None}
    return {**row, "status": status, "path": out.relative_to(ROOT).as_posix()}


def detect_year(path) -> tuple[int | None, int]:
    """Most common 'year ended DD Month YYYY' in the first pages, and page count."""
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        txt = " ".join((p.extract_text() or "") for p in pdf.pages[: min(12, n)])
    years = Counter(int(m[2]) for m in _YE.finditer(txt))
    return (years.most_common(1)[0][0] if years else None), n


def run(workers: int = 4) -> pl.DataFrame:
    cat = (pl.read_csv(REFERENCE / "report_catalogue.csv", infer_schema_length=0)
           .filter(pl.col("include") == "true").drop("include", "note").to_dicts())
    with ThreadPoolExecutor(workers) as ex:
        rows = list(ex.map(download, cat))
    for r in rows:
        r["detected_fy"], r["pages"], r["sha256"] = None, None, None
        if r["path"]:
            p = ROOT / r["path"]
            r["detected_fy"], r["pages"] = detect_year(p)
            r["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    df = pl.DataFrame(rows, infer_schema_length=None)
    df.write_csv(TIDY / "report_inventory.csv")
    return df


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    pl.Config.set_tbl_rows(100)
    pl.Config.set_tbl_width_chars(200)
    print(run().select("bank", "fy_hint", "detected_fy", "doc_type", "source", "status", "pages"))
