import unittest
from acceptance_gate import audit


def case(case_id='one', portal='propertyfinder', status='not_established',
         predicted_id=None, predicted_unit=None):
    return {
        'case_id': case_id, 'portal': portal,
        'adjudicated_truth': {'property_id': '1234', 'unit': '1001'},
        'kuku_report': {
            'identity': {'status': status, 'property_id': predicted_id, 'unit': predicted_unit},
            'final_status': 'unresolved',
            'deployment_ready': False,
            'owner': {'status': 'not_requested'}
        }
    }


class AcceptanceTests(unittest.TestCase):
    def test_unsupported_auto_completion_never_passes(self):
        rows = [case('one'), case('two', 'bayut', 'advert_self_disclosure', '1234', '1001')]
        result = audit(rows)
        self.assertFalse(result['complete'])
        self.assertEqual(result['test_cases'], 2)
        self.assertEqual(result['correct_source_claims'], 1)
        self.assertEqual(result['abstentions'], 1)
        self.assertEqual(result['verified_exact_coverage'], 0)

    def test_false_positive_counts_as_incorrect_source_claim(self):
        result = audit([case(status='external_crosswalk_claim',
                             predicted_id='9999', predicted_unit='1001')])
        self.assertEqual(result['incorrect_source_claims'], 1)
        self.assertEqual(result['source_claim_precision'], 0)

    def test_never_prints_sensitive_identifiers_from_cases(self):
        input_case = case(status='authorised_document_claim',
                          predicted_id='1234', predicted_unit='1001')
        input_case['private_phone'] = '+971555554444'
        r = audit([input_case])
        self.assertNotIn('1234', str(r))
        self.assertNotIn('1001', str(r))
        self.assertNotIn('+971555554444', str(r))

    def test_refuses_missing_truth_and_duplicate_case(self):
        with self.assertRaises(ValueError):
            audit([])
        with self.assertRaises(ValueError):
            audit([case('dup'), case('dup')])
        x = case()
        del x['adjudicated_truth']['property_id']
        with self.assertRaises(ValueError):
            audit([x])

    def test_detects_unsupported_deployment_claim(self):
        c = case()
        c['kuku_report']['deployment_ready'] = True
        r = audit([c])
        self.assertEqual(r['unsupported_verified_labels'], 1)
        self.assertFalse(r['complete'])


if __name__ == '__main__':
    unittest.main()
