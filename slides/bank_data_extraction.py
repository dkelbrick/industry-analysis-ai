import marimo

__generated_with = "0.25.2"
app = marimo.App(width="full", layout_file="layouts/bank_data_extraction.slides.json")


@app.cell(hide_code=True)
def _():
    import marimo as mo
    import polars as pl
    import altair as alt

    alt.data_transformers.disable_max_rows()
    TIDY = mo.notebook_dir().parent / "data" / "tidy"
    REF = mo.notebook_dir().parent / "data" / "reference"

    BANKS = ["capitec", "absa", "nedbank", "sbsa"]
    BANK_NAMES = {
        "capitec": "Capitec Bank Limited",
        "absa": "Absa Bank Limited",
        "nedbank": "Nedbank Limited",
        "sbsa": "The Standard Bank of South Africa Limited",
    }
    BANK_SHORT = {"capitec": "Capitec", "absa": "Absa", "nedbank": "Nedbank", "sbsa": "SBSA"}
    # Fixed categorical order (validated reference palette, slots 1-4): colour follows the bank, never its rank
    BANK_COLOURS = {"Capitec": "#2a78d6", "Absa": "#eb6834", "Nedbank": "#1baf7a", "SBSA": "#eda100"}
    bank_scale = alt.Scale(domain=list(BANK_COLOURS), range=list(BANK_COLOURS.values()))

    def short(df, col="bank"):
        return df.with_columns(pl.col(col).replace_strict(BANK_SHORT, default=pl.col(col)).alias("Bank"))

    def fmt(v, d=0):
        return "–" if v is None else f"{v:,.{d}f}".replace(",", " ")
    return (
        BANKS,
        BANK_NAMES,
        BANK_SHORT,
        REF,
        TIDY,
        alt,
        bank_scale,
        fmt,
        mo,
        pl,
        short,
    )


@app.cell(hide_code=True)
def _(REF, TIDY, pl):
    ba900 = pl.read_parquet(TIDY / "ba900.parquet")
    ba900_h = pl.read_parquet(TIDY / "ba900_headlines.parquet")
    ba900_chk = pl.read_parquet(TIDY / "ba900_internal_checks.parquet")
    ind = {rt: pl.read_parquet(TIDY / f"industry_{rt}.parquet") for rt in ["ba100", "ba110", "ba120", "ba700"]}
    inv = pl.read_csv(TIDY / "report_inventory.csv")
    cat = pl.read_csv(REF / "report_catalogue.csv")
    xlog = pl.read_csv(TIDY / "report_extraction_log.csv")
    rs = pl.read_parquet(TIDY / "report_statements.parquet")
    fs = pl.read_parquet(TIDY / "financial_statements.parquet")
    rwa_h = pl.read_parquet(TIDY / "rwa_headline.parquet")
    ov1 = pl.read_parquet(TIDY / "rwa_ov1.parquet")
    recon = pl.read_parquet(TIDY / "recon_ba900_vs_reports.parquet")
    recon_rwa = pl.read_parquet(TIDY / "recon_rwa_sources.parquet")
    vint = pl.read_parquet(TIDY / "recon_vintage_consistency.parquet")
    return (
        ba900,
        ba900_chk,
        ba900_h,
        cat,
        fs,
        ind,
        inv,
        ov1,
        recon,
        recon_rwa,
        rs,
        rwa_h,
        vint,
        xlog,
    )


@app.cell(hide_code=True)
def _(ba900, fmt, fs, inv, mo, rs, rwa_h):
    mo.md(
        f"""
# South African banks: financial data extraction

**Capitec Bank · Absa Bank · Nedbank · The Standard Bank of South Africa**, all at the bank-entity level

| What | Coverage | Values extracted |
|---|---|---|
| BA900 balance sheet and off-balance sheet, monthly (SARB API) | {ba900['month_end'].min():%b %Y} – {ba900['month_end'].max():%b %Y} | {fmt(ba900.height)} |
| Published statements: balance sheet and income statement | FY{fs['fiscal_year'].min()} – FY{fs['fiscal_year'].max()} | {fmt(rs.filter(rs['statement'].is_in(['sfp', 'income'])).height)} |
| Risk-weighted assets and capital: annual reports and Pillar 3 | FY{rwa_h['fiscal_year'].min()} – FY{rwa_h['fiscal_year'].max()} | {fmt(rs.filter(rs['statement'].is_in(['rwa', 'ov1'])).height)} |
| Source documents downloaded and parsed | {inv.height} PDFs | {fmt(int(inv['pages'].sum()))} pages |

The deck walks through the sources, the extraction method, the retrieved statements and the reconciliation of BA900 against the published financial statements.
"""
    )
    return


