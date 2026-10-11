import unittest
from datetime import datetime, timezone
from pf_compliance_offline import inspect_capture

GUID = 'a' * 49
OTHER = 'b' * 49
VALIDATION = 'https://trakheesi.dubailand.gov.ae/rev/madmoun/listing/validation?khevJujtDig='


def capture():
    return {
        'source': 'authorised_pf_compliance_saved_response',
        'authorised_for_lookup': 'yes',
        'reference': 'captured-case-123',
        'captured_at': '2026-10-10T12:00:00Z',
        'request': {'permitNumber': '157461', 'licenseNumber': '1425589', 'permitType': 'rent'},
        'response': {'status': 'success', 'data': [
            {'validationURL': VALIDATION + GUID, 'permitNumber': '157461',
             'property': {'listingType': 'Unit', 'unitNumber': '2308-A', 'id': '987654321'}},
            {'validationURL': VALIDATION + OTHER, 'permitNumber': '157461',
             'property': {'listingType': 'Unit', 'unitNumber': '915', 'id': '987654322'}}
        ]}
    }


class OfflineComplianceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)

    def test_exact_guid_selects_only_one_among_same_permit(self):
        result = inspect_capture(capture(), GUID, expected_permit='157461',
                                 expected_license='1425589', now=self.now)
        self.assertEqual(result['status'], 'source_reported_unit')
        self.assertEqual(result['unit_number'], '2308-A')
        self.assertEqual(result['upstream_namespace'], 'pf_compliance_property_id_unproven_dld_equivalence')
        self.assertEqual(result['current_owner'], 'not_provided')

    def test_no_guid_match_abstains(self):
        self.assertEqual(inspect_capture(capture(), 'c'*49, now=self.now)['status'],
                         'no_matching_validation_guid')

    def test_mismatched_permit_or_licence_abstains(self):
        self.assertEqual(inspect_capture(capture(), GUID, expected_permit='000', now=self.now)['status'],
                         'permit_number_conflict')
        self.assertEqual(inspect_capture(capture(), GUID, expected_license='000', now=self.now)['status'],
                         'licence_conflict')

    def test_requires_authorisation(self):
        r = capture()
        r['authorised_for_lookup'] = 'no'
        self.assertEqual(inspect_capture(r, GUID, now=self.now)['status'], 'not_authorised')

    def test_missing_unit_abstains(self):
        r = capture()
        r['response']['data'][0]['property']['unitNumber'] = None
        self.assertEqual(inspect_capture(r, GUID, now=self.now)['status'], 'no_explicit_unit_number')

    def test_duplicate_guids_not_selected(self):
        r = capture()
        r['response']['data'][1]['validationURL'] = VALIDATION + GUID
        self.assertEqual(inspect_capture(r, GUID, now=self.now)['status'],
                         'ambiguous_matching_validation_guid')

    def test_missing_permit_type_rejected(self):
        c = capture()
        del c['request']['permitType']
        self.assertEqual(inspect_capture(c, GUID, now=self.now)['status'],
                         'missing_permit_type')

    def test_property_permit_type_conflict_rejected(self):
        c = capture()
        c['response']['data'][0]['property']['permitType'] = 'sale'
        self.assertEqual(inspect_capture(c, GUID, now=self.now)['status'],
                         'permit_type_conflict')

    def test_naive_clock_rejected(self):
        self.assertEqual(
            inspect_capture(capture(), GUID, now=datetime(2026, 10, 11, 12))['status'],
            'invalid_clock_timezone')

    def test_never_leaks_owner_fields(self):
        r = capture()
        r['response']['data'][0]['property']['owner'] = {
            'name': 'PRIVATE NAME', 'phone': '+97100000000'}
        result = inspect_capture(r, GUID, now=self.now)
        self.assertNotIn('PRIVATE NAME', str(result))
        self.assertNotIn('+97100000000', str(result))


if __name__ == '__main__':
    unittest.main()
