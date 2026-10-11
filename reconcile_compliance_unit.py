"""Read-only reconciliation of authorised PF unit evidence with a local DLD snapshot.

The upstream PF compliance property ID is NOT treated as a canonical DLD ID.
Unit number and tower must both be independently named, with registered zone
corroboration; source-reported results never prove current ownership.
"""
import json
import re
import sqlite3
from pathlib import Path

def _norm(value):
    return ' '.join(re.findall(r'\w+', str(value or '').casefold()))

def reconcile_unit(evidence, db_path, *, registered_building, registered_zone):
    if not isinstance(evidence, dict) or evidence.get('status') != 'source_reported_unit':
        return {'status': 'invalid_or_unlinked_source_evidence'}
    if not registered_building or not registered_zone:
        return {'status': 'missing_independent_registered_location'}
    unit = _norm(evidence.get('unit_number'))
    if not unit:
        return {'status': 'missing_unit_number'}
    db_file = Path(db_path)
    if not db_file.is_file():
        return {'status': 'dld_registry_unavailable'}
    # The identity key to reconcile is the published UNIT label. Never query
    # using PF property.id: its namespace is not proven equivalent.
    try:
        with sqlite3.connect(f'file:{db_file.resolve().as_posix()}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            # Registry is an older SQLite snapshot. Location fields are kept
            # both as normalized columns and in the original record JSON.
            rows = db.execute("SELECT property_id, record FROM units WHERE building=? AND area=?",
                              (_norm(registered_building), _norm(registered_zone))).fetchall()
    except (sqlite3.Error, OSError):
        return {'status': 'registry_schema_unavailable'}
    identities = set()
    for row in rows:
        try:
            r = json.loads(row['record'])
        except (TypeError, ValueError):
            return {'status': 'malformed_registry_record'}
        if _norm(r.get('unit')) != unit:
            continue
        if _norm(r.get('building')) != _norm(registered_building) or _norm(r.get('area')) != _norm(registered_zone):
            return {'status': 'registry_location_disagreement'}
        pid = str(r.get('property_id') or row['property_id'] or '')
        if not re.fullmatch(r'\d{1,20}', pid):
            return {'status': 'missing_canonical_property_id'}
        identities.add((pid, str(r.get('unit'))))
    if not identities:
        return {'status': 'no_unit_in_registered_tower'}
    if len(identities) != 1:
        return {'status': 'ambiguous_unit_in_registered_tower', 'count': len(identities)}
    pid, label = next(iter(identities))
    # This proves compatibility with the historical registry, not that the
    # current advert and registry are independently authenticated as the same.
    return {'status': 'registry_consistent_source_claim',
            'unit_number': label,
            'dld_property_id_candidate': pid,
            'identity_proof': 'not_independently_established',
            'source_reference': evidence.get('reference'),
            'upstream_property_id': evidence.get('upstream_property_id'),
            'upstream_namespace': evidence.get('upstream_namespace'),
            'ownership_verified': False,
            'owner_contact_available': False}
