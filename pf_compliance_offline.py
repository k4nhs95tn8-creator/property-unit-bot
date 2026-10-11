"""Offline-only inspection of an *already authorised* PF compliance response.

Does NOT use Property Finder's API, fetch pages, generate credentials, contact
DLD, infer a private owner, or equate upstream property.id with the canonical
DLD property_id namespace. The user's existing Compliance API pause remains.
"""
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
import re

_GUID = re.compile(r'^[a-z0-9]{49}$')
_NON_SENSITIVE_REF = re.compile(r'^[A-Za-z0-9_.:-]{5,100}$')


def _guid_from_validation_url(url):
    if not isinstance(url, str):
        return None
    try:
        link = urlsplit(url)
        if (link.scheme != 'https' or link.hostname != 'trakheesi.dubailand.gov.ae'
                or link.path != '/rev/madmoun/listing/validation'
                or link.username or link.password or link.port not in (None, 443)):
            return None
        values = parse_qs(link.query).get('khevJujtDig', [])
        return values[0] if len(values) == 1 and _GUID.fullmatch(values[0]) else None
    except ValueError:
        return None


def _capture_date(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def _text(value):
    if value is None:
        return None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        val = str(value).strip()
        return val if val and val.casefold() not in {'null', 'none', 'undefined', '-1', '0'} else None
    return None


def inspect_capture(capture, advert_guid, *, expected_permit=None,
                    expected_license=None, now=None):
    """Return non-personal, source-attributed unit evidence or fail closed.

    The allowed format is:
      source = authorised_pf_compliance_saved_response
      authorised_for_lookup = yes
      reference = local non-personal reference
      captured_at = timezone-aware ISO 8601 timestamp
      request = {permitNumber, licenseNumber, permitType}
      response = {status:'success', data:[{validationURL, permitNumber, property:{...}}]}

    Important: this *cannot authenticate* that the JSON came from the vendor.
    One shared permit number may refer to several different unit-level GUIDs.
    """
    if not _GUID.fullmatch(str(advert_guid or '')):
        return {'status': 'missing_advert_guid'}
    if not isinstance(capture, dict):
        return {'status': 'invalid_capture'}
    if (capture.get('source') != 'authorised_pf_compliance_saved_response'
            or capture.get('authorised_for_lookup') != 'yes'):
        return {'status': 'not_authorised'}
    reference = capture.get('reference')
    if not isinstance(reference, str) or not _NON_SENSITIVE_REF.fullmatch(reference):
        return {'status': 'missing_nonpersonal_reference'}
    timestamp = _capture_date(capture.get('captured_at'))
    current = now or datetime.now(timezone.utc)
    if timestamp is None or timestamp > current:
        return {'status': 'invalid_capture_time'}
    request = capture.get('request')
    response = capture.get('response')
    if not isinstance(request, dict) or not isinstance(response, dict):
        return {'status': 'missing_request_or_response'}
    permit_number = _text(request.get('permitNumber'))
    license_number = _text(request.get('licenseNumber'))
    if not permit_number or not license_number:
        return {'status': 'missing_request_identity'}
    if expected_permit is not None and permit_number != str(expected_permit):
        return {'status': 'permit_number_conflict'}
    if expected_license is not None and license_number != str(expected_license):
        return {'status': 'licence_conflict'}
    if response.get('status') != 'success':
        return {'status': 'unsuccessful_response'}
    data = response.get('data')
    if not isinstance(data, list) or not data or len(data) > 100:
        return {'status': 'invalid_result_set'}

    # Only a matching individual validation GUID links the response to this ad.
    # Numeric transaction/permit numbers are not unique across ads.
    linked = [item for item in data if isinstance(item, dict)
              and _guid_from_validation_url(item.get('validationURL')) == advert_guid]
    if not linked:
        return {'status': 'no_matching_validation_guid'}
    if len(linked) != 1:
        return {'status': 'ambiguous_matching_validation_guid'}
    item = linked[0]
    if _text(item.get('permitNumber')) != permit_number:
        return {'status': 'permit_number_conflict'}
    expiry = _capture_date(item.get('expiresAt')) if item.get('expiresAt') else None
    if item.get('expiresAt') and expiry is None:
        return {'status': 'invalid_expiration_date'}
    if expiry and expiry <= current:
        return {'status': 'expired_source_record'}
    property_data = item.get('property')
    if not isinstance(property_data, dict):
        return {'status': 'no_property_data'}
    if _text(property_data.get('listingType')) not in {'Unit', 'unit'}:
        return {'status': 'not_unit_level_permit'}
    unit_number = _text(property_data.get('unitNumber'))
    if not unit_number or not re.fullmatch(r'[A-Za-z0-9/-]{1,30}', unit_number):
        return {'status': 'no_explicit_unit_number'}
    upstream_id = _text(property_data.get('id'))
    if upstream_id and not re.fullmatch(r'\d{1,20}', upstream_id):
        return {'status': 'unrecognised_upstream_id'}
    return {
        'status': 'source_reported_unit',
        'unit_number': unit_number,
        'upstream_property_id': upstream_id,
        'upstream_namespace': 'pf_compliance_property_id_unproven_dld_equivalence',
        'permit_number': permit_number,
        'license_number': license_number,
        'listing_guid': advert_guid,
        'reference': reference,
        'captured_at': timestamp.isoformat(),
        'source_authenticity': 'operator_attested_not_independently_authenticated',
        'current_owner': 'not_provided',
        'notice': 'Unit reported by authorised offline compliance JSON. Not proof of canonical DLD ID or current registered ownership.'
    }
