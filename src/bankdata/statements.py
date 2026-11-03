"""Generic parser for primary financial statement pages in bank annual reports.

Approach
--------
1. Rebuild text lines from pdfplumber characters, inserting spaces from character gaps
   (handles letter-spaced PDFs such as Capitec's).
2. Find the column header line: the line holding 2-8 four-digit year tokens to the right
   of the page's label area. Each year token's right edge is a column anchor
   (numbers in these statements are right-aligned).
3. Basis headers (GROUP / COMPANY / CONSOLIDATED / SEPARATE / BANK) above the year line
   assign each anchor to a reporting basis.
4. Every other line is split into a label (left) and numeric tokens (right). Digit groups
   are assigned to the nearest anchor by right edge and concatenated, so space-separated
   thousands ("44 269 236") are read correctly.
5. Label-only lines immediately followed by a numeric line are treated as wrapped labels.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import pdfplumber

NUM_TOKEN = re.compile(r"^\(?-?\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?\)?%?$|^\(?\d+(?:\.\d+)?\)?$")
NIL = {"—", "–", "-", "‒", "nil", "Nil"}
YEAR = re.compile(r"^(19|20)\d\d(\d|\*|\(\d\))?$")  # trailing footnote marker allowed
BASIS = re.compile(r"^(GROUP|COMPANY|CONSOLIDATED|SEPARATE|BANK|Group|Company|Consolidated|Separate|Bank)$")


@dataclass
class Token:
    text: str
    x0: float
    x1: float
    top: float


@dataclass
class Line:
    top: float
    tokens: list[Token] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(t.text for t in self.tokens)


def _chars_to_lines(chars: list[dict], y_tol: float = 2.5) -> list[Line]:
    """Rebuild lines from characters. Explicit space characters always split tokens;
    otherwise a gap wider than ~0.22 em splits (needed for PDFs without space glyphs)."""
    chars = [c for c in chars if c.get("upright", True) and c["text"] not in ("", chr(10))]
    rows: list[list[dict]] = []
    for c in sorted((c for c in chars if c["text"].strip()), key=lambda c: c["top"]):
        if rows and abs(rows[-1][0]["top"] - c["top"]) <= y_tol:
            rows[-1].append(c)
        else:
            rows.append([c])
    spaces = [c for c in chars if not c["text"].strip()]
    lines = []
    for row in rows:
        top = row[0]["top"]
        sp = sorted(((c["x0"] + c["x1"]) / 2 for c in spaces if abs(c["top"] - top) <= y_tol))
        row.sort(key=lambda c: c["x0"])
        toks: list[Token] = []
        for c in row:
            size = c.get("size", 8) or 8
            if toks:
                prev = toks[-1]
                has_space = any(prev.x1 - 0.5 <= x <= c["x0"] + 0.5 for x in sp)
                gap = c["x0"] - prev.x1
                # Letter-spaced PDFs put space glyphs between letters, so a space glyph only
                # splits when there is also a visible gap.
                if not (has_space and gap > 0.08 * size) and gap <= 0.22 * size:
                    prev.text += c["text"]
                    prev.x1 = max(prev.x1, c["x1"])
                    continue
            toks.append(Token(c["text"], c["x0"], c["x1"], c["top"]))
        lines.append(Line(top=top, tokens=toks))
    return lines


def _starts_lower(s: str) -> bool:
    s = s.lstrip("(–-• ")
    return bool(s) and (s[0].islower() or s[0] in "&")


def _resolve_label(pending: list[str], label: str) -> tuple[str, list[str]]:
    """Split pending label-only lines into section headings and a wrapped-label prefix.

    Wrapped continuations start lower-case ('profit or loss (FVTPL)'); headings do not.
    """
    if not label:
        # numbers on their own line: the last pending line is the label
        return (pending[-1] if pending else ""), pending[:-1]
    if not _starts_lower(label) or not pending:
        return label, pending
    k = len(pending) - 1
    while k > 0 and _starts_lower(pending[k]):
        k -= 1
    return " ".join(pending[k:] + [label]), pending[:k]


DECIMAL_COMMA = re.compile(r"^\(?-?\d+,\d{1,2}\)?%?$")  # '3,90' (Nedbank ratios); '1,234' stays thousands


def _is_num(s: str) -> bool:
    return bool(NUM_TOKEN.match(s)) or bool(DECIMAL_COMMA.match(s)) or s in NIL


def _to_float(s: str) -> float | None:
    if s in NIL:
        return 0.0
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    if DECIMAL_COMMA.match(s):
        s = s.replace(",", ".")
    s2 = re.sub(r"[^\d.]", "", s)
    if not s2:
        return None
    try:
        v = float(s2)
    except ValueError:  # e.g. several decimals merged into one column: not a single value
        return None
    return -v if neg else v


@dataclass
class Column:
    anchor: float  # right edge
    header: str
    year: int | None
    basis: str | None


def _header_candidates(lines: list[Line], label_limit: float) -> list[int]:
    out = []
    for i, ln in enumerate(lines[:60]):
        right = [t for t in ln.tokens if t.x0 > label_limit]
        years = [t for t in right if YEAR.match(t.text)]
        if 1 <= len(years) <= 8 and len(years) >= 0.3 * len(right):
            out.append(i)
    return out


def _alignment_score(lines: list[Line], i: int, anchors: list[float]) -> int:
    """How many numeric tokens in the next lines end within 3pt of one of the anchors."""
    n = 0
    for ln in lines[i + 1: i + 40]:
        for t in ln.tokens:
            if _is_num(t.text) and any(abs(t.x1 - a) <= 3 for a in anchors):
                n += 1
    return n


def _year_tokens(lines: list[Line], i: int, label_limit: float, window: float = 18.0) -> tuple[int, list[Token]]:
    """Year tokens on line i plus staggered year tokens on nearby lines (e.g. '2023' printed
    a line above '2024'). Returns the index of the lowest line used, and tokens sorted by x."""
    base = lines[i]
    toks = [t for t in base.tokens if t.x0 > label_limit and YEAR.match(t.text)]
    last = i
    for k in range(max(0, i - 3), min(len(lines), i + 4)):
        if k == i or abs(lines[k].top - base.top) > window:
            continue
        right = [t for t in lines[k].tokens if t.x0 > label_limit]
        yrs = [t for t in right if YEAR.match(t.text)]
        if yrs and len(yrs) >= 0.5 * len(right):
            for t in yrs:
                if all(abs(t.x1 - u.x1) > 6 for u in toks):
                    toks.append(t)
                    last = max(last, k)
    return last, sorted(toks, key=lambda t: t.x1)


SUB_IGNORE = re.compile(r"^(notes?|rm|r|r.?000|%|\(restated\)|restated)$", re.I)


def _sub_tokens(line: Line, label_limit: float) -> list[Token]:
    """Candidate sub-header tokens right of the label area, ignoring note and unit markers."""
    return [t for t in line.tokens if t.x0 > label_limit and not SUB_IGNORE.match(t.text)]


def _find_header(lines: list[Line], label_limit: float) -> tuple[int, list[Column]] | None:
    cands = _header_candidates(lines, label_limit)
    if not cands:
        return None
    scored = []
    for k in cands:
        last, toks = _year_tokens(lines, k, label_limit)
        score = _alignment_score(lines, last, [t.x1 for t in toks])
        # a sub-header line (e.g. 'RWA MRC RWA') may be what the numbers align to
        for s in range(last + 1, min(len(lines), last + 3)):
            sub = _sub_tokens(lines[s], label_limit)
            if not sub or any(_is_num(t.text) for t in sub):
                break
            if len(toks) < len(sub) <= 12:
                score = max(score, _alignment_score(lines, s, [t.x1 for t in sub]))
            break
        scored.append((score, -k, k, last, toks))
    # Prefer the year line whose positions line up with the numbers below it
    _, _, i, last, years = max(scored, key=lambda r: (r[0], r[1]))
    if len(years) > 12:
        return None
    cols = [Column(anchor=t.x1, header=t.text, year=int(t.text[:4]), basis=None) for t in years]
    # Sub-headers under the years (e.g. 'Dec 2024 | Dec 2023' over 'RWA MRC RWA'): when a following
    # line has more short header words than there are years and they align better with the
    # numbers, they become the columns, each inheriting the nearest year above it.
    for k in range(last + 1, min(len(lines), last + 3)):
        sub = _sub_tokens(lines[k], label_limit)
        if not sub or any(_is_num(t.text) for t in sub):
            break
        if len(years) < len(sub) <= 12 and all(len(t.text) <= 10 for t in sub):
            if _alignment_score(lines, k, [t.x1 for t in sub]) > _alignment_score(lines, last, [c.anchor for c in cols]):
                def near_year(t):
                    cx = (t.x0 + t.x1) / 2
                    return min(years, key=lambda y: abs((y.x0 + y.x1) / 2 - cx) if y.x1 >= t.x0 - 25 else 1e9 + abs(y.x1 - t.x1))
                cols = [Column(anchor=t.x1, header=f"{near_year(t).text} {t.text}", year=int(near_year(t).text[:4]), basis=None) for t in sub]
                last = k
            break
    bounds = [label_limit] + [c.anchor for c in cols]
    for j, c in enumerate(cols):
        lo, hi = bounds[j] + 1, c.anchor + 3
        parts = []
        for up in lines[max(0, i - 4): last + 1]:
            seg = [t.text for t in up.tokens if t.x0 >= lo - 2 and t.x1 <= hi + 2]
            if seg:
                parts.append(" ".join(seg))
        c.header = " | ".join(parts)
    basis_toks = [t for up in lines[max(0, i - 4): last + 1] for t in up.tokens if BASIS.match(t.text) and t.x0 > label_limit * 0.9]
    if basis_toks:
        for c in cols:
            cx = c.anchor - 15
            left = [b for b in basis_toks if (b.x0 + b.x1) / 2 <= c.anchor + 40]
            b = min(left or basis_toks, key=lambda b: abs((b.x0 + b.x1) / 2 - cx))
            c.basis = b.text.lower()
        _fix_basis_partition(cols, basis_toks)
    return last, cols


def normalise_basis(col_basis: str | None, title: str) -> str:
    """consolidated / separate / unspecified, from the column header word and the page title."""
    t = title.lower()
    if col_basis in ("group", "consolidated"):
        return "consolidated"
    if col_basis in ("company", "separate"):
        return "separate"
    has_c, has_s = "consolidated" in t, ("separate" in t or "company" in t)
    if has_c and not has_s:
        return "consolidated"
    if has_s and not has_c:
        return "separate"
    return "unspecified"


UNIT = re.compile(r"R\s?\W?\s?000|\bR\s?m\b|\bR\s?million\b|\bR\s?bn\b", re.I)


def detect_unit(text: str) -> str | None:
    """Most frequent currency unit marker in the header zone (R'000, Rm, Rbn)."""
    found = []
    for m in UNIT.finditer(text):
        u = m.group(0).lower().replace(" ", "")
        if "000" in u:
            found.append("R'000")
        elif "bn" in u:
            found.append("Rbn")
        elif u != "%":
            found.append("Rm")
    return max(set(found), key=found.count) if found else None


def _fix_basis_partition(cols: list[Column], basis_toks: list[Token]) -> None:
    """Basis headers sit centred over contiguous column blocks; split columns into equal blocks."""
    names = [b.text.lower() for b in sorted(basis_toks, key=lambda b: b.x0)]
    uniq = list(dict.fromkeys(names))
    if len(uniq) >= 2 and len(cols) % len(uniq) == 0:
        k = len(cols) // len(uniq)
        for j, c in enumerate(sorted(cols, key=lambda c: c.anchor)):
            c.basis = uniq[j // k]


def _group_numbers(run: list[Token]) -> list[list[Token]]:
    """Join digit groups separated by a space-sized gap into one number ('44 269 236')."""
    groups: list[list[Token]] = []
    for t in run:
        if groups:
            last = groups[-1][-1]
            digit_w = (last.x1 - last.x0) / max(1, len(last.text))
            gap = t.x0 - last.x1
            if (gap <= max(2.5, 0.9 * digit_w) and re.fullmatch(r"\d{3}\)?%?", t.text)
                    and not last.text.endswith(")") and last.text not in NIL and "." not in last.text):
                groups[-1].append(t)
                continue
        groups.append([t])
    return groups


def _assign(numbers: list[list[Token]], anchors: list[float]) -> list[int]:
    """Map numbers to columns: one-to-one by order when counts match, else nearest anchor in order."""
    if len(numbers) == len(anchors):
        return list(range(len(anchors)))
    out, last = [], -1
    for i, g in enumerate(numbers):
        x1 = g[-1].x1
        remaining = len(numbers) - i - 1
        choices = range(last + 1, len(anchors) - remaining) or range(len(anchors) - 1, len(anchors))
        j = min(choices, key=lambda k: abs(anchors[k] - x1))
        out.append(j)
        last = j
    return out


def is_spread(pdf_path, page_index: int) -> bool:
    """Landscape page holding two portrait pages side by side."""
    with pdfplumber.open(pdf_path) as pdf:
        p = pdf.pages[page_index]
        return float(p.width) > 1.2 * float(p.height)


def parse_page(pdf_path, page_index: int, label_limit: float | None = None, half: str | None = None) -> dict | None:
    """Parse one statement page. ``half`` ('left'/'right') parses one side of a two-page spread."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_index]
        if half:
            w, h = float(page.width), float(page.height)
            page = page.crop((0, 0, w / 2, h) if half == "left" else (w / 2, 0, w, h))
        x0 = float(page.bbox[0])
        lines = _chars_to_lines(page.chars)
        width = float(page.width)
    if label_limit is None:
        label_limit = x0 + width * 0.30
    hdr = _find_header(lines, label_limit)
    if not hdr:
        return None
    hi, cols = hdr
    anchors = [c.anchor for c in cols]
    min_anchor = min(anchors)
    col_gap = (max(anchors) - min_anchor) / max(1, len(anchors) - 1) if len(anchors) > 1 else 60
    note_zone = min_anchor - 0.6 * col_gap
    rows = []
    pending_label: list[str] = []
    section = None
    title = " ".join(ln.text for ln in lines[: max(1, hi - 1)])[:400]
    unit = detect_unit(" ".join(ln.text for ln in lines[max(0, hi - 6): hi + 3]))
    for ln in lines[hi + 1:]:
        toks = sorted(ln.tokens, key=lambda t: t.x0)
        # Values are the trailing run of numeric tokens right of the label area
        k = len(toks)
        while k > 0 and _is_num(toks[k - 1].text) and toks[k - 1].x0 > label_limit:
            k -= 1
        label_toks, run = toks[:k], toks[k:]
        numbers = _group_numbers(run)
        # Leading small integers left of the first column are note references
        note_toks = []
        while numbers and numbers[0][-1].x1 < note_zone and len(numbers) > 1:
            g = numbers.pop(0)
            txt = " ".join(t.text for t in g)
            if re.fullmatch(r"\d{1,2}(\.\d{1,2})*(,\s?\d{1,2}(\.\d{1,2})*)*", txt):
                note_toks += g
            else:
                label_toks += g
        label = " ".join(t.text for t in label_toks).strip()
        if not numbers:
            if label:
                pending_label.append(label)
            continue
        assign = _assign(numbers, anchors)
        full_label, headings = _resolve_label(pending_label, label)
        if headings:
            section = headings[-1]
        pending_label = []
        if not full_label:
            continue
        for g, j in zip(numbers, assign):
            raw = " ".join(t.text for t in g)
            v = 0.0 if raw in NIL else _to_float(raw.replace(" ", ""))
            c = cols[j]
            rows.append({
                "section": unicodedata.normalize("NFKC", section) if section else None,
                "line_label": re.sub(r"\s+", " ", unicodedata.normalize("NFKC", full_label)),
                "note": " ".join(t.text for t in note_toks) or None,
                "column_index": j,
                "column_header": c.header,
                "column_year": c.year,
                "basis_raw": c.basis,
                "basis": normalise_basis(c.basis, title),
                "unit": unit,
                "value": v,
                "value_raw": raw,
                "line_top": round(ln.top, 1),
            })
    return {"title": title, "columns": [c.__dict__ for c in cols], "rows": rows}
