# industry-analysis-ai

Extraction and reconciliation of financial data for four South African banks at the **bank-entity** level:
Capitec Bank Limited, Absa Bank Limited, Nedbank Limited and The Standard Bank of South Africa Limited (SBSA).

| Data | Source | Coverage |
|---|---|---|
| Balance sheet and off-balance sheet, monthly | SARB BA900 via the IFData API | FY2014 – latest month |
| Industry income statement, balance sheet, OBS, capital | SARB BA120 / BA100 / BA110 / BA700, **all-banks total only** | Jan 2014 – Jun 2025 |
| Industry non-performing loans (impaired advances) | SARB BA200, **all-banks total only** | Jan 2014 – Jun 2025 |
| Asset mix by product, credit loss ratio, impairment allowance ratio | BA900 and the published income statements | FY2014 – latest |
| Statement of financial position, income statement | Bank annual financial statements and annual reports | FY2014 – FY2026 |
| Risk-weighted assets and capital ratios | AFS capital notes, multi-year reviews, Pillar 3 (OV1) | FY2014 – FY2025 |

## Run

```bash
uv sync
uv run python -m bankdata.pipeline           # full refresh; downloads are cached
uv run marimo run slides/bank_data_extraction.py   # slides (or: marimo edit, then the slides view)
```

Individual steps: `bankdata.sarb`, `bankdata.ba900`, `bankdata.reports`, `bankdata.extract`, `bankdata.build`, `bankdata.credit`.
`python -m bankdata.sarb BA200` downloads and tidies a single return.

## How it works

1. **`sarb.py`** downloads each BA900 return (four banks plus the industry total) and the industry BA1x0/BA700 returns.
   Period strings are passed exactly as `GetPeriods` lists them, because formats vary by year.
   Each return's XML is flattened so that one value becomes one row.
2. **`ba900.py`** parses the formula embedded in each subtotal description, for example
   "total of items 111, 117 … less item 194". It re-adds every subtotal for every bank, month and column, and tests total assets against equity plus liabilities.
3. **`reports.py`** downloads the documents in `data/reference/report_catalogue.csv`. It records the SHA-256 hash and page count, and detects the financial year from the text.
4. **`locate.py` / `statements.py` / `extract.py`** find statement pages and parse them with a column-aware PDF table parser.
   A table is accepted only if it validates: A = L + E for the balance sheet, interest and profit lines for the income statement, and a total line for RWA and OV1.
5. **`build.py`** produces the canonical datasets, the standardised RWA metrics and the reconciliations.
6. **`credit.py`** groups the BA900 asset items into products, and computes the credit loss ratio and impairment allowance ratio.
   The credit loss ratio is the income-statement credit impairment charge divided by average gross customer loans from BA900.
   Average loans use the month-ends in the year plus the prior year-end. The separate basis is preferred, and Nedbank's charge is consolidated.

## Outputs (`data/tidy`, Parquet and CSV)

| File | Grain | Key columns |
|---|---|---|
| `ba900` | bank × month × table × item × column | `table_number`, `item_number`, `item_description`, `column_number`, `column_description`, `value` (R'000) |
| `industry_ba100/110/120/200/700` | month × table × item × column | as above. BA120 values are monthly flows. BA200 ratios have `unit` = `%` |
| `ba900_internal_checks` | bank × month × subtotal × column | `reported`, `components_sum`, `difference`, `passed`, `check_type` |
| `ba900_headlines` | bank × month × metric | total assets (277), pre-netting assets (284), loans, deposits, equity, OBS items 285–295 |
| `report_statements` | document × table × column × line | every extracted value: `statement`, `table_kind` (primary/supplementary), `basis`, `column_year`, `line_label`, `note`, `value`, `unit`, `value_rm`, `page`, `path` |
| `financial_statements` | bank × basis × statement × FY × line | canonical primary statements as first published (`vintage_lag` = 0), otherwise from the next year's comparative |
| `rwa` / `rwa_ov1` | table line | capital-note rows; Pillar 3 OV1 rows tagged with `scope` (bank or group) and `measure` |
| `rwa_headline` | bank × FY × metric | `total_rwa`, risk-type RWA, CET1/Tier 1/total CAR %, qualifying capital, plus `source_table` and agreement count |
| `recon_ba900_vs_reports` | bank × basis × FY × metric | published vs BA900 item 277 and item 284 |
| `recon_rwa_sources` | bank × FY | total RWA, annual report vs Pillar 3 OV1 |
| `recon_vintage_consistency` | bank × basis × statement × FY × line | first-published value vs next year's comparative |
| `report_inventory`, `report_extraction_log` | document / candidate page | provenance and validation outcome of every candidate page |
| `ba900_products` | bank × month × product | 13 products from BA900 column 5, adding back to item 277; `is_customer_loan`, `is_fy_end`, shares |
| `industry_npl` | month × metric | BA200 impaired advances, specific impairments, NPL ratio and coverage |
| `credit_quality` | bank × FY | `impairment_charge_rm` with its source line and page, `avg_gross_customer_loans_rm`, `clr_pct`, `allowance_ratio_pct`, `industry_npl_ratio_pct` |

`basis`: `consolidated` is the bank and its subsidiaries; `separate` is the bank solo, which is comparable to BA900; `unspecified` means the page did not say.
Signs and units follow each bank's presentation. `value_rm` converts everything to R million.

## Analysis window

Only financial years from **FY2014** onwards are kept (`MIN_FISCAL_YEAR` in `src/bankdata/config.py`).
A financial year is named after the calendar year in which it ends, so Capitec's FY2014 is March 2013 to February 2014; the other banks' FY2014 is calendar 2014.
The cutoff is applied when downloading, when building the tidy BA900 and industry tables, when selecting reports, and to every column of the extracted statements, which also drops pre-FY2014 comparatives.

## Known gaps

- Capitec has no bank-level reports before FY2014, and its FY2026 Pillar 3 is not yet included.
- Nedbank Limited publishes consolidated statements only.
- Absa has no bank-level Pillar 3 OV1, so only total RWA is available.
- SBSA 2015–2017 annual reports come from mirrors (sharedata, thevault), because standardbank.com no longer hosts them.
- Bank-level NPL ratios are not available from SARB: BA900 has no NPL item and BA200 is published for the industry total only.
  The BA900 impairment allowance ratio (item 194 over gross customer loans) is used as the bank-level measure instead.
