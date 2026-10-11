"""Strict, read-only reconciliation of authorised PF unit evidence with DLD.

A matching registered unit is a SOURCE CLAIM until the advert-to-property link
is independently authenticated. This never verifies current ownership or
obtains owner contact details, and it never contacts the paused PF API.

The PF compliance "property.id" namespace is *not* assumed equivalent to
canonical DLD property_id. No value from this file is a private owner record.
"""
import json
import re
import sqlite3
from pathlib import Path

GUID_RE = re.compile(r'^[a-z0-9]{49}$')
DLD_ID_RE = re.compile(r'^[1-9][0-9]{0,19}$')
MAX_SCOPE_ROWS = 50000
MAX_PROPERTY_ID_ROWS = 500


def _norm(value):
    return ' '.join(re.findall(r'\w+', str(value or '').casefold()))


def _identity(row):
    """Fields distinguishing two registered DLD units sharing a property ID."""
    return (_norm(row.get('unit')), _norm(row.get('building')),
            _norm(row.get('area')), _norm(row.get('project')),
            _norm(row.get('land_number')),
            _norm(row.get('land_sub_number')),
            _norm(row.get('building_number')))


def _parse_row(record, sql_property_id):
    try:
        row = json.loads(record)
    except (TypeError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    raw_id = str(row.get('property_id') or '')
    sql_id = str(sql_property_id or '')
    if raw_id and sql_id and raw_id != sql_id:
        return None
    if not DLD_ID_RE.fullmatch(raw_id or sql_id):
        return None
    return row


def reconcile_unit(evidence, db_path, *, registered_building, registered_zone,
                   expected_listing_guid, expected_permit=None,
                   expected_license=None):
    """Match an *individual advertised GUID* to a compatible DLD unit candidate.

    Input evidence MUST be the previously inspected, permissioned, offline
    response. A permit number alone cannot identify a unit; Seslia and Cloud
    advertisements share permit identifiers. An untrusted caller may supply
    arbitrary evidence and registered location, so even a successful match is
    never an authenticated advert-to-DLD link.
    """
    if not isinstance(evidence, dict) or evidence.get('status') != 'source_reported_unit':
        return {'status': 'invalid_or_unlinked_source_evidence'}
    if not GUID_RE.fullmatch(str(expected_listing_guid or '')):
        return {'status': 'missing_expected_listing_guid'}
    if evidence.get('listing_guid') != expected_listing_guid:
        return {'status': 'listing_guid_conflict'}
    if evidence.get('source_authenticity') != 'operator_attested_not_independently_authenticated':
        return {'status': 'source_provenance_unconfirmed'}
    if evidence.get('upstream_namespace') != 'pf_compliance_property_id_unproven_dld_equivalence':
        return {'status': 'unrecognised_upstream_namespace'}
    if not evidence.get('reference') or not evidence.get('captured_at'):
        return {'status': 'source_provenance_incomplete'}
    if expected_permit is not None and str(evidence.get('permit_number')) != str(expected_permit):
        return {'status': 'permit_number_conflict'}
    if expected_license is not None and str(evidence.get('license_number')) != str(expected_license):
        return {'status': 'licence_conflict'}
    if not registered_building or not registered_zone:
        return {'status': 'missing_independent_registered_location'}
    unit = _norm(evidence.get('unit_number'))
    if not unit:
        return {'status': 'missing_unit_number'}
    db_file = Path(db_path)
    if not db_file.is_file():
        return {'status': 'dld_registry_unavailable'}

    # Query ONLY the local registry's registered tower and zone. No guessed
    # project-to-building alias or PF internal property ID is used here.
    try:
        with sqlite3.connect(db_file.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('''SELECT property_id, record FROM units
                                 WHERE building=? AND area=? LIMIT ?''',
                              (_norm(registered_building), _norm(registered_zone),
                               MAX_SCOPE_ROWS + 1)).fetchall()
            if len(rows) > MAX_SCOPE_ROWS:
                return {'status': 'registered_scope_too_broad'}

            matches = {}
            for db_row in rows:
                raw = _parse_row(db_row['record'], db_row['property_id'])
                if raw is None:
                    return {'status': 'malformed_or_conflicting_registry_record'}
                if _norm(raw.get('unit')) != unit:
                    continue
                if (_norm(raw.get('building')) != _norm(registered_building) or
                        _norm(raw.get('area')) != _norm(registered_zone)):
                    return {'status': 'registry_location_disagreement'}
                identity = _identity(raw)
                pid = str(db_row['property_id'])
                matches.setdefault(pid, set()).add(identity)
            if not matches:
                return {'status': 'no_unit_in_registered_tower'}
            if len(matches) != 1:
                return {'status': 'ambiguous_unit_in_registered_tower',
                        'count': len(matches)}
            pid = next(iter(matches))
            if len(matches[pid]) != 1:
                return {'status': 'registry_duplicate_identity_conflict'}

            # Crucial global check: the SAME canonical DLD property_id must
            # never map to another tower, unit, zone, project, building or land.
            # Looking up only this tower would miss such corrupt/colliding IDs.
            all_rows = db.execute('SELECT property_id, record FROM units WHERE property_id=? LIMIT ?',
                                  (pid, MAX_PROPERTY_ID_ROWS + 1)).fetchall()
            if not all_rows or len(all_rows) > MAX_PROPERTY_ID_ROWS:
                return {'status': 'canonical_property_id_not_globally_unambiguous'}
            for db_row in all_rows:
                raw = _parse_row(db_row['record'], db_row['property_id'])
                if raw is None:
                    return {'status': 'malformed_or_conflicting_registry_record'}
                if _identity(raw) != next(iter(matches[pid])):
                    return {'status': 'canonical_property_id_collision'}
    except (sqlite3.Error, OSError):
        return {'status': 'registry_schema_unavailable'}

    # The operator-attested upstream data and registered building labels are
    # still not independently authenticated. Never imply current ownership.
    return {'status': 'registry_consistent_source_claim',
            'unit_number': evidence['unit_number'],
            'dld_property_id_candidate': pid,
            'identity_proof': 'not_independently_established',
            'source_reference': evidence['reference'],
            'listing_guid': expected_listing_guid,
            'upstream_property_id': evidence.get('upstream_property_id'),
            'upstream_namespace': evidence['upstream_namespace'],
            'ownership_verified': False,
            'owner_contact_available': False}
