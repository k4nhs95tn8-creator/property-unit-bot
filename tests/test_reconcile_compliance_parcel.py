import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reconcile_compliance_parcel import reconcile_project_parcel

GUID = 'a' * 49

def evidence():
    return {'status': 'source_reported_unit', 'unit_number': 'A-901',
            'listing_guid': GUID, 'reference': 'consented-capture-1',
            'captured_at': '2026-10-11T12:00:00+00:00',
            'permit_number': '157461', 'license_number': '1425589',
            'upstream_namespace': 'pf_compliance_property_id_unproven_dld_equivalence',
            'source_authenticity': 'operator_attested_not_independently_authenticated'}

def scope():
    return {'source': 'published_project_parcel_crosswalk',
            'authorised_for_lookup': 'yes', 'source_reference': 'project-land-proof-123',
            'project': 'Demo Project', 'zone': 'Reg Zone',
            'land_number': '1730', 'land_sub_number': '0',
            'building_number': '1'}

class ParcelReconciliationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'units.sqlite'
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE units (property_id TEXT, record TEXT, project TEXT, area TEXT)')
            con.execute('CREATE INDEX scope_idx ON units(project,area)')
            con.execute('CREATE INDEX pid_idx ON units(property_id)')
            self.add(con, '10001', 'A-901', '1')

    def add(self, con, pid, unit, building, *, land='1730', subland='0'):
        record = dict(property_id=pid, unit=unit, project='Demo Project',
                      area='Reg Zone', land_number=land,
                      land_sub_number=subland, building_number=building)
        con.execute('INSERT INTO units VALUES (?,?,?,?)',
                    (pid, json.dumps(record), 'demo project', 'reg zone'))

    def run_match(self, ev=None, sc=None):
        return reconcile_project_parcel(ev or evidence(), self.db, scope=sc or scope(),
                                        expected_listing_guid=GUID)

    def test_unnamed_building_verified_parcel_candidate(self):
        result = self.run_match()
        self.assertEqual(result['status'], 'registry_consistent_parcel_unit_source_claim')
        self.assertEqual(result['dld_property_id_candidate'], '10001')
        self.assertFalse(result['current_owner_verified'])

    def test_rejects_unknown_building_number(self):
        item = scope()
        item['building_number'] = None
        self.assertEqual(self.run_match(sc=item)['status'], 'building_number_not_verified')

    def test_rejects_wrong_parcel(self):
        item = scope()
        item['land_number'] = '9999'
        self.assertEqual(self.run_match(sc=item)['status'], 'no_matching_unit_in_verified_parcel')

    def test_same_unit_other_building_is_not_a_collision(self):
        with sqlite3.connect(self.db) as con:
            self.add(con, '10002', 'A-901', '2')
        self.assertEqual(self.run_match()['status'], 'registry_consistent_parcel_unit_source_claim')

    def test_same_unit_same_building_different_property_ids_abstains(self):
        with sqlite3.connect(self.db) as con:
            self.add(con, '10002', 'A-901', '1')
        self.assertEqual(self.run_match()['status'], 'ambiguous_unit_within_parcel_building')

    def test_property_id_collision_elsewhere_abstains(self):
        with sqlite3.connect(self.db) as con:
            self.add(con, '10001', 'B-902', '2')
        self.assertEqual(self.run_match()['status'], 'canonical_dld_id_conflict')

    def test_guid_mismatch_abstains(self):
        item = evidence()
        item['listing_guid'] = 'b'*49
        self.assertEqual(self.run_match(ev=item)['status'], 'guid_mismatch')

    def test_project_scope_missing_abstains(self):
        item = scope()
        item['project'] = ''
        self.assertEqual(self.run_match(sc=item)['status'], 'incomplete_registered_parcel')

    def test_no_source_db_abstains(self):
        self.db.unlink()
        self.assertEqual(self.run_match()['status'], 'dld_registry_unavailable')

if __name__ == '__main__':
    unittest.main()
