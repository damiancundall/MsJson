# MsJson


A Streamlit app for batch editing exported Sisense `.dash` JSON files. Upload individual dashboards or ZIPs, optionally supply field mappings, preview, and download a ZIP of originals, migrated dashboards and reports. Fields with Yes/No, True/False strings or JSON boolean filter members are detected and converted automatically from JAQL. There is no Sisense upload or credential handling.

## Run on Windows

Install Python 3.11 or later, then open PowerShell in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Open the localhost URL shown by Streamlit. Use the sidebar to download templates, load CSV/Excel/JSON libraries and save updated rules with **Save mapping library**. Uploaded data stays in server memory during processing. If you host this app for others, uploads go to that host. Saved libraries are downloaded files; reload them on the next visit. No shared server-side mapping store is created.

## Deploy on Streamlit Community Cloud

In Streamlit's deployment form select repository `damiancundall/MsJson`, branch `main`, and main file path `app.py`. Select Python 3.12 in Advanced settings, then deploy. All required application files and `requirements.txt` are at the repository root. Upload dashboards and mapping libraries through the running app; do not commit dashboard exports or credentials to this repository.

## Field mapping

CSV headers or the Excel **Fields** sheet:

```csv
Old table,Old column,New table,New column
TEST_Fact_Form_CustomEventsCube,What type of fall?,Fall Data,What type of fall?
TEST_Fact_Form_CustomEventsCube,Where was the fall?,Fall Data,Where was the fall?
TEST_Fact_Form_CustomEventsCube,Wound Reason,Wound Data,Wound Reason
TEST_Fact_Form_CustomEventsCube,Wound Type,Wound Data,Wound Type
```

Match the exact names exposed by the Sisense datasource, including case and spaces. Mapping is by table AND column. Matching JAQL `table`, `column`, `tableTitle`, `dim`, associated `field.id` and `colorDim` are updated. `columnTitle` is renamed only if it equals the old column; custom labels are preserved. Exact bracketed dimension tokens inside `formula` properties are updated in a single pass. IDs, layouts, scripts and custom titles are preserved. Conflicting and chained/cyclic mappings are rejected so a rerun cannot silently move fields again.

## Boolean-to-string rules

**Boolean filter conversion is automatic. No boolean list, mapping library or field move is required.** Upload dashboards and click Preview migration. The preview lists each detected field, its dashboard, filter path and matching members; the ZIP report retains that evidence.

The app finds field-scoped JAQL filter members equal to Yes, No, True or False (case-insensitive), including single selections, include/exclude, nested and/or, widget filters and formula context filters. Actual JSON true/false members also qualify, without requiring a declared datatype. Yes, True and JSON true become **"1"**; No, False and JSON false become **"0"**. It uses the original table/column identity to set matching field references to `datatype: "text"`, including when the field is also moved. Detection is local to each dashboard so similarly named fields in other dashboards do not inherit an inferred rule. Dashboard titles, scripts, orphan member lists, All-only filters, bare 1/0 values and text such as "True story" are not evidence for automatic detection.

An optional Excel **Booleans** sheet (or the collapsed custom-label editor) supports custom labels and existing saved overrides:

```csv
Old table,Old column,Yes member,No member
TEST_Fact_Form_CustomEventsCube,Example boolean question,Yes,No
```

Identify overrides by ORIGINAL table/column, even when they also move. Overrides take precedence over automatic labels. Once a field is identified, its filter `members` are converted within include/exclude and nested and/or structures, as are scalar `equals` and `doesntEqual` conditions. Actual JSON true/false and numeric 1/0 also become strings. Existing string 1/0 are unchanged. Null and empty strings stay null/empty; they never become false. Nested JAQL objects use their own field identities. Saved libraries from the original version remain compatible.

Unsupported filter conditions, unfamiliar boolean members, boolean measures/formulas, inconsistent dimensions and references inside unsupported string properties trigger Review status. No guessing is done for those cases. Scripts are never executed. Other boolean-like fields are untouched.

## Output and review

The output ZIP contains:

- `originals/`: exact original bytes, including invalid dashboards.
- `migrated/`: valid transformed dashboards. Review dashboards are excluded unless explicitly selected; Invalid dashboards are always excluded.
- `mapping-config.json`: versioned rules used for this batch.
- `report.json`: per-dashboard status, issues, JSON paths and before/after changes.
- `changes.csv`: flattened change log with spreadsheet formula protection.

Numeric filename prefixes prevent collisions across duplicate names and ZIP folders. Files are never extracted from ZIP onto disk. Limits are 1,000 dashboards, 20 MB per dashboard and 150 MB total uncompressed input. Preview becomes stale when files or rules change and must be rerun before downloading current results. A field inventory can help identify mappings; it must be rescanned after replacing uploads.

The export cannot prove that target fields exist or that results and joins are correct. Deploy the new tables to the datasource first, then validate representative dashboard counts, filters, formulas and combined detail tables before importing at scale. This engine covers the observed export structures; custom plugin payloads may require additional handlers.

## Test

```powershell
py -m unittest -v test_migration.py
```

Reference for the Streamlit upload interface: https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader
