import hashlib
import io
import json

import pandas as pd
import streamlit as st

from migration import (BOOL_COLUMNS, FIELD_COLUMNS, VERSION, build_bundle, csv_bytes,
                       detect_boolean_filters, expand_uploads, inventory, migrate, parse_dashboard, rules_from_rows)

st.set_page_config(page_title='Dashboard Migrator', page_icon='🔁', layout='wide')
st.title('Dashboard Migrator')
st.write('Move field references and automatically convert Yes/No and True/False filters across a batch of Sisense dashboards.')
st.caption('Files are processed in memory on the machine hosting this app. Download results to retain them. No Sisense connection is required.')

def records(frame):
    return frame.fillna('').astype(str).to_dict('records')

def frame(rows, columns):
    return pd.DataFrame(rows, columns=columns).fillna('')

if 'fields' not in st.session_state:
    st.session_state.fields = []
    st.session_state.booleans = []
    st.session_state.revision = 0

with st.sidebar:
    st.header('Mapping library')
    st.write('Load saved field mappings from JSON, CSV or the Fields sheet of an Excel workbook. Yes/No and True/False filters are detected automatically; the Booleans sheet is optional for custom labels.')
    library = st.file_uploader('Import mapping library', type=['json', 'csv', 'xlsx'])
    if st.button('Load library', disabled=library is None):
        try:
            if library.name.lower().endswith('.json'):
                config = json.loads(library.getvalue().decode('utf-8-sig'))
                if config.get('version') != VERSION:
                    raise ValueError('Unsupported configuration version.')
                fields, booleans = config.get('fields', []), config.get('booleans', [])
            elif library.name.lower().endswith('.csv'):
                df = pd.read_csv(io.BytesIO(library.getvalue()), dtype=str, keep_default_na=False)
                if not set(FIELD_COLUMNS).issubset(df.columns):
                    raise ValueError('CSV must have: ' + ', '.join(FIELD_COLUMNS))
                fields, booleans = records(df[FIELD_COLUMNS]), st.session_state.booleans
            else:
                sheets = pd.read_excel(io.BytesIO(library.getvalue()), sheet_name=None, dtype=str, keep_default_na=False)
                fields = records(sheets['Fields'][FIELD_COLUMNS])
                booleans = records(sheets['Booleans'][BOOL_COLUMNS]) if 'Booleans' in sheets else []
            rules_from_rows(fields, booleans)
            st.session_state.fields, st.session_state.booleans = fields, booleans
            st.session_state.revision += 1
            st.success('Library loaded.')
        except Exception as exc:
            st.error(f'Could not load library: {exc}')
    st.download_button('Field CSV template', csv_bytes([], FIELD_COLUMNS), 'field-mappings.csv', 'text/csv')
    workbook = io.BytesIO()
    with pd.ExcelWriter(workbook, engine='openpyxl') as writer:
        frame([], FIELD_COLUMNS).to_excel(writer, sheet_name='Fields', index=False)
        frame([], BOOL_COLUMNS).to_excel(writer, sheet_name='Booleans', index=False)
    st.download_button('Excel template (both sheets)', workbook.getvalue(), 'mapping-template.xlsx')
    st.divider()
    st.caption('Batch limits: 1,000 dashboards · 20 MB per dashboard · 150 MB uncompressed total.')

st.subheader('1. Dashboard files')
uploads = st.file_uploader('Upload .dash files or ZIP archives', type=['dash', 'zip'], accept_multiple_files=True)
files = []
try:
    files = expand_uploads([(u.name, u.getvalue()) for u in uploads])
    if files:
        st.caption(f'{len(files)} dashboard(s) loaded.')
except Exception as exc:
    st.error(str(exc))

with st.expander('Discover fields in uploaded dashboards'):
    if st.button('Scan field inventory', disabled=not files):
        inv = []
        for name, raw in files:
            try:
                inv.extend([{'Dashboard': name, **r} for r in inventory(parse_dashboard(raw))])
            except Exception as exc:
                st.warning(f'{name}: {exc}')
        st.session_state.inventory = inv
    if st.session_state.get('inventory'):
        st.dataframe(st.session_state.inventory, width='stretch')
        st.download_button('Download inventory', csv_bytes(st.session_state.inventory, ['Dashboard', 'Old table', 'Old column', 'References']), 'field-inventory.csv')

st.subheader('2. Field mappings')
st.caption('Exact Sisense table/column names. Every matching reference is migrated, including nested JAQL, filters, field IDs and formula dimension tokens. Custom display titles are retained.')
fields = records(st.data_editor(frame(st.session_state.fields, FIELD_COLUMNS), num_rows='dynamic', width='stretch', key=f'fields_{st.session_state.revision}'))
st.subheader('3. Automatic boolean conversion')
st.write('No boolean mapping list is needed. The app detects Yes/No, True/False strings and JSON true/false members in JAQL filters, including dashboard, widget and nested filters. Yes/True becomes "1", No/False becomes "0", and matching field references become text.')
st.caption('Detection is case-insensitive and works with a single selected value. Blank/null members are preserved. Unfamiliar members are flagged for review.')
with st.expander('Optional: custom boolean labels / existing overrides'):
    st.caption('Only needed for labels such as Enabled/Disabled. Use original table/column names. Saved libraries from the earlier version still work.')
    bools = records(st.data_editor(frame(st.session_state.booleans, BOOL_COLUMNS), num_rows='dynamic', width='stretch', key=f'bools_{st.session_state.revision}'))
