import csv
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from matching import import_csv, match, PULSE_URL
import update_data as updater


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.csv = self.root / 'units.csv'
        self.db = self.root / 'units.sqlite'
        self.rows = [
            {'property_id': '1', 'unit_number': '101', 'area_name_en': 'Al Barsha South Fifth',
             'project_name_en': 'Seslia Tower', 'actual_area': '36.88', 'rooms': '7',
             'rooms_en': 'Studio', 'parent_property_id': '900'},
            {'property_id': '2', 'unit_number': '201', 'area_name_en': 'Al Barsha South Fifth',
             'project_name_en': 'Seslia Tower', 'actual_area': '36.90', 'rooms': '7',
             'rooms_en': 'Studio', 'parent_property_id': '901'},
        ]
        self.facts = {'project': 'Seslia Tower', 'size_sqm': 36.883, 'bedrooms': 0}
        self.write()

    def write(self):
        with self.csv.open('w', newline='') as f:
            w = csv.DictWriter(f, self.rows[0].keys())
            w.writeheader()
            w.writerows(self.rows)

    def load(self, as_of=None):
        self.write()
        return import_csv(self.csv, self.db, PULSE_URL, as_of, bulk=True)

    def test_project_only_rows_are_loaded_and_remain_candidates(self):
        self.load()
        r = match(self.db, self.facts)
        self.assertEqual(r['count'], 2)
        self.assertEqual(r['status'], 'possible')
        self.assertIn('project', r['scope'])
        self.assertNotIn('unit', r['records'][0])

    def test_building_input_can_find_project_without_claiming_building(self):
        self.load()
        r = match(self.db, {'building': 'Seslia Tower', 'bedrooms': 0})
        self.assertEqual(r['count'], 2)
        self.assertIn('project', r['scope'])

    def test_room_category_id_is_not_bedroom_count(self):
        for row in self.rows:
            row['rooms_en'] = ''
        self.load()
        self.assertEqual(match(self.db, self.facts)['count'], 2)
        self.assertNotIn('bedrooms', match(self.db, self.facts)['records'][0])

    def test_unknown_size_and_location_rows_are_kept(self):
        self.rows[1]['actual_area'] = '0'
        self.rows[1]['area_name_en'] = ''
        meta = self.load()
        self.assertEqual(meta['rows'], 2)
        self.assertEqual(meta['unknown_size_rows'], 1)
        self.assertEqual(meta['incomplete_identity_rows'], 1)
        r = match(self.db, dict(self.facts, area='Al Barsha South Fifth'))
        self.assertEqual(r['count'], 2)

    def test_fresh_project_id_match_still_not_exact_building(self):
        self.load(date.today().isoformat())
        self.assertEqual(match(self.db, dict(self.facts, property_id='1', area='Al Barsha South Fifth'))['status'], 'possible')

    def test_download_date_is_not_snapshot_date(self):
        self.load()
        self.assertIsNone(match(self.db, self.facts)['metadata']['as_of'])

    def test_html_and_wrong_length_rejected(self):
        for headers in [{'content-type': 'text/html'}, {'content-length': '1'}]:
            with self.subTest(headers=headers), self.assertRaises(updater.UpdateError):
                updater.validate_csv(self.csv, headers)
        self.csv.write_text('<html>Captcha</html>')
        with self.assertRaises(updater.UpdateError):
            updater.validate_csv(self.csv, {})

    def test_public_download_schema_validated(self):
        self.assertGreater(updater.validate_csv(self.csv, {}), 0)

    def test_official_redirect_allowlist(self):
        self.assertTrue(updater.allowed_url(updater.CSV_URL))
        for url in [updater.CSV_URL.replace('https:', 'http:'), updater.CSV_URL.replace('www.dubaipulse.gov.ae', 'evil.example'), 'https://www.dubaipulse.gov.ae/login']:
            self.assertFalse(updater.allowed_url(url))

    def test_access_errors_stop_without_retry(self):
        for code in ('401', '403', '429', '503'):
            def fake(command, **kwargs):
                Path(command[command.index('--dump-header') + 1]).write_text(f'HTTP/2 {code}\n\n')
                return subprocess.CompletedProcess(command, 0, code, '')
            with self.subTest(code=code), patch.object(updater.subprocess, 'run', side_effect=fake) as call:
                with self.assertRaises(updater.UpdateError):
                    updater.download(self.root / 'new.csv', lambda _: None)
                self.assertEqual(call.call_count, 1)

    def test_database_row_and_shrink_guards(self):
        self.load()
        self.assertEqual(updater.validate_database(self.db, minimum=1)['rows'], 2)
        for options in ({'minimum': 3}, {'minimum': 1, 'old_rows': 100}):
            with self.assertRaises(updater.UpdateError):
                updater.validate_database(self.db, **options)

    def test_failed_refresh_preserves_database(self):
        self.load()
        before = self.db.read_bytes()
        with patch.object(updater, 'download', side_effect=updater.UpdateError('Official source unavailable')):
            with self.assertRaises(updater.UpdateError):
                updater.update_bulk(self.root, lambda _: None)
        self.assertEqual(before, self.db.read_bytes())

    def test_successful_refresh_stages_then_replaces(self):
        def fake_download(path, progress):
            path.write_bytes(self.csv.read_bytes())
            return {'url': updater.CSV_URL, 'downloaded_at': 'test'}
        original_validator = updater.validate_database
        with patch.object(updater, 'download', side_effect=fake_download), patch.object(
                updater, 'validate_database', side_effect=lambda path, old_rows: original_validator(path, minimum=1, old_rows=old_rows)):
            meta = updater.update_bulk(self.root, lambda _: None)
        self.assertEqual(meta['rows'], 2)
        self.assertEqual(match(self.db, self.facts)['count'], 2)
        self.assertEqual(meta['download']['url'], updater.CSV_URL)


if __name__ == '__main__':
    unittest.main()
