"""Locate primary statement and capital (RWA) pages inside report PDFs.

Matching runs on lower-cased text with all whitespace removed, so letter-spaced
PDFs ("C a s h") match the same patterns as normal ones.
"""
from __future__ import annotations

import re
from concurrent.futures import ProcessPoolExecutor

import polars as pl

from .config import ROOT, TIDY
from .pdftext import page_texts

PATTERNS = {
    "sfp": [r"statements?offinancialposition", r"totalassets", r"totalequity"],
    "income": [r"(incomestatements?|statements?of(profit|comprehensiveincome))", r"netinterestincome", r"(profit|loss)fortheyear"],
    "rwa": [r"risk-?weightedassets", r"creditrisk", r"operationalrisk", r"marketrisk"],
}


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def locate(path: str) -> dict[str, list[int]]:
    texts = [squash(t) for t in page_texts(ROOT / path)]
    return {k: [i for i, t in enumerate(texts) if all(re.search(p, t) for p in pats)] for k, pats in PATTERNS.items()}


def _one(row: dict) -> dict:
    return {**row, **{f"pages_{k}": v for k, v in locate(row["path"]).items()}}


def run(workers: int = 4) -> pl.DataFrame:
    inv = pl.read_csv(TIDY / "report_inventory.csv").filter(pl.col("path").is_not_null()).to_dicts()
    with ProcessPoolExecutor(workers) as ex:
        rows = list(ex.map(_one, inv))
    return pl.DataFrame(rows, infer_schema_length=None)


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    df = run()
    df.write_parquet(TIDY / "report_pages.parquet")
    for r in df.iter_rows(named=True):
        print(r["bank"], r["fy_hint"], r["detected_fy"], r["doc_type"], r["pages_sfp"][:8], r["pages_income"][:8], r["pages_rwa"][:10])
