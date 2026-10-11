"""Kuku offline acceptance gate: scores *held-out* truth without publishing it.

Run this on a private workstation. The input contains independent adjudicated
facts, never imported into Kuku's runtime join engine. Printed output is only
aggregate numbers (no names, phone numbers, units or property IDs).
"""
import argparse
import json
from collections import Counter
from pathlib import Path

CLAIM_STATUSES = {'permit_record_claim', 'external_crosswalk_claim',
                  'authorised_document_claim', 'advert_self_disclosure'}
SUPPORTED_PORTALS = {'propertyfinder', 'bayut'}


def audit(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('A non-empty blind-case list is required.')
    counts = Counter()
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError('Invalid case.')
        key = case.get('case_id')
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError('Each case must have a unique opaque case_id.')
        seen.add(key)
        portal = case.get('portal')
        if portal not in SUPPORTED_PORTALS:
            raise ValueError('Only eligible Property Finder/Bayut cases.')
        truth = case.get('adjudicated_truth')
        report = case.get('kuku_report')
        if not isinstance(truth, dict) or not isinstance(report, dict):
            raise ValueError('Case is missing independent truth or frozen Kuku report.')
        if truth.get('property_id') in (None, '') or truth.get('unit') in (None, ''):
            raise ValueError('Every blind test must have independent exact-unit truth.')
        identity = report.get('identity')
        if not isinstance(identity, dict):
            raise ValueError('Kuku identity record missing.')
        counts['cases'] += 1
        counts['portal_' + portal] += 1
        # A source-reported identity is a *claim*, even when correct on the
        # benchmark. Never count it as independently verified coverage.
        claimed = identity.get('status') in CLAIM_STATUSES
        exact = (str(identity.get('property_id')) == str(truth['property_id'])
                 and str(identity.get('unit')).strip().casefold() ==
                 str(truth['unit']).strip().casefold())
        if claimed:
            counts['source_claims'] += 1
            counts['correct_source_claims' if exact else 'incorrect_source_claims'] += 1
        else:
            counts['abstentions'] += 1
        if report.get('final_status') == 'verified_exact' or report.get('deployment_ready') is True:
            counts['unsupported_verified_labels'] += 1
        # Private contact labels never enter the printed report.
        if report.get('owner', {}).get('status') == 'authorised_source_reported_contacts':
            counts['consented_contact_claims'] += 1
        if case.get('independently_verified_current_owner') is True:
            counts['owner_truth_verified_cases'] += 1
    total = counts['cases']
    metrics = {
        'test_cases': total,
        'portals': {p: counts['portal_' + p] for p in sorted(SUPPORTED_PORTALS)},
        'source_claims': counts['source_claims'],
        'correct_source_claims': counts['correct_source_claims'],
        'incorrect_source_claims': counts['incorrect_source_claims'],
        'abstentions': counts['abstentions'],
        'unsupported_verified_labels': counts['unsupported_verified_labels'],
        'verified_exact_cases': 0,  # current resolver cannot make this claim
        'owner_truth_verified_cases': counts['owner_truth_verified_cases'],
        'consented_contact_claims': counts['consented_contact_claims'],
        'source_claim_precision': (
            counts['correct_source_claims'] / counts['source_claims']
            if counts['source_claims'] else None),
        'verified_exact_coverage': 0.0,
        'complete': False,
        'reason': 'No independent universal advert-to-unit and current-owner/contact crosswalk verified.'
    }
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private, offline blind Kuku benchmark.')
    parser.add_argument('--cases', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    result = audit(json.loads(args.cases.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