config = {'version': VERSION, 'fields': fields, 'booleans': bools, 'automatic_boolean_filters': True}
st.download_button('Save mapping library', json.dumps(config, indent=2), 'mapping-config.json', 'application/json')

fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode() + b''.join(name.encode() + hashlib.sha256(raw).digest() for name, raw in files)).hexdigest()
st.subheader('4. Preview and download')
st.caption('IDs, layout and scripts are preserved. Unrecognised filter conditions and references in scripts are flagged for review. The app does not upload dashboards to Sisense.')
if st.button('Preview migration', type='primary', disabled=not files):
    try:
        mappings, boolean_rules = rules_from_rows(fields, bools)
        results = []
        detected = []
        coverage = {key: 0 for key in set(mappings) | set(boolean_rules)}
        progress = st.progress(0)
        for i, (name, raw) in enumerate(files):
            r = {'name': name, 'raw': raw, 'changes': [], 'issues': [], 'output': None, 'status': 'Invalid'}
            try:
                original = parse_dashboard(raw)
                detections = detect_boolean_filters(original)
                detected.extend([{'Dashboard': name, **d} for d in detections])
                for entry in inventory(original):
                    key = (entry['Old table'], entry['Old column'])
                    if key in coverage:
                        coverage[key] += entry['References']
                output, changes, issues = migrate(original, mappings, boolean_rules)
                r.update(output=output, changes=changes, issues=issues, detected_booleans=detections,
                         title=original.get('title', ''), status='Review' if issues else ('Ready' if changes else 'Unchanged'))
            except Exception as exc:
                r['issues'] = [{'path': '/', 'message': str(exc)}]
            results.append(r)
            progress.progress((i + 1) / len(files))
        st.session_state.preview = {'fingerprint': fingerprint, 'results': results, 'config': config,
                                    'detected_booleans': detected,
                                    'coverage': [{'Old table': k[0], 'Old column': k[1], 'Matched references': v} for k, v in sorted(coverage.items())]}
    except Exception as exc:
        st.error(str(exc))

preview = st.session_state.get('preview')
if preview and preview['fingerprint'] != fingerprint:
    st.info('Files or mappings have changed. Run Preview migration again for current results.')
elif preview:
    results = preview['results']
    detected = preview.get('detected_booleans', [])
    st.write(f'Automatically detected {len(set((d["Dashboard"], d["Old table"], d["Old column"]) for d in detected))} field(s) with Yes/No, True/False or JSON boolean filter members.')
    if detected:
        st.dataframe([{**d, 'Members': json.dumps(d['Members'], ensure_ascii=False)} for d in detected], width='stretch')
    else:
        st.caption('No Yes/No, True/False or JSON boolean filter members found. Fields with only All, blanks or 1/0 members are not inferred from their labels.')
    with st.expander('Mapping coverage'):
        st.dataframe(preview['coverage'], width='stretch')
        if any(r['Matched references'] == 0 for r in preview['coverage']):
            st.warning('Some rules matched no structured field references. Check names, or confirm these fields are absent from this batch.')
    counts = {s: sum(r['status'] == s for r in results) for s in ['Ready', 'Review', 'Unchanged', 'Invalid']}
    for col, (label, value) in zip(st.columns(4), counts.items()):
        col.metric(label, value)
    st.dataframe([{'Dashboard': r['name'], 'Status': r['status'], 'Changes': len(r['changes']), 'Review items': len(r['issues'])} for r in results], width='stretch')
    selected = st.selectbox('Inspect a dashboard', range(len(results)), format_func=lambda i: results[i]['name'])
    r = results[selected]
    changes_tab, issues_tab, json_tab = st.tabs(['Changes', 'Review items', 'Migrated JSON'])
    with changes_tab:
        st.dataframe([{**c, 'before': json.dumps(c['before']), 'after': json.dumps(c['after'])} for c in r['changes']], width='stretch')
    with issues_tab:
        if r['issues']:
            st.dataframe(r['issues'], width='stretch')
        else:
            st.success('No unsupported references detected. Validate dashboard results against the new datasource before production use.')
    with json_tab:
        if r['output'] is not None:
            st.code(json.dumps(r['output'], indent=2, ensure_ascii=False), language='json')
    include_review = st.checkbox('Include dashboards flagged Review in the migrated folder', value=False)
    st.download_button('Download migration ZIP', build_bundle(results, preview['config'], include_review), 'dashboard-migration.zip', 'application/zip', type='primary')
    st.caption('ZIP contains originals, eligible migrated dashboards, mapping configuration, report.json and changes.csv. Invalid dashboards are never included as migrated output.')
