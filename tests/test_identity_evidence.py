import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from identity_evidence import FIELDS, canonical_url, import_claims, lookup_claim

PF = 'https://www.propertyfinder.ae/en/plp/rent/test-tower-111111111.html'
GUID = 'a' * 49


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.link_db = self.base / 'claims.sqlite'
        self.registry = self.base / 'units.sqlite'
        with sqlite3.connect(self.registry) as db:
            db.execute('CREATE TABLE units (property_id TEXT, record TEXT)')
            self.add_unit(db, unit='1001')
        self.claim = dict(listing_url=PF, listing_guid=GUID, property_id='12345678',
                          unit_number='1001', building='Sample Tower', area='Dubai Marina',
                          land_number='3', evidence_type='signed_listing_mandate',
                          evidence_reference='signed-agreement-1', observed_date='2026-10-10',
                          authorised_for_lookup='yes')

    def add_unit(self, db, **overrides):
        record = dict(unit='1001', area='Dubai Marina', building='Sample Tower',
                      project='Sample Tower', land_number='3')
        record.update(overrides)
        db.execute('INSERT INTO units VALUES (?,?)', ('12345678', json.dumps(record)))

    def write(self, *rows):
        file = self.base / 'claims.csv'
        with file.open('w', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return file

    def test_document_claim_requires_explicit_permission(self):
        with self.assertRaises(ValueError):
            import_claims(self.write(dict(self.claim, authorised_for_lookup='no')), self.link_db)

    def test_registry_corroborates_but_does_not_certify_current_owner(self):
        import_claims(self.write(self.claim), self.link_db)
        result = lookup_claim(PF, self.link_db, self.registry)
        self.assertEqual(result['unit_number'], '1001')
        self.assertEqual(result['status'], 'document_claim_registry_corroborated')
        self.assertFalse(any(key in result for key in ('owner_name', 'owner_mobile', 'owner_email', 'contact_phone')))

    def test_rejects_conflicting_identity_claims(self):
        with self.assertRaises(ValueError):
            import_claims(self.write(self.claim, dict(self.claim, unit_number='1002', evidence_reference='agreement-2')), self.link_db)

    def test_rejects_bad_document_type(self):
        with self.assertRaises(ValueError):
            import_claims(self.write(dict(self.claim, evidence_type='fuzzy_guess')), self.link_db)

    def test_rejects_wrong_registry_unit(self):
        import_claims(self.write(dict(self.claim, unit_number='1002')), self.link_db)
        self.assertEqual(lookup_claim(PF, self.link_db, self.registry)['status'], 'registry_identity_conflict')

    def test_rejects_duplicate_property_id_used_for_different_units(self):
        with sqlite3.connect(self.registry) as db:
            self.add_unit(db, unit='1002')
        import_claims(self.write(self.claim), self.link_db)
        self.assertEqual(lookup_claim(PF, self.link_db, self.registry)['status'], 'registry_identity_conflict')

    def test_rejects_wrong_building(self):
        import_claims(self.write(dict(self.claim, building='Wrong Tower')), self.link_db)
        self.assertEqual(lookup_claim(PF, self.link_db, self.registry)['status'], 'registry_scope_unconfirmed')

    def test_rejects_wrong_land(self):
        import_claims(self.write(dict(self.claim, land_number='999')), self.link_db)
        self.assertEqual(lookup_claim(PF, self.link_db, self.registry)['status'], 'registry_land_conflict')

    def test_unsupported_portal_rejected(self):
        with self.assertRaises(ValueError):
            canonical_url('https://example.com/property/details-123.html')

    def test_identifiers_required(self):
        with self.assertRaises(ValueError):
            import_claims(self.write(dict(self.claim, listing_guid='too-short')), self.link_db)

    def test_no_database_no_claim(self):
        self.assertEqual(lookup_claim(PF, self.link_db, self.registry)['status'], 'no_authorised_document_link')


if __name__ == '__main__':
    unittest.main()
