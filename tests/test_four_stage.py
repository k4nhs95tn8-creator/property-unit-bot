"""Synthetic contract tests. No user-supplied correct unit is fed to the engine."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import four_stage as f

GUID = 'a' * 49
URL = 'https://www.propertyfinder.ae/en/plp/rent/apartment-for-rent-dubai-demo-12345678.html'
BAYUT_URL = 'https://www.bayut.com/property/details-67890123.html'


def advert():
    return {
        'listing_url': URL,
        'extracted_property_data': {
            'id': '12345678', 'listing_id': 'PF-ABC', 'reference': 'REF-EXAMPLE',
            'location': {'type': 'TOWER', 'name': 'Demo Tower'},
            'project': {'title': 'Demo Tower'},
            'regulatory_details': [{'id': 'regulatory_zone_name',
                'values': {'primary': {'value': {'localized_value': {'en': 'Al Barsha South Fifth'}}}}}],
            'size': {'value': 1076.39, 'unit': 'sqft'}, 'bedrooms': '2',
            'description': 'A lovely flat, 1,080 ft². Unit withheld.',
            'rera': {'permit_validation_url': 'https://trakheesi.dubailand.gov.ae/rev/madmoun/listing/validation?khevJujtDig=' + GUID},
            'agent': {'phone': '+971555555555', 'email': 'private@example.com'},
            'owner': {'name': 'PRIVATE PERSON'},
        }, 'captured_at': '2026-10-01T12:00:00Z',
    }


def permit(unit=None, guid=GUID):
    return {'property_records': [{'property_record': {
        'ListingGuid': guid, 'PermitNumber': '789123', 'ListingNumber': None,
        'PropertyNumber': unit, 'PropertyId': -1, 'FloorNumber': None,
        'BuildingNameEn': 'DEMO TOWER', 'ZoneNameEn': 'Al Barsha South Fifth',
        'PropertySize': 100.0, 'PropertyValue': 110000, 'RoomTypeEn': '2 B/R',
    }}]}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.db = self.root / 'units.sqlite'
        self.cache = self.root / 'listing-index.sqlite'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE metadata(value TEXT)')
            db.execute('INSERT INTO metadata VALUES (?)', (json.dumps({'source': 'synthetic', 'as_of': None, 'rows': 3}),))
            db.execute('CREATE TABLE units(area TEXT, building TEXT, property_id TEXT, record TEXT, project TEXT)')
            for property_id, unit, size, bedrooms in [(101, '1001', 100, 2), (102, '1101', 100, 2), (103, '1201', 99.4, 2)]:
                record = {'area': 'Al Barsha South Fifth', 'project': 'Demo Tower', 'unit': unit,
                    'property_id': str(property_id), 'floor': unit[:2], 'bedrooms': bedrooms, 'size_sqm': size}
                db.execute('INSERT INTO units VALUES (?,?,?,?,?)', (f.norm(record['area']), '', str(property_id), json.dumps(record), f.norm(record['project'])))

    def test_url_allowlist_canonicalizes_tracking(self):
        self.assertEqual(f.canonical_url(URL + '?utm_source=x')[1], URL)
        self.assertEqual(f.canonical_url(BAYUT_URL)[0], 'bayut')
        self.assertEqual(f.canonical_url('https://dubai.dubizzle.com/en/property-for-rent/residential/flat/2026/5/1/test/')[0], 'dubizzle')
        for bad in ('http://evil.org', 'https://www.propertyfinder.ae.evil.org/en/plp/x.html',
                    'https://www.propertyfinder.ae:9090/en/plp/x.html',
                    'https://www.propertyfinder.ae/en/search.html',
                    'https://www.bayut.com/for-sale/property/dubai/',
                    'https://user@www.propertyfinder.ae/en/plp/a.html'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                f.canonical_url(bad)

    def test_pf_public_capture_and_no_pii_in_output(self):
        p = f.ingest_advert(advert())
        self.assertEqual(p['area'], 'Al Barsha South Fifth')
        self.assertEqual(p['permit_guid'], GUID)
        self.assertEqual(p['bedrooms'], 2)
        self.assertAlmostEqual(p['size_sqm'], 100.0, places=2)
        self.assertNotIn('owner', json.dumps(p))
        self.assertNotIn('private@example.com', json.dumps(p))

    def test_four_stages_abstain_on_same_size_rivals(self):
        r = f.analyze(advert(), self.db, permit_payload=permit(), cache_path=self.cache)
        self.assertEqual(r['registry']['count'], 3)
        self.assertEqual(r['permit_check']['status'], 'guid_correlated')
        self.assertEqual(r['identity']['status'], 'not_established')
        self.assertEqual(r['final_status'], 'unresolved')
        self.assertFalse(r['deployment_ready'])
        self.assertNotIn('exact unit identified', f.message(r).lower())
        self.assertNotIn('private@example.com', json.dumps(r))

    def test_area_mismatch_preserved_as_unverified_retained_rival(self):
        # Simulate a newer registry revision where a real unit's area has
        # changed materially. No unit identity is supplied to the resolver.
        with sqlite3.connect(self.db) as db:
            record = dict(json.loads(db.execute(
                "SELECT record FROM units WHERE property_id='101'").fetchone()[0]))
            record['size_sqm'] = 95.0
            db.execute("UPDATE units SET record=? WHERE property_id='101'",
                       (json.dumps(record),))
        report = f.analyze(advert(), self.db, permit_payload=permit())
        self.assertEqual(report['registry']['count'], 2)
        self.assertEqual(report['registry']['retained_count'], 3)
        self.assertEqual(report['registry']['area_discrepancy_count'], 1)
        self.assertIn('101', [r['property_id'] for r in report['registry']['top_retained_area_alternatives']])
        self.assertEqual(report['identity']['status'], 'not_established')
        self.assertEqual(report['final_status'], 'unresolved')
        self.assertIn('Area discrepancy:', f.message(report))

    def test_conflicting_area_still_allows_explicit_permit_claim_as_unverified(self):
        with sqlite3.connect(self.db) as db:
            record = dict(json.loads(db.execute(
                "SELECT record FROM units WHERE property_id='101'").fetchone()[0]))
            record['size_sqm'] = 95.0
            db.execute("UPDATE units SET record=? WHERE property_id='101'",
                       (json.dumps(record),))
        result = f.analyze(advert(), self.db, permit_payload=permit(unit='1001'))
        self.assertEqual(result['identity']['status'], 'permit_record_claim')
        self.assertEqual(result['identity']['property_id'], '101')
        self.assertEqual(result['registry']['retained_count'], 3)
        self.assertFalse(result['deployment_ready'])
        self.assertNotEqual(result['final_status'], 'verified_exact')

    def test_still_refuses_wrong_property_when_area_mismatch_exists(self):
        with sqlite3.connect(self.db) as db:
            record = dict(json.loads(db.execute(
                "SELECT record FROM units WHERE property_id='101'").fetchone()[0]))
            record['size_sqm'] = 95.0
            db.execute("UPDATE units SET record=? WHERE property_id='101'",
                       (json.dumps(record),))
        result = f.analyze(advert(), self.db, permit_payload=permit(unit='9999'))
        self.assertEqual(result['identity']['status'], 'not_established')
        self.assertEqual(result['final_status'], 'unresolved')

    def test_marketing_area_does_not_exclude_registry_units(self):
        cap = advert()
        cap['extracted_property_data']['regulatory_details'] = []
        cap['extracted_property_data']['area'] = 'Dubai Marina'
        result = f.analyze(cap, self.db)
        self.assertEqual(result['registry']['count'], 3)
        self.assertEqual(result['registry']['area_filter'], 'not_applied_advert_and_dld_zone_names_can_differ')
        self.assertEqual(result['identity']['status'], 'not_established')

    def test_permit_wrong_registered_zone_fails_closed(self):
        p = permit(unit='1001')
        p['property_records'][0]['property_record']['ZoneNameEn'] = 'Different Registered Zone'
        result = f.analyze(advert(), self.db, permit_payload=p)
        self.assertEqual(result['identity']['status'], 'not_established')

    def test_conflicting_advert_unit_disclosures_do_not_identify_property(self):
        p = advert()
        p['extracted_property_data']['description'] = 'Unit Number: 1001\\nUnit Number: 1101'
        r = f.analyze(p, self.db)
        self.assertEqual(r['identity']['status'], 'not_established')
        self.assertIsNone(r['advert'].get('unit_disclosure'))

    def test_one_explicit_unit_disclosure_remains_source_claim(self):
        p = advert()
        p['extracted_property_data']['description'] = 'Unit Number: 1001'
        r = f.analyze(p, self.db)
        self.assertEqual(r['identity']['status'], 'advert_self_disclosure')
        self.assertEqual(r['identity']['unit'], '1001')
        self.assertNotEqual(r['final_status'], 'verified_exact')

    def test_one_candidate_is_still_not_verified(self):
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM units WHERE property_id <> '101'")
        r = f.analyze(advert(), self.db, permit_payload=permit())
        self.assertEqual(r['registry']['count'], 1)
        self.assertEqual(r['final_status'], 'unresolved')

    def test_explicit_unit_from_permit_is_only_a_source_claim(self):
        r = f.analyze(advert(), self.db, permit_payload=permit(unit='1001'))
        self.assertEqual(r['identity']['status'], 'permit_record_claim')
        self.assertEqual(r['identity']['property_id'], '101')
        self.assertEqual(r['final_status'], 'permit_explicit_unverified')
        self.assertFalse(r['deployment_ready'])

    def test_explicit_property_unit_number_is_preserved_without_decoding(self):
        p = permit()
        p['property_records'][0]['property_record']['PropertyUnitNumber'] = '1001'
        p['property_records'][0]['property_record']['PermitPropertyId'] = '9007788'
        parsed = f.ingest_permit(p)
        self.assertEqual(parsed['property_unit_number'], '1001')
        self.assertEqual(parsed['permit_property_id'], '9007788')
        self.assertIsNone(parsed['property_id_claim'])
        result = f.analyze(advert(), self.db, permit_payload=p)
        self.assertEqual(result['identity']['status'], 'permit_record_claim')
        self.assertEqual(result['identity']['property_id'], '101')
        self.assertFalse(result['deployment_ready'])

    def test_conflicting_unit_number_fields_refuse_identity(self):
        p = permit(unit='1101')
        p['property_records'][0]['property_record']['PropertyUnitNumber'] = '1001'
        result = f.analyze(advert(), self.db, permit_payload=p)
        self.assertEqual(result['identity']['status'], 'not_established')

    def test_empty_property_unit_number_does_not_invent_unit(self):
        p = permit()
        p['property_records'][0]['property_record']['PropertyUnitNumber'] = None
        p['property_records'][0]['property_record']['PermitPropertyId'] = '907711'
        result = f.analyze(advert(), self.db, permit_payload=p)
        self.assertEqual(result['identity']['status'], 'not_established')

    def test_guid_mismatch_never_joins_permit_to_ad(self):
        r = f.analyze(advert(), self.db, permit_payload=permit(unit='1001', guid='b'*49))
        self.assertEqual(r['permit_check']['status'], 'mismatch')
        self.assertEqual(r['identity']['status'], 'not_established')

    def test_no_guid_never_joins_even_if_descriptors_match(self):
        p = permit(unit='1001')
        p['property_records'][0]['property_record']['ListingGuid'] = None
        r = f.analyze(advert(), self.db, permit_payload=p)
        self.assertEqual(r['permit_check']['status'], 'unlinked')
        self.assertEqual(r['final_status'], 'unresolved')

    def test_conflicting_permit_fields_do_not_promote_identity(self):
        p = permit(unit='1001')
        p['property_records'][0]['property_record']['RoomTypeEn'] = '1 B/R'
        r = f.analyze(advert(), self.db, permit_payload=p)
        self.assertEqual(r['permit_check']['status'], 'conflict')
        self.assertEqual(r['identity']['status'], 'not_established')

    def test_permit_from_command_a_has_transaction_guid_and_blank_listing(self):
        text = ('Real Estate Permit Card\nTransaction Number157461\nListing Number\nEnd Date27/01/2027\n'
                'Property NameSESLIA TOWER\nBuilding NameSESLIA TOWER\n'
                'Property Size(Sqm)90.82\nProperty Value(AED)110,000\n'
                'Room Type2 B/R\n'
                '[Report violation](https://dubailand.gov.ae/en/complaints?listingGuid='+GUID+')')
        result = f.ingest_permit(text)
        self.assertEqual(result['guid'], GUID)
        self.assertEqual(result['transaction_number'], '157461')
        self.assertIsNone(result['listing_number'])
        self.assertEqual(result['bedrooms'], 2)
        self.assertEqual(result['size_sqm'], 90.82)

    def test_dedupe_exact_guid_not_same_size(self):
        first = f.analyze(advert(), self.db, cache_path=self.cache)
        second = f.analyze({'url': BAYUT_URL, 'building': 'Demo Tower', 'project': 'Demo Tower',
            'bedrooms': 2, 'size_sqm': 100, 'permit_guid': GUID}, self.db, cache_path=self.cache)
        self.assertEqual(second['dedup']['same_permit'], [URL])
        self.assertEqual(second['identity']['status'], 'not_established')
        third = f.analyze({'url': 'https://www.bayut.com/property/details-99999999.html',
            'building': 'Demo Tower', 'project': 'Demo Tower', 'bedrooms': 2, 'size_sqm': 100},
            self.db, cache_path=self.cache)
        self.assertEqual(third['dedup']['same_permit'], [])
        self.assertEqual(set(third['dedup']['possible_relists']), {URL, BAYUT_URL})

    def test_refuses_missing_database(self):
        r = f.analyze(advert(), self.root / 'not_there.sqlite')
        self.assertEqual(r['registry']['status'], 'data_unavailable')
        self.assertEqual(r['final_status'], 'unresolved')

    def test_public_html_next_data_without_network(self):
        n = {'props': {'pageProps': {'propertyResult': {'property': advert()['extracted_property_data']}}}}
        source = '<html><script id="__NEXT_DATA__" type="application/json">'+json.dumps(n)+'</script></html>'
        r = f.parse_public_html(source, URL)
        self.assertEqual(r['permit_guid'], GUID)
        self.assertEqual(r['portal'], 'propertyfinder')

    def test_authorised_document_link_is_source_claim_not_owner(self):
        import csv
        from identity_evidence import FIELDS, import_claims
        claim_db = self.root / 'claims.sqlite'
        src = self.root / 'claims.csv'
        with src.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerow(dict(listing_url=URL, listing_guid=GUID, property_id='101',
                unit_number='1001', building='Demo Tower',
                area='Al Barsha South Fifth', land_number='',
                evidence_type='signed_listing_mandate',
                evidence_reference='agency-approval-123',
                observed_date='2026-10-10', authorised_for_lookup='yes'))
        import_claims(src, claim_db)
        # The fixture registry stores building scope as a project, not a tower.
        report = f.analyze(advert(), self.db, permit_payload=permit(),
                           document_db=claim_db, owner_db=self.root / 'owners.sqlite',
                           include_owner=True)
        self.assertEqual(report['identity']['status'], 'authorised_document_claim')
        self.assertEqual(report['identity']['unit'], '1001')
        self.assertEqual(report['owner']['status'], 'no_authorised_owner_dataset')
        self.assertEqual(report['final_status'], 'document_source_claim')
        self.assertFalse(report['deployment_ready'])

    def test_joint_owner_contacts_require_matching_consent_reference(self):
        import csv
        from identity_evidence import FIELDS as EFIELDS, import_claims
        from owner_records import COLUMN_SET, import_records
        evidence = self.root / 'identity-claims.sqlite'
        efile = self.root / 'identity-claims.csv'
        with efile.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=EFIELDS)
            writer.writeheader()
            writer.writerow(dict(listing_url=URL, listing_guid=GUID, property_id='101',
                unit_number='1001', building='Demo Tower',
                area='Al Barsha South Fifth', land_number='',
                evidence_type='signed_listing_mandate',
                evidence_reference='owner-consent-123',
                observed_date='2026-10-10', authorised_for_lookup='yes'))
        import_claims(efile, evidence)
        contacts = self.root / 'owners.sqlite'
        cfile = self.root / 'contacts.csv'
        with cfile.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(COLUMN_SET))
            writer.writeheader()
            for name in ('Coowner A', 'Coowner B'):
                writer.writerow(dict(property_id='101', contact_name=name,
                    contact_phone='', contact_email=name.lower().replace(' ', '')+'@example.test',
                    contact_role='registered_owner_claim', source_name='owner_direct',
                    source_reference='owner-consent-123', observed_date='2026-10-10',
                    use_basis='owner_consent', authorised_for_lookup='yes'))
        import_records(cfile, contacts)
        report = f.analyze(advert(), self.db, permit_payload=permit(), document_db=evidence,
                           owner_db=contacts, include_owner=True)
        self.assertEqual(report['identity']['status'], 'authorised_document_claim')
        self.assertEqual(report['owner']['status'], 'authorised_source_reported_contacts')
        self.assertEqual(len(report['owner']['contacts']), 2)
        self.assertEqual(report['owner']['ownership_status'], 'not_independently_verified_current_owner')

    def test_crosswalk_alone_cannot_release_owner_contact(self):
        report = f.analyze(advert(), self.db, permit_payload=permit(),
                           owner_db=self.root / 'none.sqlite', include_owner=True)
        self.assertEqual(report['owner']['status'], 'current_owner_not_independently_verified')

    def test_wrong_guid_cannot_upgrade_document_claim(self):
        import csv
        from identity_evidence import FIELDS, import_claims
        claim_db = self.root / 'claims.sqlite'
        src = self.root / 'claims.csv'
        with src.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerow(dict(listing_url=URL, listing_guid='b'*49, property_id='101',
                unit_number='1001', building='Demo Tower',
                area='Al Barsha South Fifth', land_number='',
                evidence_type='signed_listing_mandate',
                evidence_reference='agency-approval-123',
                observed_date='2026-10-10', authorised_for_lookup='yes'))
        import_claims(src, claim_db)
        result = f.analyze(advert(), self.db, permit_payload=permit(), document_db=claim_db)
        self.assertEqual(result['identity']['status'], 'not_established')
        self.assertEqual(result['document']['status'], 'document_claim_conflicts_with_advert_or_permit')

    def test_no_public_network_from_default_research(self):
        with patch.object(f, 'fetch_public_listing', side_effect=AssertionError('network attempted')):
            f.analyze(advert(), self.db, permit_payload=permit())


if __name__ == '__main__':
    unittest.main()

class TelegramResearchTests(PipelineTests):
    """Exercises the opt-in wrapper without starting any Telegram connections."""
    def test_link_manual_details_permit_flow(self):
        from research_dialog import ResearchConversation
        dialog = ResearchConversation(self.db, self.root / 'listings.json')
        self.assertIn('Advert accepted', dialog.handle(123, URL))
        response = dialog.handle(123, 'area: Al Barsha South Fifth\nbuilding: Demo Tower\nbedrooms: 2\nsize_sqm: 100')
        self.assertIn('compatible registry rows: 3', response)
        self.assertIn('EXACT UNIT NOT VERIFIED', response)
        permit_copy = ('Real Estate Permit Card\nTransaction Number789123\nListing Number\n'
                       'Building NameDEMO TOWER\nRoom Type2 B/R\n'
                       '[Report violation](https://dubailand.gov.ae/en/complaints?listingGuid='+GUID+')')
        reply = dialog.handle(123, permit_copy)
        self.assertIn('permit: unlinked', reply)  # User-provided visible details alone contain no GUID
        self.assertIn('EXACT UNIT NOT VERIFIED', reply)
        self.assertIn('cleared', dialog.handle(123, '/cancel'))
        self.assertIn('Send a supported advert URL first', dialog.handle(123, permit_copy))

    def test_research_mode_refuses_known_answer_as_input(self):
        from research_dialog import ResearchConversation
        dialog = ResearchConversation(self.db, self.root / 'listings.json')
        dialog.handle(1, URL)
        self.assertIn('Do not supply the known unit', dialog.handle(1, 'unit: 1001'))
        self.assertIn('Do not supply the known unit', dialog.handle(1, 'property_id: 101'))

    def test_saved_listing_and_matching_guid(self):
        from research_dialog import ResearchConversation
        path = self.root / 'listings.json'
        path.write_text(json.dumps({URL: {'facts': {'area': 'Al Barsha South Fifth', 'project': 'Demo Tower', 'building': 'Demo Tower', 'size_sqm': 100, 'bedrooms': 2}, 'saved_at': __import__('time').time()}}))
        dialog = ResearchConversation(self.db, path)
        self.assertIn('compatible registry rows: 3', dialog.handle(1, URL))
        # The recorded ad lacks the GUID, so receiving a permit with an identical
        # GUID is not allowed to upgrade this case.
        reply = dialog.handle(1, json.dumps(permit()))
        self.assertIn('permit: unlinked', reply)