"""SARB IFData API client: download, cache and tidy BA-series returns.

Every value in a return's XML becomes one row, so no granularity is lost.
Values are reported in R'000 as submitted to the SARB.
"""
from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import httpx
import polars as pl

from .config import BANKS, BA900_TOTAL_ID, INDUSTRY_RETURNS, INDUSTRY_TOTAL_ID, RAW, SARB_BASE, TIDY

CLIENT = httpx.Client(timeout=120, headers={"User-Agent": "industry-analysis-ai/0.1"})


def _get(url: str, retries: int = 4):
    for i in range(retries):
        try:
            r = CLIENT.get(url)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            if i == retries - 1:
                raise
            time.sleep(2 ** i)


def periods(if_type: str) -> list[str]:
    """Period strings exactly as the API expects them (formats vary by year)."""
    return sorted(_get(f"{SARB_BASE}/GetPeriods/{if_type}"))


def institutions(if_type: str, period: str) -> list[dict]:
    return _get(f"{SARB_BASE}/GetInstitutions/{if_type}/{period}")


def fetch(if_type: str, period: str, inst: str, refresh: bool = False) -> Path | None:
    out = RAW / if_type / inst / f"{period}.json"
    if out.exists() and not refresh:
        return out
    data = _get(f"{SARB_BASE}/GetInstitutionData/{if_type}/{period}/{inst}")
    if not data:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def _num(v: str | None):
    if v is None or v.strip() == "":
        return None
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def parse(path: Path) -> list[dict]:
    """Flatten one cached return into tidy records."""
    recs = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        root = ET.fromstring(item["XMLData"])
        y, m, d = root.get("TheYear"), root.get("TheMonth"), root.get("TheDay")
        ref = date(int(y), int(m), int(d))
        for form_i, form in enumerate(root.iter("SARBForm"), start=1):
            for table in form.iter("Table"):
                cols = {
                    c.get("ColumnNumber"): (c.get("ColumnCode"), c.get("ColumnDescription"))
                    for c in table.findall("ColumnHeader")
                }
                for row in table.findall("Row"):
                    for col in row.findall("Column"):
                        cn = col.get("ColumnNumber")
                        code, cdesc = cols.get(cn, (None, None))
                        recs.append({
                            "return_type": item["IFType"],
                            "institution_id": item["InstitutionId"],
                            "institution_name": item["InstitutionName"],
                            "period": ref,
                            "api_period": item["Period"],
                            "last_update": item.get("LastUpdate"),
                            "form": form_i,
                            "table_number": _int(table.get("TableNumber")),
                            "table_description": table.get("TableDescription"),
                            "row_number": _int(row.get("RowNumber")),
                            "item_number": _int(row.get("ItemNumber")),
                            "item_description": row.get("ItemDescription"),
                            "column_number": _int(cn),
                            "column_code": code,
                            "column_description": cdesc,
                            "value": _num(col.get("Value")),
                            "value_raw": col.get("Value"),
                        })
    return recs


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def download_all(workers: int = 6) -> list[Path]:
    jobs = []
    for p in periods("BA900"):
        for inst in [*BANKS, BA900_TOTAL_ID]:
            jobs.append(("BA900", p, inst))
    for rt in INDUSTRY_RETURNS:
        for p in periods(rt):
            jobs.append((rt, p, INDUSTRY_TOTAL_ID))
    paths, failed = [], []
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch, *j): j for j in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                if (p := f.result()) is not None:
                    paths.append(p)
            except Exception as e:  # noqa: BLE001
                failed.append((futs[f], repr(e)))
            if i % 100 == 0:
                print(f"{i}/{len(jobs)} fetched", flush=True)
    print(f"done: {len(paths)} files, {len(failed)} failed")
    for j, e in failed:
        print("FAILED", j, e)
    return paths


def build_tidy() -> dict[str, pl.DataFrame]:
    TIDY.mkdir(parents=True, exist_ok=True)
    out = {}
    for rt in ["BA900", *INDUSTRY_RETURNS]:
        files = sorted((RAW / rt).rglob("*.json"))
        recs = [r for f in files for r in parse(f)]
        df = pl.DataFrame(recs, infer_schema_length=None)
        bank_map = {k: v["bank"] for k, v in BANKS.items()}
        df = df.with_columns(
            pl.col("institution_id").replace_strict(bank_map, default="industry_total").alias("bank"),
            pl.col("period").dt.month_end().alias("month_end"),
            pl.lit("R'000").alias("unit"),
        ).sort(["bank", "period", "table_number", "row_number", "column_number"])
        name = "ba900" if rt == "BA900" else f"industry_{rt.lower()}"
        df.write_parquet(TIDY / f"{name}.parquet")
        df.write_csv(TIDY / f"{name}.csv")
        out[name] = df
        print(name, df.shape)
    return out


if __name__ == "__main__":
    download_all()
    build_tidy()
