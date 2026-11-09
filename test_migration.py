import copy
import io
import json
import unittest
import zipfile

from migration import (build_bundle, expand_uploads, inventory, migrate,
                       parse_dashboard, rules_from_rows)


def dashboard(jaql, **extra):
    return {'title': 'Test', 'oid': 'keep-me', 'widgets': [], 'filters': [{'jaql': jaql, **extra}]}


class MigrationTests(unittest.TestCase):
    def test_move_and_rename_everywhere_preserving_labels_and_ids(self):
        source = dashboard({'table': 'Old', 'column': 'A', 'dim': '[Old.A]',
                            'columnTitle': 'A', 'tableTitle': 'Old', 'title': 'Custom label'})
        source['widgets'] = [{'oid': 'w', 'field': {'id': '[Old.A]'}, 'formula': '[Old.A] + [Other.X]'}]
        original = copy.deepcopy(source)
        out, changes, issues = migrate(source, {('Old', 'A'): ('New', 'B')}, {})
        self.assertEqual(out['filters'][0]['jaql']['dim'], '[New.B]')
        self.assertEqual(out['filters'][0]['jaql']['columnTitle'], 'B')
        self.assertEqual(out['filters'][0]['jaql']['title'], 'Custom label')
        self.assertEqual(out['widgets'][0]['field']['id'], '[New.B]')
        self.assertEqual(out['widgets'][0]['formula'], '[New.B] + [Other.X]')
        self.assertEqual(source, original)
        self.assertEqual(out['oid'], 'keep-me')
        self.assertEqual(len(changes), 7)
        self.assertFalse(issues)

    def test_boolean_scope_nested_conditions_and_preserved_empty(self):
        j = {'table': 'Old', 'column': 'Flag', 'datatype': 'boolean',
             'filter': {'or': [{'members': ['Yes', 'NO', True, False, 1, 0, '', None]},
                               {'exclude': {'members': ['No']}}]}}
        source = dashboard(j)
        source['filters'].append({'jaql': {'table': 'Other', 'column': 'Flag', 'filter': {'members': ['Yes']}}})
        out, _, issues = migrate(source, {}, {('Old', 'Flag'): ('Yes', 'No')})
        self.assertEqual(out['filters'][0]['jaql']['filter']['or'][0]['members'], ['1', '0', '1', '0', '1', '0', '', None])
        self.assertEqual(out['filters'][0]['jaql']['filter']['or'][1]['exclude']['members'], ['0'])
        self.assertEqual(out['filters'][1]['jaql']['filter']['members'], ['Yes'])
        self.assertEqual(out['filters'][0]['jaql']['datatype'], 'text')
        self.assertFalse(issues)

    def test_boolean_and_move_and_second_run(self):
        source = dashboard({'table': 'Old', 'column': 'Flag', 'dim': '[Old.Flag]', 'filter': {'members': ['Yes', 'No']}})
        rules = {('Old', 'Flag'): ('New', 'Flag')}
        out, _, _ = migrate(source, rules, {('Old', 'Flag'): ('Yes', 'No')})
        self.assertEqual(out['filters'][0]['jaql']['filter']['members'], ['1', '0'])
        again, changes, _ = migrate(out, rules, {('Old', 'Flag'): ('Yes', 'No')})
        self.assertEqual(out, again)
        self.assertFalse(changes)

    def test_custom_boolean_labels(self):
        source = dashboard({'table': 'T', 'column': 'F', 'filter': {'members': ['Enabled', 'disabled']}})
        out, _, _ = migrate(source, {}, {('T', 'F'): ('Enabled', 'Disabled')})
        self.assertEqual(out['filters'][0]['jaql']['filter']['members'], ['1', '0'])

    def test_unknown_members_and_conditions_flagged(self):
        source = dashboard({'table': 'T', 'column': 'F', 'filter': {'members': ['Maybe'], 'contains': 'Yes'}})
        out, _, issues = migrate(source, {}, {('T', 'F'): ('Yes', 'No')})
        self.assertEqual(len(issues), 2)
        self.assertEqual(out['filters'][0]['jaql']['filter']['members'], ['Maybe'])

    def test_sibling_filter(self):
        source = dashboard({'table': 'T', 'column': 'F'}, filter={'members': ['Yes']})
        out, _, _ = migrate(source, {}, {('T', 'F'): ('Yes', 'No')})
        self.assertEqual(out['filters'][0]['filter']['members'], ['1'])

    def test_nested_jaql_does_not_inherit_boolean_scope(self):
        source = dashboard({'table': 'T', 'column': 'F', 'filter': {
            'by': {'table': 'Other', 'column': 'F', 'filter': {'members': ['Yes']}}}})
        out, _, _ = migrate(source, {}, {('T', 'F'): ('Yes', 'No')})
        self.assertEqual(out['filters'][0]['jaql']['filter']['by']['filter']['members'], ['Yes'])

    def test_validation_conflicts_chains_and_partial_rows(self):
        row = lambda a, b: {'Old table': a, 'Old column': 'F', 'New table': b, 'New column': 'F'}
        for rows in [[row('A', 'B'), row('A', 'C')], [row('A', 'B'), row('B', 'C')], [{'Old table': 'A'}]]:
            with self.assertRaises(ValueError):
                rules_from_rows(rows, [])

    def test_unmapped_fields_unchanged_and_inventory(self):
        source = dashboard({'table': 'T', 'column': 'F'})
        out, changes, issues = migrate(source, {('T', 'Other'): ('X', 'Y')}, {})
        self.assertEqual(source, out)
        self.assertFalse(changes or issues)
        self.assertEqual(inventory(source)[0]['References'], 1)

    def test_script_not_rewritten_but_flagged(self):
        source = dashboard({'table': 'T', 'column': 'F'})
        source['script'] = 'doSomething("[T.F]")'
        out, _, issues = migrate(source, {('T', 'F'): ('N', 'F')}, {})
        self.assertEqual(out['script'], source['script'])
        self.assertTrue(issues)

    def test_invalid_json_duplicate_keys_and_shape(self):
        for raw in [b'{', b'[]', b'{"widgets":[],"widgets":[]}', b'{"widgets":[],"x":NaN}']:
            with self.assertRaises(ValueError):
                parse_dashboard(raw)

    def test_zip_duplicate_names_and_review_exclusion(self):
        raw = json.dumps(dashboard({'table': 'T', 'column': 'F'})).encode()
        results = [{'name': 'same.dash', 'raw': raw, 'output': json.loads(raw), 'changes': [], 'issues': [], 'status': 'Unchanged'},
                   {'name': 'same.dash', 'raw': raw, 'output': json.loads(raw), 'changes': [], 'issues': [{'message': 'Review'}], 'status': 'Review'}]
        with zipfile.ZipFile(io.BytesIO(build_bundle(results, {}))) as z:
            self.assertIn('migrated/0001_same.dash', z.namelist())
            self.assertNotIn('migrated/0002_same.dash', z.namelist())
            self.assertIn('originals/0002_same.dash', z.namelist())
        with zipfile.ZipFile(io.BytesIO(build_bundle(results, {}, True))) as z:
            self.assertIn('migrated/0002_same.dash', z.namelist())

    def test_zip_input_stays_in_memory(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('../../sample.dash', '{"widgets":[]}')
            z.writestr('ignore.txt', 'not a dashboard')
        files = expand_uploads([('batch.zip', buf.getvalue())])
        self.assertEqual(len(files), 1)
        self.assertEqual(parse_dashboard(files[0][1]), {'widgets': []})


if __name__ == '__main__':
    unittest.main()