@app.cell(hide_code=True)
def _(ind, mo):
    _p = {k: (v["period"].min(), v["period"].max()) for k, v in ind.items()}
    mo.md(
        f"""
## Sources and what each one can provide

| Source | Entity level | Frequency | Used for |
|---|---|---|---|
| **SARB BA900** (IFData API) | Per bank (solo, as submitted to the SARB) | Monthly, Jan 2008 onwards | Granular balance sheet (tables 1–14), off-balance sheet (table 15), securitisation and repo detail (tables 17–19) |
| **SARB BA100 / BA110 / BA120 / BA700** | **Industry total only** ("TOTAL BANKS") | Monthly, {_p['ba120'][0]:%b %Y} – {_p['ba120'][1]:%b %Y} | Sector context only: income statement (BA120), capital and RWA (BA700) |
| **Bank annual financial statements / annual reports** | Bank entity: consolidated and separate | Annual | Statement of financial position, income statement, capital note |
| **Pillar 3 disclosures** | Bank entity and holding group | Annual (year end) | RWA by risk type (OV1), cross-check of total RWA |

**Finding on point 2 of the brief:** BA120 (income statement) and BA700 (capital adequacy) *do* exist in the SARB API, but they are published only as the all-banks total.
Per-bank income statements and RWA therefore come from the banks' own reports, as anticipated.
The four banks' BA900 institution codes are stable for the whole 2008–2026 history: Capitec 333107, Absa 34118, Nedbank 416088, SBSA 416061.
"""
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.vstack([
        mo.md("## Pipeline"),
        mo.mermaid(
            """
flowchart LR
  A[SARB IFData API] -->|GetPeriods / GetInstitutionData| B[(raw JSON + XML\\n1,960 returns)]
  B --> C[sarb.py\\nflatten XML to tidy]
  C --> D[ba900.parquet\\nindustry_ba1x0.parquet]
  D --> E[ba900.py\\nstated-formula checks\\nheadline items]
  F[report_catalogue.csv\\nbank-entity AFS, ARs, Pillar 3] --> G[reports.py\\ndownload + year detection]
  G --> H[locate.py\\nfind statement pages]
  H --> I[statements.py\\ncolumn-aware PDF table parser]
  I --> J[extract.py\\nvalidate: A = L + E, NII, RWA totals]
  J --> K[build.py\\ntidy statements, RWA, OV1]
  E --> L[reconciliation]
  K --> L
  L --> M[[Marimo slides]]
"""
        ),
        mo.md(
            "Every step caches its input, so a rerun is incremental. "
            "`uv run python -m bankdata.pipeline` rebuilds everything from the SARB API and the catalogue."
        ),
    ])
    return


@app.cell(hide_code=True)
def _(BANKS, BANK_SHORT, ba900, fs, mo, pl, rwa_h):
    def _span(df, ycol):
        return df.group_by("bank").agg(pl.col(ycol).min().alias("from"), pl.col(ycol).max().alias("to"), pl.col(ycol).n_unique().alias("n"))

    _b = ba900.filter(pl.col("bank") != "industry_total").group_by("bank").agg(
        pl.col("month_end").min().alias("from"), pl.col("month_end").max().alias("to"), pl.col("month_end").n_unique().alias("n"))
    _sfp = _span(fs.filter(pl.col("statement") == "sfp"), "fiscal_year")
    _inc = _span(fs.filter(pl.col("statement") == "income"), "fiscal_year")
    _rwa = _span(rwa_h.filter(pl.col("metric") == "total_rwa"), "fiscal_year")
    _ov = _span(rwa_h.filter(pl.col("source_table") == "pillar3_ov1"), "fiscal_year")

    def _cell(df, b, dates=False):
        r = df.filter(pl.col("bank") == b)
        if r.is_empty():
            return "–"
        r = r.row(0, named=True)
        if dates:
            return f"{r['from']:%b %Y} – {r['to']:%b %Y} ({r['n']} months)"
        return f"FY{r['from']} – FY{r['to']} ({r['n']} years)"

    _rows = [{"Bank": BANK_SHORT[b], "BA900 (monthly)": _cell(_b, b, True), "Balance sheet (reports)": _cell(_sfp, b),
              "Income statement": _cell(_inc, b), "Total RWA": _cell(_rwa, b), "RWA by risk type (OV1)": _cell(_ov, b)} for b in BANKS]
    mo.vstack([
        mo.md("## Coverage retrieved"),
        mo.ui.table(pl.DataFrame(_rows), selection=None, show_column_summaries=False),
        mo.md(
            "Report years are each bank's financial year. Capitec's year ends in February, so FY2026 means 28 Feb 2026; the other banks' years end in December. "
            "Where a year's own report was not available online, its figures come from the next year's comparative column, which is flagged by `vintage_lag` in the data."
        ),
    ])
    return


@app.cell(hide_code=True)
def _(ba900, mo, pl):
    _last = ba900.filter(pl.col("bank") == "capitec", pl.col("month_end") == ba900["month_end"].max())
    _t = (_last.group_by("table_number", "table_description")
          .agg(pl.col("item_number").min().alias("first item"), pl.col("item_number").max().alias("last item"),
               pl.col("item_number").n_unique().alias("items"), pl.col("column_number").n_unique().alias("columns"),
               pl.col("column_description").unique(maintain_order=True).str.join(" · ").alias("column headers"))
          .sort("table_number"))
    mo.vstack([
        mo.md(
            "## BA900: the form, kept at full granularity\n"
            "Each return is XML with *tables → rows (items) → columns*. Every value becomes one tidy row keyed by "
            "bank, month, table, item and column, with the form's own descriptions kept. "
            "Item numbers are stable from 2008 to 2026; only cosmetic description changes occur."
        ),
        mo.ui.table(_t, selection=None, page_size=20, show_column_summaries=False),
    ])
    return


@app.cell(hide_code=True)
def _(BANKS, BANK_SHORT, ba900_chk, fmt, mo, pl):
    _s = (ba900_chk.filter(pl.col("bank").is_in(BANKS))
          .group_by("bank", "check_type").agg(pl.len().alias("checks"), pl.col("passed").sum().alias("passed"))
          .with_columns((pl.col("passed") / pl.col("checks") * 100).round(2).alias("pass %"))
          .with_columns(pl.col("bank").replace_strict(BANK_SHORT)).sort("bank", "check_type"))
    _f = (ba900_chk.filter(pl.col("bank").is_in(BANKS), ~pl.col("passed"))
          .group_by("bank", "parent_item", "parent_description", "column_number")
          .agg(pl.len().alias("months failing"), pl.col("month_end").min().alias("from"), pl.col("month_end").max().alias("to"),
               pl.col("difference").abs().median().round(0).alias("median |diff| R'000"))
          .sort("months failing", descending=True).with_columns(pl.col("bank").replace_strict(BANK_SHORT)))
    mo.vstack([
        mo.md(
            f"## BA900 internal consistency\n"
            f"Most subtotal descriptions state their formula, for example *'TOTAL LIABILITIES (total of items 78 to 80)'*. "
            f"These formulas are parsed, including ranges and *less item N*, and every subtotal is re-added for every bank, month and column. "
            f"The balance identity, total assets = total equity and liabilities, is also tested. "
            f"Tolerance is R5k or 1e-6 relative, which covers rounding of R'000 figures. "
            f"In total {fmt(ba900_chk.filter(pl.col('bank').is_in(BANKS)).height)} checks were run."
        ),
        mo.hstack([
            mo.ui.table(_s, selection=None, show_column_summaries=False),
            mo.vstack([mo.md("**Exceptions**: genuine reporting quirks in the submitted returns, not parsing errors"),
                       mo.ui.table(_f, selection=None, page_size=8, show_column_summaries=False)]),
        ], widths=[1, 2]),
    ])
    return


@app.cell(hide_code=True)
def _(alt, ba900_h, bank_scale, mo, pl, short):
    _d = short(ba900_h.filter(pl.col("bank") != "industry_total",
                              pl.col("metric").is_in(["total_assets", "loans_and_advances_gross", "deposits", "total_equity"])))
    _d = _d.with_columns((pl.col("value") / 1e6).alias("Rbn"), pl.col("metric").str.replace_all("_", " ").str.to_titlecase().alias("Metric"))
    _base = alt.Chart(_d.to_pandas()).encode(
        x=alt.X("month_end:T", title=None),
        y=alt.Y("Rbn:Q", title="R billion"),
        color=alt.Color("Bank:N", scale=bank_scale, legend=alt.Legend(orient="top", title=None)),
        tooltip=["Bank", alt.Tooltip("month_end:T", title="Month"), "Metric", alt.Tooltip("Rbn:Q", format=",.1f")],
    )
    _chart = (_base.mark_line(strokeWidth=2)
              .properties(width=330, height=200)
              .facet(facet=alt.Facet("Metric:N", title=None), columns=2)
              .resolve_scale(y="independent"))
    mo.vstack([
        mo.md("## BA900 balance sheet, monthly since 2008\n"
              "Headline items from the full return: total assets (item 277), gross loans (items 110 + 194), deposits (item 1) and equity (item 96)."),
        mo.ui.altair_chart(_chart),
    ])
    return


@app.cell(hide_code=True)
def _(BANK_SHORT, ba900, mo):
    _months = sorted({d.strftime("%Y-%m-%d") for d in ba900["month_end"].unique().to_list()}, reverse=True)
    _latest = ba900.filter(ba900["month_end"] == ba900["month_end"].max())
    _opts = {f"{r[0]}: {r[1]}": r[0] for r in _latest.select("table_number", "table_description").unique(subset="table_number").sort("table_number").iter_rows()}
    ba_bank = mo.ui.dropdown({v: k for k, v in BANK_SHORT.items()} | {"Industry total": "industry_total"}, value="Capitec", label="Bank")
    ba_month = mo.ui.dropdown(_months, value=_months[0], label="Month end")
    ba_table = mo.ui.dropdown(_opts, value=next(iter(_opts)), label="Table")
    return ba_bank, ba_month, ba_table


@app.cell(hide_code=True)
def _(ba900, ba_bank, ba_month, ba_table, mo, pl):
    from datetime import date as _date
    _m = _date.fromisoformat(ba_month.value)
    _t = ba900.filter(pl.col("bank") == ba_bank.value, pl.col("month_end") == _m, pl.col("table_number") == ba_table.value)
    _w = (_t.with_columns(pl.concat_str(pl.col("column_number").cast(str), pl.col("column_description"), separator=". ").alias("col"))
          .pivot(on="col", index=["item_number", "item_description"], values="value", aggregate_function="first")
          .sort("item_number")) if not _t.is_empty() else pl.DataFrame({"message": ["No data for this selection"]})
    mo.vstack([
        mo.md("## BA900 explorer: any bank, month and table (R'000)"),
        mo.hstack([ba_bank, ba_month, ba_table], justify="start"),
        mo.ui.table(_w, selection=None, page_size=12, show_column_summaries=False),
    ])
    return


@app.cell(hide_code=True)
def _(alt, ba900, bank_scale, mo, pl, short):
    _obs = (ba900.filter(pl.col("table_number") == 15, pl.col("bank") != "industry_total", pl.col("item_number") != 294)
            .with_columns((pl.col("value").fill_null(0) / 1e6).alias("Rbn")))
    _tot = short(_obs.group_by("bank", "month_end").agg(pl.col("Rbn").sum()))
    _line = alt.Chart(_tot.to_pandas()).mark_line(strokeWidth=2).encode(
        x=alt.X("month_end:T", title=None), y=alt.Y("Rbn:Q", title="R billion"),
        color=alt.Color("Bank:N", scale=bank_scale, legend=alt.Legend(orient="top", title=None)),
        tooltip=["Bank", alt.Tooltip("month_end:T", title="Month"), alt.Tooltip("Rbn:Q", format=",.1f")],
    ).properties(width=420, height=260, title="Total off-balance-sheet exposures, table 15 (items 285–293, 295)")
    _latest = short(_obs.filter(pl.col("month_end") == _obs["month_end"].max()))
    _w = (_latest.pivot(on="Bank", index=["item_number", "item_description"], values="Rbn", aggregate_function="first")
          .sort("item_number").with_columns(pl.exclude("item_number", "item_description").round(1)))
    mo.vstack([
        mo.md(f"## Off-balance sheet (BA900 table 15)\nComposition at {_obs['month_end'].max():%b %Y}, in R billion. "
              "Uncommitted undrawn facilities (item 294) are a 'of which' line, so they are excluded from the total."),
        mo.hstack([mo.ui.altair_chart(_line), mo.ui.table(_w, selection=None, show_column_summaries=False)], widths=[1, 1]),
    ])
    return


@app.cell(hide_code=True)
def _(alt, ba900_h, ind, mo, pl, rwa_h):
    _b120 = ind["ba120"].filter(pl.col("column_number") == 3, pl.col("item_number").is_in([28, 64, 87]))
    _b120 = (_b120.with_columns(pl.col("item_number").replace_strict({28: "Net interest income", 64: "Non-interest revenue", 87: "Profit for the period"}).alias("Line"))
             .sort("month_end").with_columns((pl.col("value").rolling_sum(12).over("Line") / 1e6).alias("Rbn (rolling 12m)")))
    _c1 = alt.Chart(_b120.drop_nulls("Rbn (rolling 12m)").to_pandas()).mark_line(strokeWidth=2).encode(
        x=alt.X("month_end:T", title=None), y=alt.Y("Rbn (rolling 12m):Q", title="R billion, rolling 12 months"),
        color=alt.Color("Line:N", scale=alt.Scale(range=["#2a78d6", "#eb6834", "#1baf7a"]), legend=alt.Legend(orient="top", title=None)),
        tooltip=["Line", alt.Tooltip("month_end:T", title="Month"), alt.Tooltip("Rbn (rolling 12m):Q", format=",.1f")],
    ).properties(width=420, height=240, title="BA120 industry income statement (all banks)")
    _b700 = ind["ba700"].filter(pl.col("item_number") == 6).select("month_end", (pl.col("value") / 1e6).alias("Industry RWA"))
    _four = (rwa_h.filter(pl.col("metric") == "total_rwa").group_by("fiscal_year").agg((pl.col("value").sum() / 1e3).alias("Four banks"), pl.len().alias("n"))
             .filter(pl.col("n") == 4))
    _ind_ye = _b700.filter(pl.col("month_end").dt.month() == 12).with_columns(pl.col("month_end").dt.year().alias("fiscal_year"))
    _sh = _four.join(_ind_ye, on="fiscal_year").with_columns((pl.col("Four banks") / pl.col("Industry RWA") * 100).round(1).alias("Share %")).sort("fiscal_year")
    mo.vstack([
        mo.md("## Industry returns: context only, since they are not per bank\n"
              "BA120 values are monthly flows, so they are summed over rolling 12 months. "
              "BA700 *Total risk weighted exposure* is compared with the four banks' year-end RWA. "
              "Capitec's February year end is mixed with December here, so treat the share as indicative."),
        mo.hstack([mo.ui.altair_chart(_c1),
                   mo.vstack([mo.md("**Four banks' RWA as a share of BA700 industry RWA (R billion, December)**"),
                              mo.ui.table(_sh.select("fiscal_year", pl.col("Four banks").round(0), pl.col("Industry RWA").round(0), "Share %"),
                                          selection=None, show_column_summaries=False)])], widths=[3, 2]),
    ])
    return


@app.cell(hide_code=True)
def _(BANK_SHORT, cat, inv, mo, pl):
    _c = (inv.with_columns(pl.coalesce("detected_fy", pl.col("fy_hint").cast(pl.Int64)).alias("FY"))
          .group_by("bank", "doc_type").agg(pl.len().alias("documents"), pl.col("FY").min().alias("from"), pl.col("FY").max().alias("to"),
                                            pl.col("pages").sum().alias("pages"), pl.col("source").unique().sort().str.join(", ").alias("sources"))
          .with_columns(pl.col("bank").replace_strict(BANK_SHORT)).sort("bank", "doc_type"))
    _x = cat.filter(pl.col("include") == False).select("bank", "fy_hint", "note")  # noqa: E712
    mo.vstack([
        mo.md("## Published reports retrieved\n"
              "The catalogue in `data/reference/report_catalogue.csv` lists every document with its URL. "
              "Official bank sites were used throughout, except for SBSA 2010–2017, which comes from the sharedata and thevault mirrors because standardbank.com no longer hosts those years. "
              "Each PDF's SHA-256 hash and detected financial year are recorded in `report_inventory.csv`."),
        mo.hstack([mo.ui.table(_c, selection=None, show_column_summaries=False),
                   mo.vstack([mo.md("**Excluded after checking the content** (wrong entity or duplicate)"),
                              mo.ui.table(_x, selection=None, show_column_summaries=False)])], widths=[3, 2]),
    ])
    return


@app.cell(hide_code=True)
def _(mo, pl, xlog):
    _ok = xlog.filter(pl.col("ok"))
    _s = (_ok.group_by("statement", "reason").agg(pl.len().alias("tables")).sort("statement", "tables", descending=[False, True]))
    mo.vstack([
        mo.md(
            """
## How the PDF statements are extracted

1. **Locate pages.** Each page's text is squashed (lower-case, no whitespace) so letter-spaced PDFs such as Capitec's still match. Pages carrying a statement title and its key line are candidates, with heading matches ranked first.
2. **Rebuild lines from characters.** Space glyphs and character gaps rebuild words, and NFKC normalisation turns ligatures like "ﬁ" into plain letters.
3. **Find the column header.** This is the line of year tokens, plus any staggered or sub-header lines, that best *aligns with the right edges of the numbers below*. The words GROUP/COMPANY or the page title give the basis (consolidated or separate). The unit (R'000 or Rm) comes from the header zone.
4. **Assemble numbers.** Digit groups separated by a space-sized gap join into one value, so "44 269 236" reads as 44,269,236. Parentheses are negatives, dashes are nil, and decimal commas such as "3,90" are handled. Values are then assigned to columns in order. Two-page spreads are split into left and right halves.
5. **Validate before accepting.** A balance sheet must satisfy A = L + E. An income statement needs an interest line and a profit line. An RWA table needs a total line. Multi-year reviews and annexures are kept separately as *supplementary*.
"""
        ),
        mo.md("**Validated tables by rule**"),
        mo.ui.table(_s, selection=None, show_column_summaries=False, page_size=8),
    ])
    return


@app.cell(hide_code=True)
def _(BANK_SHORT, fs, mo):
    st_bank = mo.ui.dropdown({v: k for k, v in BANK_SHORT.items()}, value="Capitec", label="Bank")
    st_stmt = mo.ui.dropdown({"Statement of financial position": "sfp", "Income statement": "income"}, value="Income statement", label="Statement")
    st_basis = mo.ui.dropdown(["separate", "consolidated", "unspecified"], value="separate", label="Basis")
    st_year = mo.ui.dropdown([str(y) for y in sorted(fs["fiscal_year"].unique().to_list(), reverse=True)], value="2025", label="Financial year")
    return st_basis, st_bank, st_stmt, st_year


@app.cell(hide_code=True)
def _(fs, mo, pl, st_bank, st_basis, st_stmt, st_year):
    _d = fs.filter(pl.col("bank") == st_bank.value, pl.col("statement") == st_stmt.value,
                   pl.col("basis") == st_basis.value, pl.col("fiscal_year") == int(st_year.value)).sort("line_order")
    _avail = (fs.filter(pl.col("bank") == st_bank.value, pl.col("statement") == st_stmt.value)
              .group_by("basis").agg(pl.col("fiscal_year").min().alias("from"), pl.col("fiscal_year").max().alias("to")))
    if _d.is_empty():
        _body = mo.md("No statement for this combination. Available: " +
                      ", ".join(f"**{r['basis']}** FY{r['from']}–FY{r['to']}" for r in _avail.iter_rows(named=True)))
    else:
        _r0 = _d.row(0, named=True)
        _body = mo.vstack([
            mo.md(f"Source: `{_r0['path']}`, page {_r0['page']} · report FY{_r0['report_year']} "
                  f"({'as first published' if _r0['vintage_lag'] == 0 else 'comparative column of the following year'}) · unit {_r0['unit']}"),
            mo.ui.table(_d.select("section", "line_label", "note", "value"), selection=None, page_size=15, show_column_summaries=False),
        ])
    mo.vstack([mo.md("## Retrieved statements: explorer"), mo.hstack([st_bank, st_stmt, st_basis, st_year], justify="start"), _body])
    return


@app.cell(hide_code=True)
def _(fs, pl):
    # Standardised headline lines from the canonical statements, solo basis first (comparable to BA900)
    _basis_pref = {"separate": 0, "unspecified": 1, "consolidated": 2}
    _f = fs.with_columns(pl.col("basis").replace_strict(_basis_pref).alias("bp"))
    _pick = _f.group_by("bank", "statement", "fiscal_year").agg(pl.col("bp").min())
    solo = _f.join(_pick, on=["bank", "statement", "fiscal_year", "bp"])
    # patterns in priority order: e.g. Capitec's 'Total profit for the year' (continued + discontinued) beats
    # 'Profit for the year - continued operations'
    _pats = {
        "Net interest income": [r"^netinterestincome$"],
        "Profit for the year": [r"^totalprofitfortheyear$", r"^(profit|loss)forthe(year|reportingperiod)$", r"^profitfortheyear"],
        "Total assets": [r"^totalassets$"],
        "Total equity": [r"^totalequity$"],
    }
    _rows = []
    for _name, _plist in _pats.items():
        _cand = pl.concat([solo.filter(pl.col("line_key").str.contains(_p)).with_columns(pl.lit(_k).alias("prio"))
                           for _k, _p in enumerate(_plist)])
        _m = (_cand.sort("prio", "line_order").unique(subset=["bank", "fiscal_year"], keep="first")
              .drop("prio").with_columns(pl.lit(_name).alias("Line")))
        _rows.append(_m)
    headline_fs = pl.concat(_rows).with_columns((pl.col("value_rm") / 1e3).alias("Rbn"))
    return (headline_fs,)


@app.cell(hide_code=True)
def _(alt, bank_scale, headline_fs, mo, pl, short):
    _d = short(headline_fs.filter(pl.col("Line").is_in(["Net interest income", "Profit for the year"])))
    _c = (alt.Chart(_d.to_pandas()).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=40)).encode(
        x=alt.X("fiscal_year:O", title="Financial year"), y=alt.Y("Rbn:Q", title="R billion"),
        color=alt.Color("Bank:N", scale=bank_scale, legend=alt.Legend(orient="top", title=None)),
        tooltip=["Bank", "fiscal_year", "Line", "basis", alt.Tooltip("Rbn:Q", format=",.2f"), "report_year"])
        .properties(width=430, height=260).facet(column=alt.Column("Line:N", title=None)).resolve_scale(y="independent"))
    mo.vstack([
        mo.md("## Income statement trends from the published statements\n"
              "Solo or separate basis where published, otherwise the consolidated bank figure (Nedbank). "
              "Capitec reported *net lending income* rather than net interest income from FY2017 to FY2023, so its NII line has gaps in those years. "
              "The full statements keep every line."),
        mo.ui.altair_chart(_c),
    ])
    return


@app.cell(hide_code=True)
def _(alt, ba900_h, bank_scale, mo, ov1, pl, rwa_h, short):
    _r = short(rwa_h.filter(pl.col("metric") == "total_rwa").with_columns((pl.col("value") / 1e3).alias("Rbn")))
    _c1 = alt.Chart(_r.to_pandas()).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=40)).encode(
        x=alt.X("fiscal_year:O", title="Financial year"), y=alt.Y("Rbn:Q", title="R billion"),
        color=alt.Color("Bank:N", scale=bank_scale, legend=alt.Legend(orient="top", title=None)),
        tooltip=["Bank", "fiscal_year", alt.Tooltip("Rbn:Q", format=",.1f"), "line_label", "doc_type", "page"],
    ).properties(width=420, height=250, title="Total RWA, bank entity")
    _ta = ba900_h.filter(pl.col("metric") == "total_assets").select("bank", "month_end", (pl.col("value") / 1e6).alias("ta_rbn"))
    _den = short(_r.join(_ta, left_on=["bank", "period_end"], right_on=["bank", "month_end"], how="inner")
                 .with_columns((pl.col("Rbn") / pl.col("ta_rbn") * 100).alias("RWA density %")))
    _c2 = alt.Chart(_den.to_pandas()).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=40)).encode(
        x=alt.X("fiscal_year:O", title="Financial year"), y=alt.Y("RWA density %:Q", title="RWA / BA900 total assets, %"),
        color=alt.Color("Bank:N", scale=bank_scale, legend=None),
        tooltip=["Bank", "fiscal_year", alt.Tooltip("RWA density %:Q", format=".1f")],
    ).properties(width=420, height=250, title="RWA density")
    mo.vstack([
        mo.md("## Risk-weighted assets\n"
              "Total RWA comes from each bank's capital adequacy note, or its multi-year review for earlier years. "
              "Where several reports state a year's figure, the value most reports agree on is kept. Capitec's bank-entity columns are preferred over Capitec Bank Holdings. "
              "Density divides RWA by BA900 total assets at the same month end."),
        mo.hstack([mo.ui.altair_chart(_c1), mo.ui.altair_chart(_c2)]),
    ])
    return


