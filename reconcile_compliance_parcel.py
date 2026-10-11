"""Offline project/parcel reconciliation for DLD snapshots without tower names.

A legitimate unit-level permit response may contain the unit label even when
historical open DLD "units" records lack a building name. This read-only path
uses separately supported project, zone, land and building NUMBER references;
it never guesses the absent building number, reads owner data, calls an API, or
equates PF property.id with canonical DLD property_id.

All outputs remain source claims. Matching historical rows does not establish
advert authenticity, present ownership, or anyone's private contact details.
"""
import re
import sqlite3
from pathlib import Path

from reconcile_compliance_unit import (
    _norm, _identity, _parse_row, DLD_ID_RE, GUID_RE, MAX_PROPERTY_ID_ROWS
)

MAX_PROJECT_ROWS = 25000
KEY_PATTERN = re.compile(r'^[0-9]{1,12}$')


def _positive_id(value):
    return bool(DLD_ID_RE.fullmatch(str(value or '')))


def reconcile_project_parcel(evidence, db_path, *, scope, expected_listing_guid,
                             expected_permit=None, expected_license=None):
    """Resolve only a source-reported unit within a registered parcel+building.

    'scope' must come from a separately vetted public project/land/building
    crosswalk, not an inferred apartment/owner answer:
      source: published_project_parcel_crosswalk
      authorised_for_lookup: yes
      source_reference: non-sensitive provenance identifier
      project, zone, land_number, land_sub_number, building_number: exact
    The provenance flag cannot authenticate the source itself.
    """
    if not isinstance(evidence, dict) or evidence.get('status') != 'source_reported_unit':
        return {'status': 'invalid_unit_evidence'}
    if not GUID_RE.fullmatch(str(expected_listing_guid or '')):
        return {'status': 'missing_listing_guid'}
    if evidence.get('listing_guid') != expected_listing_guid:
        return {'status': 'guid_mismatch'}
    if evidence.get('source_authenticity') != 'operator_attested_not_independently_authenticated':
        return {'status': 'upstream_source_not_attributed'}
    if evidence.get('upstream_namespace') != 'pf_compliance_property_id_unproven_dld_equivalence':
        return {'status': 'upstream_namespace_unverified'}
    if not evidence.get('reference') or not evidence.get('captured_at'):
        return {'status': 'missing_unit_source_receipt'}
    if expected_permit is not None and str(evidence.get('permit_number')) != str(expected_permit):
        return {'status': 'permit_mismatch'}
    if expected_license is not None and str(evidence.get('license_number')) != str(expected_license):
        return {'status': 'licence_mismatch'}
    unit = _norm(evidence.get('unit_number'))
    if not unit:
        return {'status': 'missing_unit_number'}
    if (not isinstance(scope, dict)
            or scope.get('source') != 'published_project_parcel_crosswalk'
            or scope.get('authorised_for_lookup') != 'yes'
            or not scope.get('source_reference')):
        return {'status': 'missing_authorised_parcel_scope'}

    project = _norm(scope.get('project'))
    zone = _norm(scope.get('zone'))
    land = str(scope.get('land_number') or '').strip()
    subland = str(scope.get('land_sub_number') if scope.get('land_sub_number') is not None else '').strip()
    building = str(scope.get('building_number') or '').strip()
    if not project or not zone or not KEY_PATTERN.fullmatch(land) or not KEY_PATTERN.fullmatch(subland):
        return {'status': 'incomplete_registered_parcel'}
    if not KEY_PATTERN.fullmatch(building):
        return {'status': 'building_number_not_verified'}

    source_db = Path(db_path)
    if not source_db.is_file():
        return {'status': 'dld_registry_unavailable'}
    try:
        # Reads only the published, normalized units index; never writes to
        # the source database or creates missing tables/indexes on disk.
        with sqlite3.connect(source_db.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                'SELECT property_id, record FROM units WHERE project=? AND area=? LIMIT ?',
                (project, zone, MAX_PROJECT_ROWS + 1)
            ).fetchall()
            if len(rows) > MAX_PROJECT_ROWS:
                return {'status': 'project_scope_too_broad'}

            compatible_ids = {}
            parcel_units_examined = 0
            for item in rows:
                record = _parse_row(item['record'], item['property_id'])
                if record is None:
                    return {'status': 'corrupt_registry_row'}
                if (str(record.get('land_number') or '').strip() != land
                        or str(record.get('land_sub_number') if record.get('land_sub_number') is not None else '').strip() != subland):
                    continue
                if str(record.get('building_number') or '').strip() != building:
                    continue
                parcel_units_examined += 1
                if _norm(record.get('unit')) != unit:
                    continue
                pid = str(item['property_id'] or '')
                if not _positive_id(pid):
                    return {'status': 'missing_canonical_dld_id'}
                compatible_ids.setdefault(pid, set()).add(_identity(record))

            if not compatible_ids:
                return {'status': 'no_matching_unit_in_verified_parcel',
                        'parcel_unit_rows': parcel_units_examined}
            if len(compatible_ids) != 1:
                return {'status': 'ambiguous_unit_within_parcel_building',
                        'candidate_property_ids': len(compatible_ids)}
            pid, identities = next(iter(compatible_ids.items()))
            if len(identities) != 1:
                return {'status': 'inconsistent_duplicate_registry_id'}
            expected_identity = next(iter(identities))

            # A collision ELSEWHERE in the same SQLite must invalidate this
            # candidate too; parcel-scoped uniqueness alone is insufficient.
            global_rows = db.execute(
                'SELECT property_id, record FROM units WHERE property_id=? LIMIT ?',
                (pid, MAX_PROPERTY_ID_ROWS + 1)
            ).fetchall()
            if not global_rows or len(global_rows) > MAX_PROPERTY_ID_ROWS:
                return {'status': 'canonical_dld_id_not_globally_unique'}
            for item in global_rows:
                record = _parse_row(item['record'], item['property_id'])
                if record is None or _identity(record) != expected_identity:
                    return {'status': 'canonical_dld_id_conflict'}
    except (sqlite3.Error, OSError):
        return {'status': 'registry_schema_or_access_unavailable'}

    return {
        'status': 'registry_consistent_parcel_unit_source_claim',
        'dld_property_id_candidate': pid,
        'unit_number': str(evidence['unit_number']),
        'listing_guid': expected_listing_guid,
        'permit_evidence_reference': evidence['reference'],
        'parcel_evidence_reference': scope['source_reference'],
        'land_number': land,
        'land_sub_number': subland,
        'building_number': building,
        'source_authenticity': 'not_independently_authenticated',
        'identity_proof': 'not_independently_established',
        'current_owner_verified': False,
        'owner_contact_available': False,
    }
