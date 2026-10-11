import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reconcile_compliance_unit import reconcile_unit

def evidence():
    return {'status':'source_reported_unit', 'unit_number':'A-901',
            'upstream_property_id':'900001', 'reference':'synthetic-reference',
            'upstream_namespace':'pf_compliance_property_id_unproven_dld_equivalence'}

class RegistryReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'units.sqlite'
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE units (property_id TEXT,building TEXT,area TEXT, record TEXT)')
            con.execute('CREATE INDEX by_location ON units (building, area)')
            self.add(con, '10001','A-901','Test Tower A')

    def add(self,con,pid,unit,building='Test Tower A',area='Reg Zone'):
        con.execute('INSERT INTO units VALUES (?,?,?,?)',
            (pid,building.casefold(),area.casefold(),
             json.dumps({'property_id':pid,'unit':unit,'building':building,'area':area})))

    def lookup(self,e=None,building='Test Tower A',area='Reg Zone'):
        return reconcile_unit(e or evidence(),self.db,
                   registered_building=building,registered_zone=area)

    def test_compatible_registry_source_claim(self):
        r=self.lookup()
        self.assertEqual(r['status'],'registry_consistent_source_claim')
        self.assertEqual(r['dld_property_id_candidate'],'10001')
        self.assertNotEqual(r['upstream_property_id'],r['dld_property_id_candidate'])
        self.assertFalse(r['ownership_verified'])

    def test_refuses_different_registered_building(self):
        self.assertEqual(self.lookup(building='Test Tower B')['status'],
                         'no_unit_in_registered_tower')

    def test_refuses_project_only(self):
        self.assertEqual(self.lookup(building=None)['status'],
                         'missing_independent_registered_location')

    def test_refuses_duplicate_unit_in_same_tower(self):
        with sqlite3.connect(self.db) as con:
            self.add(con,'10002','A-901')
        self.assertEqual(self.lookup()['status'],'ambiguous_unit_in_registered_tower')

    def test_refuses_inconsistent_raw_building(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE units SET record=? WHERE property_id='10001'",(
                json.dumps({'unit':'A-901','property_id':'10001',
                            'building':'Test Tower B','area':'Reg Zone'}),))
        self.assertEqual(self.lookup()['status'],'registry_location_disagreement')

    def test_refuses_unproven_upstream_response(self):
        e=evidence()
        e['status']='missing_advert_guid'
        self.assertEqual(self.lookup(e)['status'],'invalid_or_unlinked_source_evidence')

    def test_absent_registry(self):
        self.db.unlink()
        self.assertEqual(self.lookup()['status'],'dld_registry_unavailable')

if __name__=='__main__':
    unittest.main()
