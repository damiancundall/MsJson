"""Pure JSON transformations: no Sisense connection, credentials or external writes."""
from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import PurePosixPath
import zipfile

FIELD_COLUMNS = ['Old table', 'Old column', 'New table', 'New column']
BOOL_COLUMNS = ['Old table', 'Old column', 'Yes member', 'No member']
BOOLEAN_STRINGS = {'yes': '1', 'no': '0', 'true': '1', 'false': '0'}
VERSION = 1
MAX_FILE = 20 * 1024 * 1024
MAX_TOTAL = 150 * 1024 * 1024
MAX_FILES = 1000


def parse_dashboard(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON property: {key}')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError(f'Invalid JSON number: {value}')
    if len(raw) > MAX_FILE:
        raise ValueError('Dashboard exceeds 20 MB.')
    data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(data, dict) or not isinstance(data.get('widgets'), list):
        raise ValueError('Expected a dashboard object with a widgets array.')
    return data


def rules_from_rows(fields, booleans):
    mappings, bools = {}, {}
    for rows, columns, target in [(fields, FIELD_COLUMNS, mappings), (booleans, BOOL_COLUMNS, bools)]:
        for n, row in enumerate(rows, 1):
            vals = [str(row.get(k, '') or '') for k in columns]
            if not any(v.strip() for v in vals):
                continue
            if not all(v.strip() for v in vals):
                raise ValueError(f'Incomplete row {n} in {"field" if target is mappings else "boolean"} mappings.')
            key, value = tuple(vals[:2]), tuple(vals[2:])
            if key in target and target[key] != value:
                raise ValueError(f'Conflicting mappings for {key}.')
            if target is bools and value[0].casefold() == value[1].casefold():
                raise ValueError(f'Yes and No members must differ for {key}.')
            target[key] = value
    # Overlapping source/target sets make a second run ambiguous. Require a direct final mapping.
    for source, dest in mappings.items():
        if source != dest and dest in mappings and mappings[dest] != dest:
            raise ValueError(f'Chained/cyclic mapping {source} → {dest}. Map directly to final destinations.')
    for source in bools:
        dest = mappings.get(source, source)
        if dest != source and dest in bools:
            raise ValueError(f'Boolean rules must identify original fields only: {source} and {dest}.')
    return mappings, bools


def dimension(key):
    return f'[{key[0]}.{key[1]}]'


def inventory(data):
    result = {}
    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get('table'), str) and isinstance(node.get('column'), str):
                key = (node['table'], node['column'])
                result[key] = result.get(key, 0) + 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(data)
    return [{'Old table': k[0], 'Old column': k[1], 'References': v} for k, v in sorted(result.items())]


