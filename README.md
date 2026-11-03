# industry-analysis-ai

Extraction and reconciliation of financial data for four South African banks at the **bank-entity** level:
Capitec Bank Limited, Absa Bank Limited, Nedbank Limited and The Standard Bank of South Africa Limited (SBSA).

| Data | Source | Coverage |
|---|---|---|
| Balance sheet and off-balance sheet, monthly | SARB BA900 via the IFData API | Jan 2008 – latest month |
| Industry income statement, balance sheet, OBS, capital | SARB BA120 / BA100 / BA110 / BA700, **all-banks total only** | Jan 2008 – Jun 2025 |
| Statement of financial position, income statement | Bank annual financial statements and annual reports | FY2007 – FY2026, varies by bank |
| Risk-weighted assets and capital ratios | AFS capital notes, multi-year reviews, Pillar 3 (OV1) | FY2004 – FY2025, varies by bank |

## Run

```bash
uv sync
uv run python -m bankdata.pipeline           # full refresh; downloads are cached
uv run marimo run slides/bank_data_extraction.py   # slides (or: marimo edit, then the slides view)
```

Individual steps: `bankdata.sarb`, `bankdata.ba900`, `bankdata.reports`, `bankdata.extract`, `bankdata.build`.

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

## Outputs (`data/tidy`, Parquet and CSV)

| File | Grain | Key columns |
|---|---|---|
| `ba900` | bank × month × table × item × column | `table_number`, `item_number`, `item_description`, `column_number`, `column_description`, `value` (R'000) |
| `industry_ba100/110/120/700` | month × table × item × column | as above. BA120 values are monthly flows |
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

`basis`: `consolidated` is the bank and its subsidiaries; `separate` is the bank solo, which is comparable to BA900; `unspecified` means the page did not say.
Signs and units follow each bank's presentation. `value_rm` converts everything to R million.

## Known gaps

- DI900 (1993–2007) is out of scope; the series starts with BA900 in 2008.
- Capitec has no bank-level reports before FY2014, and its FY2026 Pillar 3 is not yet included.
- Nedbank Limited publishes consolidated statements only. FY2013 and earlier come from comparatives and the ten-year review.
- Absa has no RWA for 2010–2012 and no bank-level Pillar 3 OV1.
- SBSA 2010–2017 annual reports come from mirrors (sharedata, thevault), because standardbank.com no longer hosts them.
