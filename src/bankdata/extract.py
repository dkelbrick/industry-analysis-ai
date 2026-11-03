"""Extract primary statements and RWA tables from the downloaded annual reports.

For each document and statement type, candidate pages are those naming the statement
(heading matches first). Each candidate is parsed with ``statements.parse_page`` and validated:

* statement of financial position: total assets == total equity and liabilities
  (or total liabilities + total equity) in the first column;
* income statement: an interest income / net interest income line and a profit line;
* RWA: a 'total risk-weighted assets' / 'total RWA' / 'risk-weighted assets' line.

Validated tables are classified:

* ``primary``: first column is the document's fiscal year and at most 6 columns
  (the audited statement itself; group/company and restated columns allowed);
* ``review``: multi-year reviews or tables whose first column is not the fiscal year.
  These are kept because they extend history (e.g. Nedbank's ten-year review).

The best primary table is kept per (statement, basis), plus every distinct review table.
Every parsed line is kept (tidy, one row per value).
"""
from __future__ import annotations

import re
import sys
import traceback
import unicodedata
from concurrent.futures import ProcessPoolExecutor

import polars as pl

from .config import ROOT, TIDY
from .locate import squash
from .pdftext import page_texts
from .statements import is_spread, parse_page

HEAD = 900  # characters of squashed text treated as the page heading zone