@app.cell(hide_code=True)
def _(BANK_SHORT, alt, mo, pl, rwa_h):
    _comp = ["credit_risk_rwa", "counterparty_credit_risk_rwa", "cva_rwa", "equity_risk_rwa", "market_risk_rwa",
             "operational_risk_rwa", "threshold_deductions_rwa", "other_assets_rwa"]
    _o = rwa_h.filter(pl.col("source_table") == "pillar3_ov1", pl.col("metric").is_in(_comp))
    _o = (_o.with_columns(pl.col("bank").replace_strict(BANK_SHORT).alias("Bank"),
                          pl.col("metric").str.replace("_rwa", "").str.replace_all("_", " ").alias("Risk type"),
                          (pl.col("value") / 1e3).alias("Rbn")))
    _order = [c.replace("_rwa", "").replace("_", " ") for c in _comp]
    _c = alt.Chart(_o.to_pandas()).mark_bar(cornerRadiusEnd=4).encode(
        y=alt.Y("Risk type:N", sort=_order, title=None), x=alt.X("Rbn:Q", title="R billion"),
        color=alt.value("#2a78d6"),
        tooltip=["Bank", "fiscal_year", "Risk type", alt.Tooltip("Rbn:Q", format=",.1f")],
    ).properties(width=200, height=200).facet(row=alt.Row("Bank:N", title=None), column=alt.Column("fiscal_year:O", title=None))
    mo.vstack([
        mo.md("## RWA by risk type: Pillar 3 OV1, bank entity only\n"
              "Available for Capitec Bank (FY2022–FY2025), Nedbank Limited (2016–2018, 2023–2025) and SBSA (2022–2025). "
              "Absa Bank's year-end Pillar 3 report could not be located, and its AFS gives total RWA only."),
        mo.ui.altair_chart(_c),
    ])
    return