def detect_boolean_filters(data):
    """Find evidence inside field-scoped filters, never in labels or scripts.

    Detection stays local to each dashboard. A matching table/column in another
    dashboard may belong to a different datasource and must not inherit a rule.
    """
    detections = []

    def members(node, path):
        if isinstance(node, dict):
            if 'jaql' in node or ('table' in node and 'column' in node):
                return
            for key, value in node.items():
                p = path + '/' + str(key)
                if key == 'members' and isinstance(value, list):
                    yield p, value
                elif key in ('equals', 'doesntEqual') and not isinstance(value, (dict, list)):
                    yield p, [value]
                elif isinstance(value, (dict, list)):
                    yield from members(value, p)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from members(value, path + '/' + str(i))

    def inspect(jaql, filt, path):
        table, column = jaql.get('table'), jaql.get('column')
        if not isinstance(table, str) or not isinstance(column, str):
            return
        for p, values in members(filt, path):
            evidence = [v for v in values if
                        (isinstance(v, str) and v.casefold() in BOOLEAN_STRINGS) or
                        isinstance(v, bool)]
            if evidence:
                detections.append({'Old table': table, 'Old column': column,
                                   'Filter path': p, 'Members': evidence})

    def walk(node, path=''):
        if isinstance(node, dict):
            if 'filter' in node:
                if 'table' in node and 'column' in node:
                    inspect(node, node['filter'], path + '/filter')
                elif isinstance(node.get('jaql'), dict):
                    inspect(node['jaql'], node['filter'], path + '/filter')
            for key, value in node.items():
                walk(value, path + '/' + str(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, path + '/' + str(i))

    walk(data)
    return detections


def migrate(data, mappings, bools=None, auto_booleans=True):
    """Decisions always use original input, never already transformed values."""
    inferred = {(r['Old table'], r['Old column']): ('Yes', 'No')
                for r in detect_boolean_filters(data)} if auto_booleans else {}
    bools = {**inferred, **(bools or {})}
    out = copy.deepcopy(data)
    changes, issues = [], []
    tokens = {dimension(k): dimension(v) for k, v in mappings.items() if k != v}
    bool_tokens = {dimension(k): k for k in bools}

    def issue(path, message):
        entry = {'path': path or '/', 'message': message}
        if entry not in issues:
            issues.append(entry)

    def set_value(node, key, value, path, reason):
        if node.get(key) != value:
            changes.append({'path': path + '/' + str(key), 'before': node.get(key), 'after': value, 'reason': reason})
            node[key] = value

    def token_replace(value):
        # One pass over original text: replacements cannot trigger other mappings.
        pieces, pos = [], 0
        while pos < len(value):
            matches = [(value.find(old, pos), old) for old in tokens]
            matches = [(at, old) for at, old in matches if at >= 0]
            if not matches:
                pieces.append(value[pos:])
                break
            at, old = min(matches, key=lambda x: (x[0], -len(x[1])))
            pieces.extend([value[pos:at], tokens[old]])
            pos = at + len(old)
        return ''.join(pieces)

    def convert_member(value, rule, path):
        yes, no = rule
        # Already-converted strings are stable, actual booleans and numeric 0/1 supported.
        if isinstance(value, str):
            if value in ('1', '0'):
                return value
            if value.casefold() == yes.casefold():
                return '1'
            if value.casefold() == no.casefold():
                return '0'
            if value.casefold() in BOOLEAN_STRINGS:
                return BOOLEAN_STRINGS[value.casefold()]
        elif isinstance(value, bool):
            return '1' if value else '0'
        elif isinstance(value, (int, float)) and value in (0, 1):
            return str(int(value))
        # Null/empty remain distinct from false.
        if value is None or value == '':
            return value
        issue(path, f'Unrecognised boolean member {value!r}; left unchanged.')
        return value

    def convert_filter(node, rule, path):
        if isinstance(node, dict):
            # A nested JAQL has its own identity; never inherit a different field's rule.
            if 'jaql' in node or ('table' in node and 'column' in node):
                return
            for key, value in list(node.items()):
                p = path + '/' + str(key)
                if key == 'members' and isinstance(value, list):
                    converted = [convert_member(v, rule, p + '/' + str(i)) for i, v in enumerate(value)]
                    set_value(node, key, converted, path, 'boolean filter members → strings')
                elif key in ('equals', 'doesntEqual') and not isinstance(value, (dict, list)):
                    set_value(node, key, convert_member(value, rule, p), path, 'boolean filter condition → string')
                elif key in ('contains', 'doesntContain', 'startsWith', 'endsWith', 'from', 'to', 'top', 'bottom'):
                    issue(p, 'Condition requires manual review for a boolean-to-text field; left unchanged.')
                elif isinstance(value, (dict, list)):
                    convert_filter(value, rule, p)
                elif key not in ('all', 'explicit', 'multiSelection', 'isCondition', 'locked', 'disabled'):
                    issue(p, f'Unrecognised boolean filter property {key}; review required.')
        elif isinstance(node, list):
            for i, value in enumerate(node):
                convert_filter(value, rule, path + '/' + str(i))

    def walk(original, node, path=''):
        if isinstance(original, dict):
            identity = (original.get('table'), original.get('column'))
            if all(isinstance(v, str) for v in identity):
                if identity in mappings:
                    dest = mappings[identity]
                    set_value(node, 'table', dest[0], path, 'field mapping')
                    set_value(node, 'column', dest[1], path, 'field mapping')
                    if 'tableTitle' in original:
                        set_value(node, 'tableTitle', dest[0], path, 'field mapping')
                    if original.get('columnTitle') == identity[1]:
                        set_value(node, 'columnTitle', dest[1], path, 'field mapping')
                    if original.get('dim') not in (None, dimension(identity)):
                        issue(path + '/dim', 'Dimension differs from table/column identity; review required.')
                if identity in bools:
                    set_value(node, 'datatype', 'text', path, 'boolean field → text')
                    if 'filter' in original:
                        convert_filter(node['filter'], bools[identity], path + '/filter')
                    if original.get('agg'):
                        issue(path, 'Boolean field is used as a measure; verify aggregation after conversion to text.')
            for key, value in original.items():
                p = path + '/' + str(key)
                if isinstance(value, str):
                    if key in ('dim', 'id', 'colorDim', 'formula'):
                        # Identifiers must be exact. Formula text may contain multiple bracketed identifiers.
                        new = token_replace(value) if key == 'formula' else tokens.get(value, value)
                        set_value(node, key, new, path, 'field identifier mapping')
                        if key == 'formula' and any(t in value for t in bool_tokens):
                            issue(p, 'Formula references a boolean-to-text field; review its logic.')
                    elif (any(t in value for t in tokens) or any(t in value for t in bool_tokens)
                          or (key in ('script', 'code') and any(k[0] in value for k in set(mappings) | set(bools)))):
                        issue(p, f'Field reference in unsupported string property {key}; left unchanged.')
                elif key == 'filter' and identity in bools:
                    # Values were converted above. Still walk original nested JAQL objects independently.
                    walk(value, node[key], p)
                else:
                    walk(value, node[key], p)
            # Some exports store a filter as a sibling of JAQL rather than inside it.
            jaql = original.get('jaql')
            if isinstance(jaql, dict) and isinstance(original.get('filter'), (dict, list)):
                key = (jaql.get('table'), jaql.get('column'))
                if key in bools:
                    convert_filter(node['filter'], bools[key], path + '/filter')
        elif isinstance(original, list):
            for i, value in enumerate(original):
                walk(value, node[i], path + '/' + str(i))

    walk(data, out)
    # Logical JSON round-trip validation; scripts and formula logic are never executed.
    json.loads(json.dumps(out, allow_nan=False))
    return out, changes, issues


def expand_uploads(uploads):
    """Return in-memory files. ZIP paths are never extracted to disk."""
    files, total = [], 0
    def add(name, raw):
        nonlocal total
        total += len(raw)
        if len(files) >= MAX_FILES or total > MAX_TOTAL or len(raw) > MAX_FILE:
            raise ValueError('Batch limit: 1,000 dashboards, 20 MB each, 150 MB total.')
        files.append((name, raw))
    for name, raw in uploads:
        if name.lower().endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for entry in z.infolist():
                    if entry.is_dir() or not entry.filename.lower().endswith('.dash'):
                        continue
                    if entry.file_size > MAX_FILE or total + entry.file_size > MAX_TOTAL:
                        raise ValueError('ZIP exceeds dashboard size limits.')
                    add(name + '/' + entry.filename, z.read(entry))
        elif name.lower().endswith('.dash'):
            add(name, raw)
        else:
            raise ValueError(f'Unsupported dashboard file: {name}')
    return files


def csv_bytes(rows, columns):
    buf = io.StringIO(newline='')
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        # Spreadsheet formula-injection protection for report text.
        writer.writerow({k: ("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v) for k, v in row.items()})
    return buf.getvalue().encode('utf-8-sig')


def build_bundle(results, config, include_review=False):
    buf = io.BytesIO()
    report = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mapping-config.json', json.dumps(config, indent=2, ensure_ascii=False))
        for i, r in enumerate(results, 1):
            # A numeric prefix prevents duplicate upload names from overwriting one another.
            name = f'{i:04d}_' + PurePosixPath(r['name'].replace('\\', '/')).name
            z.writestr('originals/' + name, r['raw'])
            eligible = r.get('output') is not None and (not r['issues'] or include_review)
            if eligible:
                z.writestr('migrated/' + name, json.dumps(r['output'], indent=2, ensure_ascii=False, allow_nan=False))
            report.append({k: v for k, v in r.items() if k not in ('raw', 'output') } | {'included': eligible})
        z.writestr('report.json', json.dumps(report, indent=2, ensure_ascii=False))
        flat = [{'file': r['name'], **c, 'before': json.dumps(c['before'], ensure_ascii=False), 'after': json.dumps(c['after'], ensure_ascii=False)} for r in results for c in r.get('changes', [])]
        z.writestr('changes.csv', csv_bytes(flat, ['file', 'path', 'before', 'after', 'reason']))
    return buf.getvalue()