TITLES = {
    "sfp": r"statements?offinancialposition|balancesheets?",
    "income": r"(incomestatements?|statements?ofprofitorloss|statements?ofcomprehensiveincome)",
    "rwa": r"(risk-?weightedassets|rwa)",
    "ov1": "ov1[:–\\-]",  # 'OV1:' or 'OV1 –' table titles
}
MUST = {
    "sfp": [r"totalassets"],
    "income": [r"interestincome"],
    "rwa": [r"(total(risk-?weightedassets|rwa)|risk-?weightedassets)"],
    "ov1": [r"operationalrisk", r"creditrisk"],
}
EXCLUDE_TITLE = re.compile(r"segment|business unit|businessunit", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKC", s).lower())


def _first_col(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["column_index"] == 0]


def _find(rows, pat):
    for r in rows:
        if re.search(pat, _norm(r["line_label"])) and r["value"] is not None:
            return r["value"]
    return None


def validate(kind: str, parsed: dict) -> tuple[bool, str]:
    rows = parsed["rows"]
    if not rows:
        return False, "no rows"
    if EXCLUDE_TITLE.search(parsed["title"]):
        return False, "segment / business unit table"
    ncols = len(parsed["columns"])
    if ncols > 12:
        return False, f"{ncols} columns"
    fc = _first_col(rows)
    if kind == "sfp":
        ta = _find(fc, r"^totalassets$")
        tel = _find(fc, r"^total(equityandliabilities|liabilitiesandequity)")
        tl = _find(fc, r"^totalliabilities$")
        te = _find(fc, r"^totalequity$")
        if ta is None:
            return False, "no total assets"
        if tel is not None and abs(ta - tel) <= max(2, 1e-5 * abs(ta)):
            return True, "A = E + L (stated total)"
        if tl is not None and te is not None and abs(ta - tl - te) <= max(2, 1e-5 * abs(ta)):
            return True, "A = L + E"
        return False, "total assets found but identity not on page"
    if kind == "income":
        # Capitec (2017+) reports 'net lending/investment income' rather than net interest income
        nii = _find(fc, r"^netinterestincome|^net(lending|interest)(insurance|and|investment)*income")
        ii = _find(fc, r"^interest(andsimilar)?income|^interestincome")
        pft = _find(fc, r"(profit|loss|earnings).*(fortheyear|forthereportingperiod|fortheperiod)|^profitattributable|^netprofit")
        if nii is None and ii is None:
            return False, "no interest income line"
        if any(re.search(r"^totalassets$", _norm(r["line_label"])) for r in fc):
            return False, "balance sheet page"
        ie = _find(fc, r"^interest(andsimilar)?expense|^interestexpense")
        note = "interest lines found"
        if nii is not None and ii is not None and ie is not None and abs(abs(ii) - abs(ie) - nii) <= max(2, 1e-4 * abs(nii)):
            note = "NII = interest income - interest expense"
        return (pft is not None, note + ("" if pft is not None else "; no profit line"))
    if kind == "ov1":
        tot = _find(rows, r"^total$|^total(riskweightedassets|rwa)?$|^total1$")
        op = _find(rows, r"^operationalrisk")
        return (tot is not None and op is not None, "OV1 total and operational risk found" if tot is not None and op is not None else "not an OV1 table")
    if kind == "rwa":
        tot = _find(rows, r"^total(riskweightedassets|rwa)|^riskweightedassets(rm|rbn)?$|^rwa(rm)?$")
        return (tot is not None, "total RWA found" if tot is not None else "no total RWA line")
    return False, "unknown kind"


def candidates(texts: list[str], kind: str) -> list[int]:
    """Pages naming the statement and holding its key line; heading matches ranked first."""
    head_hits, body_hits, untitled = [], [], []
    for i, t in enumerate(texts):
        s = squash(t)
        if not all(re.search(p, s) for p in MUST[kind]):
            continue
        if re.search(TITLES[kind], s[:HEAD]):
            head_hits.append(i)
        elif re.search(TITLES[kind], s):
            body_hits.append(i)
        elif kind == "sfp" and re.search(r"totalequityandliabilities|totalliabilitiesandequity", s):
            untitled.append(i)  # e.g. Capitec 2014/15 statements have no printed title
    return head_hits + body_hits + untitled


def _closing_pattern(kind: str) -> str | None:
    return {"sfp": r"totalequityandliabilities|totalliabilitiesandequity",
            "income": r"(profit|loss).*(fortheyear|forthereportingperiod|fortheperiod)"}.get(kind)


def _parse_variants(path, pi: int) -> list[tuple[str | None, dict | None]]:
    if is_spread(path, pi):
        return [(h, parse_page(path, pi, half=h)) for h in ("left", "right")]
    return [(None, parse_page(path, pi))]


def _classify(parsed: dict, fy: int | None) -> str:
    cols = parsed["columns"]
    first = cols[0]["year"] if cols else None
    if fy is not None and first == fy and len(cols) <= 6:
        return "primary"
    return "review"


def _basis_key(parsed: dict) -> str:
    return "+".join(sorted({r["basis"] for r in parsed["rows"]}))


def extract_doc(row: dict) -> tuple[list[dict], list[dict]]:
    path = ROOT / row["path"]
    texts = page_texts(path)
    fy = row["fy"]
    out_rows, log = [], []
    sfp_pages: list[int] = []
    kinds = ("sfp", "income", "rwa", "ov1") if row["doc_type"] == "pillar3" else ("sfp", "income", "rwa")
    for kind in kinds:
        cands = candidates(texts, kind)
        if kind == "income" and sfp_pages:
            # Untitled income statements usually follow the statement of financial position
            near = [p for sp in sfp_pages for p in (sp, sp + 1, sp + 2) if p < len(texts)]
            cands = list(dict.fromkeys(near + cands))
        kept: list[tuple[str, int, str | None, dict]] = []
        seen_primary: set[str] = set()
        for pi in cands[:60]:
            try:
                variants = _parse_variants(path, pi)
            except Exception as e:  # noqa: BLE001
                log.append({**_meta(row), "statement": kind, "page": pi + 1, "ok": False, "reason": f"error {e!r}", "table_kind": None})
                continue
            for half, parsed in variants:
                if not parsed:
                    continue
                clos = _closing_pattern(kind)
                if half is None and clos and not any(re.search(clos, _norm(r["line_label"])) for r in parsed["rows"]) and pi + 1 < len(texts):
                    nxt = parse_page(path, pi + 1)
                    if nxt and len(nxt["columns"]) == len(parsed["columns"]) and not EXCLUDE_TITLE.search(nxt["title"]):
                        parsed["rows"] += nxt["rows"]
                ok, reason = validate(kind, parsed)
                tkind = _classify(parsed, fy) if ok else None
                log.append({**_meta(row), "statement": kind, "page": pi + 1, "ok": ok,
                            "reason": reason + (f" [{half} half]" if half else ""), "table_kind": tkind})
                if not ok:
                    continue
                if kind == "ov1":
                    tkind = "primary"  # every entity's OV1 table is kept
                elif tkind == "primary":
                    bk = _basis_key(parsed)
                    if bk in seen_primary:
                        continue
                    seen_primary.add(bk)
                    if kind == "sfp":
                        sfp_pages.append(pi)
                kept.append((tkind, pi, half, parsed))
        if not kept:
            log.append({**_meta(row), "statement": kind, "page": None, "ok": False,
                        "reason": f"no validated page among {len(cands)} candidates", "table_kind": None})
        has_primary = any(k[0] == "primary" for k in kept)
        for tkind, page, half, parsed in kept:
            if kind == "rwa" and tkind == "review" and has_primary:
                continue
            tid = f"{kind}-p{page + 1}{'-' + half if half else ''}"
            for k, r in enumerate(parsed["rows"]):
                out_rows.append({**_meta(row), "statement": kind, "table_kind": tkind, "table_id": tid,
                                 "page": page + 1, "page_half": half, "line_order": k, "page_title": parsed["title"][:200], **r})
    return out_rows, log


def _meta(row: dict) -> dict:
    return {"bank": row["bank"], "fiscal_year": row["fy"], "doc_type": row["doc_type"], "source": row["source"], "path": row["path"]}


def _safe(row):
    try:
        return extract_doc(row)
    except Exception:  # noqa: BLE001
        return [], [{**_meta(row), "statement": "all", "page": None, "ok": False, "reason": traceback.format_exc()[-300:], "table_kind": None}]


def run(workers: int = 4) -> tuple[pl.DataFrame, pl.DataFrame]:
    inv = pl.read_csv(TIDY / "report_inventory.csv").filter(pl.col("path").is_not_null())
    inv = inv.with_columns(pl.coalesce(pl.col("detected_fy"), pl.col("fy_hint").cast(pl.Int64)).alias("fy"))
    rows, logs = [], []
    with ProcessPoolExecutor(workers) as ex:
        for r, lg in ex.map(_safe, inv.to_dicts()):
            rows += r
            logs += lg
    df = pl.DataFrame(rows, infer_schema_length=None)
    lg = pl.DataFrame(logs, infer_schema_length=None)
    df.write_parquet(TIDY / "report_statements_raw.parquet")
    lg.write_csv(TIDY / "report_extraction_log.csv")
    return df, lg


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    df, lg = run()
    t = df.select("bank", "fiscal_year", "statement", "table_kind", "table_id", "basis").unique()
    summ = t.group_by("bank", "fiscal_year", "statement").agg(
        pl.concat_str(pl.col("table_kind"), pl.col("table_id"), pl.col("basis"), separator="|").unique().sort().alias("tables"))
    for r in summ.sort("bank", "fiscal_year", "statement").iter_rows(named=True):
        print(r["bank"], r["fiscal_year"], r["statement"], r["tables"])
    errs = lg.filter(pl.col("reason").str.contains("Traceback|error"))
    print("errors:", errs.height)
    print(errs.head(5))