@app.cell(hide_code=True)
def _(BANK_SHORT, alt, mo, pl, recon):
    _pref = {"separate": 0, "unspecified": 1, "consolidated": 2}
    _r = recon.filter(pl.col("metric") == "total_assets", pl.col("ba900_rm").is_not_null()).with_columns(pl.col("basis").replace_strict(_pref).alias("bp"))
    _r = _r.join(_r.group_by("bank", "fiscal_year").agg(pl.col("bp").min()), on=["bank", "fiscal_year", "bp"])
    _r = _r.with_columns(pl.col("bank").replace_strict(BANK_SHORT).alias("Bank"),
                         ((pl.col("report_rm") / pl.col("ba900_prior_to_netting_rm") - 1) * 100).alias("diff vs pre-netting %"))
    _zero = alt.Chart().mark_rule(color="#8a8984", strokeDash=[3, 3]).encode(y=alt.datum(0))
    _c = alt.layer(
        alt.Chart().mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=40)).encode(
            x=alt.X("fiscal_year:O", title="Financial year"), y=alt.Y("diff_pct:Q", title="Published minus BA900, % of BA900"),
            tooltip=["Bank", "fiscal_year", "basis", alt.Tooltip("report_rm:Q", format=",.0f", title="Published Rm"),
                     alt.Tooltip("ba900_rm:Q", format=",.0f", title="BA900 Rm"), alt.Tooltip("diff_pct:Q", format=".2f")]),
        _zero, data=_r.to_pandas(),
    ).properties(width=250, height=200).facet(facet=alt.Facet("Bank:N", title=None), columns=4)
    mo.vstack([
        mo.md("## Reconciliation 1: BA900 total assets vs published balance sheet\n"
              "Published total assets on the *separate* (solo) basis are compared with BA900 item 277 at the same month end. "
              "Nedbank publishes only consolidated statements, so its gap includes subsidiaries."),
        mo.ui.altair_chart(_c),
        mo.md(
            """
* **Capitec** ties to BA900 within about R1m in every year from FY2013. This independently confirms both the SARB data and the PDF parser.
* **Absa** is within ±0.3% since 2016. Before 2016 the gap is 0.5–2.5%, and BA900's *total assets prior to netting* (item 284) closes most of it.
* **SBSA** is 3–6% above BA900. Netting (item 284 vs 277) explains about 2 points, and the rest is IFRS vs regulatory scope.
* **Nedbank** consolidated is 1–6% above solo BA900, as expected from its subsidiaries.
"""
        ),
    ])
    return


