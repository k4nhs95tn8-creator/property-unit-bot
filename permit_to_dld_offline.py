"""Offline, source-attributed unit-to-DLD registry crosswalk.

No HTTP, no API credentials, no owner information, no guessed unit numbers.
Use only an already authorised, GUID-linked unit disclosure and a separately
captured authoritative tower/zone. An exact registered unit remains a sourced
CLAIM until the source and advert association are independently authenticated.
"""
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date
from pathlib import Path

MAX_TOWER_ROWS = 25000
MAX_PROPERTY_ID_ROWS = 500
VALID_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/-]{0,29}$")
VALID_ID = re.compile(r"^[0-9]{1,20}$")


def norm(value):
    return " ".join(re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).split())


def _snapshot_is_recent(raw_date):
    if not raw_date:
        return False
    try:
        lag = (date.today() - date.fromisoformat(raw_date)).days
        return 0 <= lag <= 30
    except (ValueError, TypeError):
        return False


def reconcile_authorised_unit(source_claim, registry_db, *,
                              registered_tower, registered_zone,
                              registered_land=None, registered_subland=None):
    """Corroborate a *known* authorised unit against DLD, never discover one.

    The tower and zone must come from a separate, legitimately obtained
    property-specific record tied to the very same listing GUID, not from a
    marketing-neighbourhood name. Source provenance cannot be authenticated
    by this function. The property.id in the compliance response is NOT used
    as a DLD property_id. No owner contact is queried.
    """
    if not isinstance(source_claim, dict) or source_claim.get("status") != "source_reported_unit":
        return {"status": "no_authorised_unit_source"}
    unit = source_claim.get("unit_number")
    if not isinstance(unit, str) or not VALID_UNIT.fullmatch(unit):
        return {"status": "invalid_source_unit"}
    if source_claim.get("upstream_namespace") != "pf_compliance_property_id_unproven_dld_equivalence":
        return {"status": "unknown_upstream_id_namespace"}
    guid = source_claim.get("listing_guid")
    if not isinstance(guid, str) or not re.fullmatch(r"[a-z0-9]{49}", guid):
        return {"status": "missing_listing_guid"}
    if not isinstance(registered_tower, str) or not norm(registered_tower):
        return {"status": "missing_authoritative_tower"}
    if not isinstance(registered_zone, str) or not norm(registered_zone):
        return {"status": "missing_authoritative_zone"}
    if registered_land is not None and not re.fullmatch(r"[0-9]+", str(registered_land)):
        return {"status": "invalid_registered_land"}
    if registered_subland is not None and not re.fullmatch(r"[0-9]+", str(registered_subland)):
        return {"status": "invalid_registered_subland"}

    path = Path(registry_db)
    if not path.is_file():
        return {"status": "dld_snapshot_missing"}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM metadata LIMIT 1").fetchone()
            if row is None:
                return {"status": "dld_snapshot_provenance_missing"}
            meta = json.loads(row[0])
            if not isinstance(meta, dict) or not meta.get("source"):
                return {"status": "dld_snapshot_provenance_missing"}
            # The matching.py materialized snapshot indexes area and building.
            # Project-only rows deliberately cannot establish a tower identity.
            rows = db.execute(
                """SELECT record FROM units WHERE area=? AND building=? LIMIT ?""",
                (norm(registered_zone), norm(registered_tower), MAX_TOWER_ROWS + 1)
            ).fetchall()
            if len(rows) > MAX_TOWER_ROWS:
                return {"status": "tower_scope_exceeds_safe_limit"}
            direct = []
            for (record_json,) in rows:
                record = json.loads(record_json)
                if not isinstance(record, dict):
                    return {"status": "invalid_registry_rows"}
                if (norm(record.get("unit")) == norm(unit)
                        and norm(record.get("building")) == norm(registered_tower)
                        and norm(record.get("area")) == norm(registered_zone)):
                    if registered_land is not None and str(record.get("land_number", "")) != str(registered_land):
                        continue
                    if registered_subland is not None and str(record.get("land_sub_number", "")) != str(registered_subland):
                        continue
                    direct.append(record)
            if not direct:
                return {"status": "unit_missing_in_authoritative_tower_scope"}
            ids = {str(rec.get("property_id") or "") for rec in direct}
            if len(ids) != 1:
                return {"status": "multiple_dld_property_ids_for_unit", "alternatives": len(ids)}
            property_id = next(iter(ids))
            if not VALID_ID.fullmatch(property_id):
                return {"status": "no_canonical_dld_id"}
            same_id = db.execute(
                "SELECT record FROM units WHERE property_id=? LIMIT ?",
                (property_id, MAX_PROPERTY_ID_ROWS + 1)
            ).fetchall()
            if not same_id or len(same_id) > MAX_PROPERTY_ID_ROWS:
                return {"status": "canonical_dld_id_not_unique"}
            # A single numeric property ID must never silently refer to another
            # registered tower, unit, zone or land parcel.
            expected = (norm(unit), norm(registered_tower), norm(registered_zone))
            for (record_json,) in same_id:
                record = json.loads(record_json)
                observed = (norm(record.get("unit")), norm(record.get("building")),
                            norm(record.get("area")))
                if observed != expected:
                    return {"status": "canonical_dld_id_conflicts_elsewhere"}
                if registered_land is not None and str(record.get("land_number", "")) != str(registered_land):
                    return {"status": "canonical_dld_id_land_conflict"}
                if registered_subland is not None and str(record.get("land_sub_number", "")) != str(registered_subland):
                    return {"status": "canonical_dld_id_subland_conflict"}
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return {"status": "unsupported_or_invalid_registry"}

    return {
        "status": "source_reported_unit_dld_registry_correlated",
        "unit_number": unit,
        "canonical_dld_property_id": property_id,
        "source_reference": source_claim.get("reference"),
        "matched_advert_guid": guid,
        "source_authenticity": "not_independently_authenticated",
        "registered_tower": registered_tower,
        "registered_zone": registered_zone,
        "registry_source": meta["source"],
        "registry_snapshot_date": meta.get("as_of"),
        "registry_recent": _snapshot_is_recent(meta.get("as_of")),
        "upstream_property_id": source_claim.get("upstream_property_id"),
        "upstream_id_relation_to_dld": "unproven_not_used_for_match",
        "current_owner": "not_verified",
        "owner_contact": "not_available",
        "verified_exact_advert_identity": False,
        "notice": ("The same source-reported unit number appears in one registered "
                   "tower/zone/land scope. This corroborates the source claim, "
                   "but does not authenticate upstream response or current ownership.")
    }