@app.cell(hide_code=True)
def _(BANK_SHORT, mo, pl, recon):
    _w = (recon.filter(pl.col("ba900_rm").is_not_null())
          .with_columns(pl.col("bank").replace_strict(BANK_SHORT).alias("Bank"), pl.col("diff_pct").round(2), pl.col("report_rm").round(0), pl.col("ba900_rm").round(0))
          .select("Bank", "basis", "fiscal_year", "month_end", "metric", "report_line", "report_rm", "ba900_rm", "diff_pct", "page")
          .sort("Bank", "metric", "basis", "fiscal_year"))
    mo.vstack([mo.md("## Reconciliation 1: detail, all bases and lines (R million)\n"
                     "Total assets, liabilities, equity and equity plus liabilities. Filter by column to drill in."),
               mo.ui.table(_w, selection=None, page_size=12)])
    return


@app.cell(hide_code=True)
def _(BANK_SHORT, mo, pl, recon_rwa, vint):
    _rw = (recon_rwa.filter(pl.col("pillar3_ov1_rm").is_not_null())
           .with_columns(pl.col("bank").replace_strict(BANK_SHORT).alias("Bank"), pl.col("diff_pct").round(2))
           .select("Bank", "fiscal_year", "annual_report_rm", "pillar3_ov1_rm", "diff_rm", "diff_pct"))
    _v = (vint.group_by("bank", "statement").agg(pl.len().alias("lines compared"), (pl.col("identical").mean() * 100).round(1).alias("identical %"),
                                                (pl.col("diff").abs() / pl.col("value_original").abs() > 0.01).sum().alias("lines moved >1%"))
          .with_columns(pl.col("bank").replace_strict(BANK_SHORT)).sort("bank", "statement"))
    mo.vstack([
        mo.md("## Reconciliation 2: RWA sources and restatements"),
        mo.hstack([
            mo.vstack([mo.md("**Total RWA: capital note in the annual statements vs Pillar 3 OV1, bank entity (Rm)**"),
                       mo.ui.table(_rw, selection=None, show_column_summaries=False),
                       mo.md("These match exactly for Capitec, SBSA and Nedbank 2016–2018. "
                             "From 2023, Nedbank Limited's AFS figure runs 6–9% above the Nedbank Limited OV1, which points to different consolidation scopes in the two disclosures.")]),
            mo.vstack([mo.md("**Each line as first published vs the comparative in the next year's report**"),
                       mo.ui.table(_v, selection=None, show_column_summaries=False),
                       mo.md("Differences are restatements, such as IFRS 9 and IFRS 17 adoption or reclassifications. "
                             "Canonical data keeps the figures as first published. All vintages remain in `report_statements`.")]),
        ]),
    ])
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        """
## Gaps, caveats and next steps

**Gaps that could not be filled from public sources**
- BA900 starts in January 2008. The predecessor DI900 (1993–2007) is out of scope by choice.
- Capitec: there are no bank-level reports before FY2014, so FY2013 comes from comparatives. FY2026 RWA awaits the February 2026 Pillar 3 report.
- Nedbank Limited: FY2013 and earlier come only from comparatives and the ten-year review (supplementary tables). Separate (solo) statements are not published in its AFS.
- Absa: RWA for 2010–2012 is missing, and there is no year-end Pillar 3 OV1 for Absa Bank.
- SBSA: the RWA risk-type split is available only from 2022 (OV1) and from the annual reports' Basel tables.

**Caveats**
- Sign conventions follow each bank's presentation. Nedbank, for example, shows expenses as positive numbers. Values are kept as printed, and `value_rm` only rescales units.
- `unspecified` basis means the page did not say whether it was consolidated or separate. Capitec FY2013–2018 had no subsidiaries, so it is effectively solo.
- Absa 2024: the capital note prints qualifying capital as *R199 683m*, which is inconsistent with the stated 17.1% ratio on R699bn RWA. This is likely a typo in the source, and the value is kept as printed.

**Next steps**: map published line items to BA900 items for loans, deposits and investments; add interim (June/August) reports; harmonise income statement lines across banks into a common chart of accounts.
"""
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        """
## Outputs (data/tidy, Parquet + CSV)

| File | Grain | Notes |
|---|---|---|
| `ba900` | bank × month × table × item × column | every BA900 value (R'000), including the industry total |
| `industry_ba100/110/120/700` | month × table × item × column | all-banks totals only |
| `ba900_internal_checks` | bank × month × subtotal × column | stated-formula and balance-identity checks |
| `ba900_headlines` | bank × month × metric | total assets, loans, deposits, equity, off-balance-sheet items |
| `report_statements` | document × table × column × line | every extracted value, all vintages, primary and supplementary |
| `financial_statements` | bank × basis × statement × FY × line | canonical statements as first published |
| `rwa`, `rwa_ov1`, `rwa_headline` | bank × FY × line / metric | capital notes, Pillar 3 OV1 by scope, standardised metrics |
| `recon_ba900_vs_reports` | bank × basis × FY × metric | published vs BA900 (item 277 and pre-netting item 284) |
| `recon_rwa_sources`, `recon_vintage_consistency` | bank × FY (× line) | RWA source agreement; first-published vs comparative |
| `report_inventory`, `report_extraction_log` | document / candidate page | provenance: URL, hash, pages, validation outcome |
"""
    )
    return


if __name__ == "__main__":
    app.run()
