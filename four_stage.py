"""Kuku's offline-first, evidence-led four-stage Dubai advert research pipeline.

Stages: (1) authorised/public advert ingest, (2) DLD snapshot candidates,
(3) independently supplied Madmoun permit consistency, (4) cautious dedup.

This does NOT crack a hidden permit-to-unit crosswalk. It never uses a known
answer as input or labels a match proven merely because its rank is high.
Standard library only. No external requests unless `fetch_public_listing` is
explicitly called (or CLI --fetch-public is used).
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sqlite3
from contextlib import closing

from permit_links import lookup as lookup_permit_link
from owner_records import lookup_authorised_contacts
from identity_evidence import lookup_claim as lookup_document_claim
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SQM_PER_SQFT = 0.09290304
MAX_CANDIDATES = 25000
GUID_RE = re.compile(r'^[a-z0-9]{40,80}$')


def norm(value):
    import unicodedata
    return ' '.join(re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', str(value or '')).casefold()).split())


def number(value):
    try:
        val = float(str(value).replace(',', ''))
    except (ValueError, TypeError):
        return None
    return val if math.isfinite(val) and val > 0 else None


def bedroom_count(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 <= value <= 20 else None
    x = norm(value)
    if x in ('studio', '0', '0 br', '0 b r'):
        return 0
    m = re.match(r'^(\d{1,2})(?:\s+bed(?:room)?s?|\s+b\s*r|\s+rooms?)?(?:\s+maid)?$', x)
    return int(m.group(1)) if m else None


def canonical_url(value):
    """Strict URL allowlist. Tracking parameters are deliberately discarded."""
    try:
        u = urlsplit(str(value).strip())
        host = (u.hostname or '').lower()
        if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
            raise ValueError()
    except ValueError:
        raise ValueError('Only public HTTPS Property Finder, Bayut or Dubizzle advert URLs are accepted.') from None
    path = u.path
    if host in ('propertyfinder.ae', 'www.propertyfinder.ae'):
        portal = 'propertyfinder'
        if not re.fullmatch(r'/(en|ar)/(plp|buy|rent)/[a-zA-Z0-9_/-]+\.html', path):
            raise ValueError('That is not a Property Finder property advert URL.')
        host = 'www.propertyfinder.ae'
    elif host in ('bayut.com', 'www.bayut.com'):
        portal = 'bayut'
        if not re.fullmatch(r'/property/details-\d+\.html/?', path):
            raise ValueError('That is not a Bayut property detail URL.')
        host = 'www.bayut.com'
    elif host == 'dubizzle.com' or host.endswith('.dubizzle.com'):
        portal = 'dubizzle'
        if not (re.search(r'/(?:en/)?(?:property-for-(?:sale|rent)|properties-for-(?:sale|rent)|property/)', path)
                and len(path.strip('/').split('/')) >= 3):
            raise ValueError('That is not a Dubizzle property advert URL.')
    else:
        raise ValueError('Only Property Finder, Bayut and Dubizzle advert domains are allowed.')
    return portal, urlunsplit(('https', host, path.rstrip('/'), '', ''))


def guid_from_url(value):
    if not value:
        return None
    try:
        p = urlsplit(str(value).replace('\\&', '&'))
        if p.scheme != 'https' or p.hostname != 'trakheesi.dubailand.gov.ae' or 'madmoun/listing/validation' not in p.path:
            return None
        guid = parse_qs(p.query).get('khevJujtDig', [None])[0]
        return guid if guid and GUID_RE.fullmatch(guid) else None
    except ValueError:
        return None


def _regulatory_zone(details):
    for row in details or []:
        if isinstance(row, dict) and row.get('id') == 'regulatory_zone_name':
            return (row.get('values') or {}).get('primary', {}).get('value', {}).get('localized_value', {}).get('en')
    return None


def _explicit_unit_disclosure(text):
    """Return an explicitly labelled unit from the advert body, never from URLs."""
    if not isinstance(text, str):
        return None
    m = re.search(r'(?im)(?:^|[\n;•])\s*(?:unit\s*(?:no\.?|number)|'
                  r'apartment\s*(?:no\.?|number))\s*[:#-]\s*([A-Za-z0-9/-]{1,20})(?=$|[\s;,.)])', text)
    return m.group(1) if m else None


def _description_area(text):
    if not isinstance(text, str):
        return None
    match = re.search(r'\b([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft\.?|ft²|square\s*feet)\b', text, re.I)
    return number(match.group(1)) * SQM_PER_SQFT if match and number(match.group(1)) else None


def _compact_profile(value):
    """Drop owner/contact/agent names, email, phone and unrelated nested data."""
    permitted = {'url', 'portal', 'building', 'project', 'area', 'bedrooms', 'size_sqm',
                 'description_size_sqm', 'advertised_rent', 'reference', 'listing_id',
                 'permit_guid', 'unit_disclosure', 'source', 'captured_at'}
    return {k: value[k] for k in permitted if k in value and value[k] is not None}


def ingest_advert(payload, *, url=None):
    """Accept a PF JSON capture, a compact authorised record or general public advert metadata."""
    if not isinstance(payload, dict):
        raise ValueError('Advert input must be a JSON object.')
    if 'extracted_property_data' in payload:
        url = url or payload.get('listing_url')
        p = payload['extracted_property_data']
    elif isinstance(payload.get('props'), dict):
        p = payload.get('props', {}).get('pageProps', {}).get('propertyResult', {}).get('property')
    elif isinstance(payload.get('facts'), dict):
        p = payload['facts']
    else:
        p = payload
    if not isinstance(p, dict):
        raise ValueError('The capture contains no advert property record.')
    url = url or payload.get('url') or payload.get('listing_url') or p.get('share_url')
    portal, canonical = canonical_url(url)
    location = p.get('location') if isinstance(p.get('location'), dict) else {}
    project = p.get('project') if isinstance(p.get('project'), dict) else {}
    rera = p.get('rera') if isinstance(p.get('rera'), dict) else {}
    size_value = p.get('size')
    size = None
    if isinstance(size_value, dict):
        quantity = number(size_value.get('value'))
        if quantity:
            size = quantity * (SQM_PER_SQFT if str(size_value.get('unit', 'sqft')).lower() in ('sqft', 'ft2') else 1)
    if size is None:
        size = number(p.get('size_sqm'))
    if size is None and p.get('size_sqft'):
        val = number(p['size_sqft'])
        size = val * SQM_PER_SQFT if val else None
    bedrooms = bedroom_count(p.get('bedrooms'))
    if bedrooms is None:
        bedrooms = bedroom_count(p.get('bedrooms_value'))
    guide = rera.get('permit_validation_url') or p.get('permit_validation_url')
    guid = guid_from_url(guide) or p.get('permit_guid')
    if guid and not GUID_RE.fullmatch(str(guid)):
        guid = None
    area = p.get('area') or _regulatory_zone(p.get('regulatory_details'))
    # PF location name is a building; the marketing location is NOT the DLD area.
    building = p.get('building') or (location.get('name') if location.get('type') == 'TOWER' else None)
    project_name = p.get('project') if isinstance(p.get('project'), str) else project.get('title')
    rent = p.get('advertised_rent')
    if rent is None and isinstance(p.get('price'), dict):
        rent = p['price'].get('value')
    ref = p.get('reference')
    profile = _compact_profile({
        'url': canonical, 'portal': portal, 'building': building, 'project': project_name,
        'area': area, 'bedrooms': bedrooms, 'size_sqm': size,
        'description_size_sqm': _description_area(p.get('description') or p.get('description_original')),
        'advertised_rent': number(rent), 'reference': str(ref) if ref else None,
        'listing_id': str(p.get('listing_id') or p.get('id') or '') or None,
        'permit_guid': guid, 'unit_disclosure': _explicit_unit_disclosure(p.get('description') or p.get('description_original')),
        'source': 'submitted_public_capture',
        'captured_at': payload.get('captured_at') or datetime.now(timezone.utc).isoformat(),
    })
    if not profile.get('building') and not profile.get('project'):
        # Do not infer the precise tower from keywords inside a marketing URL.
        profile['ingest_warning'] = 'No explicit building or project was present in this capture.'
    return profile


class _PublicScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.active = None
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        if tag != 'script':
            return
        attr = dict(attrs)
        if attr.get('id') == '__NEXT_DATA__' or attr.get('type') == 'application/ld+json':
            self.active = 'next' if attr.get('id') == '__NEXT_DATA__' else 'jsonld'
            self.buffer = []

    def handle_data(self, data):
        if self.active and sum(map(len, self.buffer)) < 4_000_000:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            self.scripts.append((self.active, ''.join(self.buffer)))
            self.active, self.buffer = None, []


def _jsonld_profile(raw, url):
    """Conservatively parse public Schema.org property attributes, if present."""
    nodes = []
    def visit(item, depth=0):
        if depth > 3 or len(nodes) > 100:
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            nodes.append(item)
            for field in ('@graph', 'mainEntity', 'itemOffered'):
                if isinstance(item.get(field), (dict, list)):
                    visit(item[field], depth + 1)
    visit(raw)
    for item in nodes:
        types = item.get('@type') or []
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or not any(x in types for x in
            ('Apartment', 'House', 'Residence', 'RealEstateListing', 'SingleFamilyResidence')):
            continue
        offered = item.get('itemOffered') if isinstance(item.get('itemOffered'), dict) else item
        address = offered.get('address') if isinstance(offered.get('address'), dict) else {}
        building = offered.get('building')
        if isinstance(building, dict):
            building = building.get('name')
        if not building:
            building = item.get('building')
        if not building and isinstance(address.get('streetAddress'), str):
            street = address['streetAddress'].strip()
            if re.search(r'(?i)\b(?:tower|residence|building|apartments?)\b', street):
                building = street
        size = offered.get('floorSize')
        sqm = None
        if isinstance(size, dict):
            value = number(size.get('value'))
            unit = size.get('unitCode') or size.get('unitText') or ''
            if value and str(unit).upper() in ('SQFT', 'FTK', 'SQ FT', 'SQUARE FEET'):
                sqm = value * SQM_PER_SQFT
            elif value and str(unit).upper() in ('MTK', 'SQM', 'M2', 'SQUARE METRES'):
                sqm = value
        rooms_value = offered.get('numberOfBedrooms') or offered.get('numberOfRooms')
        bedrooms = bedroom_count(rooms_value)
        if not (building or sqm or bedrooms is not None):
            continue
        advert = {'url': url, 'building': building if isinstance(building, str) else None,
                  'project': (offered.get('isPartOf') or {}).get('name') if isinstance(offered.get('isPartOf'), dict) else None,
                  'size_sqm': sqm, 'bedrooms': bedrooms,
                  'description': offered.get('description') or item.get('description')}
        profile = ingest_advert(advert, url=url)
        profile['source'] = 'public_schema_org_jsonld'
        return profile
    return None


def parse_public_html(content, url):
    parser = _PublicScripts()
    parser.feed(content)
    for kind, value in parser.scripts:
        if kind == 'next':
            try:
                return ingest_advert(json.loads(html.unescape(value)), url=url)
            except (ValueError, TypeError):
                continue
    for kind, value in parser.scripts:
        if kind == 'jsonld':
            try:
                profile = _jsonld_profile(json.loads(html.unescape(value)), url)
            except (ValueError, TypeError, AttributeError):
                continue
            if profile:
                return profile
    raise ValueError('No usable public property record was exposed in the page. Supply a permitted manual capture.')


class _SamePortalRedirect(HTTPRedirectHandler):
    def __init__(self, portal):
        self.portal = portal

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        p, _ = canonical_url(newurl)
        if p != self.portal:
            raise ValueError('Redirect left the property portal; no request made.')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch_public_listing(url):
    """One user-requested public HTML GET; never login, use cookies or bypass challenges."""
    portal, canonical = canonical_url(url)
    opener = build_opener(_SamePortalRedirect(portal))
    req = Request(canonical, headers={'User-Agent': 'KukuPropertyResearch/0.1', 'Accept': 'text/html'})
    try:
        with opener.open(req, timeout=12) as resp:
            if 'text/html' not in resp.headers.get('Content-Type', ''):
                raise ValueError('Portal did not serve public HTML.')
            body = resp.read(4_000_001)
            if len(body) > 4_000_000:
                raise ValueError('Public page exceeded the safe size limit.')
    except (HTTPError, URLError, TimeoutError) as err:
        # No retry, private API, CAPTCHA or alternate-host probe.
        code = getattr(err, 'code', 'unavailable')
        raise ValueError(f'Public listing not accessible (HTTP/network {code}); supply a manual capture.') from None
    return parse_public_html(body.decode('utf-8', 'replace'), canonical)


def _permit_field(text, name, pattern=r'([^\r\n]+)'):
    m = re.search(re.escape(name) + r'\s*:?[ \t]*' + pattern, text, re.I)
    return m.group(1).strip() if m else None


def ingest_permit(payload):
    """Read a user-copied public Trakheesi record. Never contact private APIs."""
    if isinstance(payload, dict):
        # A compact, already-parsed copy is still *user-supplied* evidence, not
        # proof of authenticated upstream origin.
        if payload.get('source') == 'user_supplied_trakheesi_capture' and 'transaction_number' in payload:
            keys = ('guid', 'transaction_number', 'listing_number', 'property_number',
                    'property_unit_number', 'permit_property_id',
                    'property_id_claim', 'floor', 'building', 'area', 'bedrooms',
                    'size_sqm', 'asking_aed', 'licence', 'source')
            return {k: payload[k] for k in keys if k in payload}
        if isinstance(payload.get('property_records'), list):
            records = [r.get('property_record') for r in payload['property_records'] if isinstance(r, dict) and isinstance(r.get('property_record'), dict)]
            if len(records) != 1:
                raise ValueError('Permit capture must contain exactly one property record.')
            payload = records[0]
        if isinstance(payload.get('Response'), dict):
            payload = payload['Response']
        if not ('PermitNumber' in payload or 'ListingGuid' in payload or 'ListingGUID' in payload):
            raise ValueError('This does not contain a recognisable Trakheesi permit record.')
        get = payload.get
        response = {
            'guid': get('ListingGuid') or get('ListingGUID'),
            'transaction_number': str(get('PermitNumber')) if get('PermitNumber') not in (None, '') else None,
            'listing_number': str(get('ListingNumber')) if get('ListingNumber') not in (None, '') else None,
            'property_number': str(get('PropertyNumber')) if get('PropertyNumber') not in (None, '') else None,
            # These identifiers inhabit different namespaces. PermitPropertyId
            # is NOT a canonical DLD property_id merely because it is numeric.
            'property_unit_number': str(get('PropertyUnitNumber')) if get('PropertyUnitNumber') not in (None, '') else None,
            'permit_property_id': str(get('PermitPropertyId') or get('PermitPropertyID')) if (get('PermitPropertyId') or get('PermitPropertyID')) not in (None, '', -1, '-1', 0, '0') else None,
            'property_id_claim': str(get('PropertyId')) if get('PropertyId') not in (None, -1, '-1', '', 0) else None,
            'floor': str(get('FloorNumber')) if get('FloorNumber') not in (None, '') else None,
            'building': get('BuildingNameEn') or get('PropertyNameEn'),
            'area': get('ZoneNameEn'), 'bedrooms': bedroom_count(get('RoomTypeEn') or get('RoomsCount')),
            'size_sqm': number(get('PropertySize')), 'asking_aed': number(get('PropertyValue')),
            'licence': str(get('LicenseNumber')) if get('LicenseNumber') else None,
        }
    elif isinstance(payload, str):
        text = payload
        # Markdown Link text may expose the GUID only in the report-violation URL.
        m = re.search(r'https://(?:www\.)?dubailand\.gov\.ae/[^\s)\]>]*[?&]listingGuid=([a-z0-9]{40,80})', text, re.I)
        response = {
            'guid': m.group(1) if m else None,
            'transaction_number': _permit_field(text, 'Transaction Number', r'(\d{3,12})'),
            'listing_number': _permit_field(text, 'Listing Number', r'(\d{5,16})'),
            'property_number': _permit_field(text, 'Property Number', r'([A-Za-z0-9-]{1,25})'),
            'property_unit_number': _permit_field(text, 'Property Unit Number', r'([A-Za-z0-9/-]{1,25})'),
            'permit_property_id': _permit_field(text, 'Permit Property ID', r'([0-9]{1,20})'),
            'property_id_claim': None,
            'floor': _permit_field(text, 'Floor Number', r'(\d{1,3})'),
            'building': _permit_field(text, 'Building Name'),
            'area': _permit_field(text, 'Zone Name'),
            'bedrooms': bedroom_count(_permit_field(text, 'Room Type') or _permit_field(text, 'Rooms Count')),
            'size_sqm': number(_permit_field(text, 'Property Size(Sqm)', r'([\d.,]+)')),
            'asking_aed': number(_permit_field(text, 'Property Value(AED)', r'([\d.,]+)')),
            'licence': _permit_field(text, 'License #', r'(\d{3,15})'),
        }
        if not response['transaction_number']:
            raise ValueError('No transaction number was found in the copied permit card.')
    else:
        raise ValueError('A permit capture must be JSON or copied page text.')
    guid = response.get('guid')
    if guid and not GUID_RE.fullmatch(str(guid)):
        raise ValueError('Malformed listing GUID in permit capture.')
    response['source'] = 'user_supplied_trakheesi_capture'
    return response


def corroborate(ad, permit):
    """Only an exact shared GUID links a submitted permit record to an advert."""
    if not permit:
        return {'status': 'not_supplied', 'discrepancies': []}
    if not ad.get('permit_guid') or not permit.get('guid'):
        return {'status': 'unlinked', 'discrepancies': ['No identical GUID available for an advert-to-permit join.']}
    if ad['permit_guid'] != permit['guid']:
        return {'status': 'mismatch', 'discrepancies': ['Advert and permit GUID do not match.']}
    issues = []
    if ad.get('building') and permit.get('building') and norm(ad['building']) != norm(permit['building']):
        issues.append('Advert and permit buildings differ.')
    if ad.get('bedrooms') is not None and permit.get('bedrooms') is not None and ad['bedrooms'] != permit['bedrooms']:
        issues.append('Advert and permit bedroom counts differ.')
    if ad.get('size_sqm') and permit.get('size_sqm'):
        if abs(ad['size_sqm'] - permit['size_sqm']) > max(1, .02 * ad['size_sqm']):
            issues.append('Advert and permit area differs by more than the comparison tolerance.')
    if ad.get('advertised_rent') and permit.get('asking_aed'):
        if ad['advertised_rent'] != permit['asking_aed']:
            issues.append('Advert and permit advertised values differ.')
    return {'status': 'conflict' if issues else 'guid_correlated', 'discrepancies': issues}


def _registry_records(db_path, ad, permit, permit_correlation):
    if not Path(db_path).is_file():
        return {'status': 'data_unavailable', 'candidates': [], 'count': 0, 'scope': 'unknown', 'snapshot': None}
    building = norm(ad.get('building'))
    project = norm(ad.get('project') or ad.get('building'))
    if not (building or project):
        return {'status': 'needs_building', 'candidates': [], 'count': 0, 'scope': 'unknown', 'snapshot': None}
    area = norm(permit.get('area') if permit_correlation == 'guid_correlated' and permit and permit.get('area') else ad.get('area'))
    database = f'file:{Path(db_path).resolve()}?mode=ro'
    try:
        with closing(sqlite3.connect(database, uri=True)) as db:
            db.row_factory = sqlite3.Row
            meta = db.execute('SELECT value FROM metadata').fetchone()
            if not meta:
                return {'status': 'data_unavailable', 'candidates': [], 'count': 0, 'scope': 'unknown', 'snapshot': None}
            snapshot = json.loads(meta['value'])
            sql = "SELECT record FROM units WHERE ((building<>'' AND building=?) OR (project<>'' AND project=?))"
            params = [building, project]
            if area:
                sql += " AND (area=? OR area='')"
                params.append(area)
            sql += ' LIMIT ?'
            params.append(MAX_CANDIDATES + 1)
            rows = [json.loads(row['record']) for row in db.execute(sql, params)]
    except (sqlite3.Error, ValueError):
        return {'status': 'data_unavailable', 'candidates': [], 'count': 0, 'scope': 'unknown', 'snapshot': None}
    if len(rows) > MAX_CANDIDATES:
        return {'status': 'too_broad', 'candidates': [], 'count': None, 'scope': 'project', 'snapshot': snapshot}
    # Deduplicate identical database rows while retaining genuinely different records.
    records = list({json.dumps(r, sort_keys=True): r for r in rows}.values())
    scope = 'project_may_span_buildings' if not building or any(not r.get('building') or norm(r.get('building')) != building for r in records) else 'building'
    # Area is a mutable descriptor, not an identity key. DLD snapshots, permit
    # cards and adverts can legitimately disagree (e.g., net versus inclusive
    # area). Retain bedroom-compatible records as possible rivals; only mark
    # close-area matches as the primary ranked candidates.
    candidates = []
    retained_candidates = []
    area_discrepancy_count = 0
    for row in records:
        if ad.get('bedrooms') is not None and row.get('bedrooms') is not None:
            if bedroom_count(row['bedrooms']) != ad['bedrooms']:
                continue
        actual = number(row.get('size_sqm'))
        area_discrepancy = bool(ad.get('size_sqm') and actual and
                                abs(actual - ad['size_sqm']) > max(1.0, ad['size_sqm'] * .02))
        score = 0
        score += 5 if building and norm(row.get('building')) == building else 0
        score += 3 if area and norm(row.get('area')) == area else 0
        score += 2 if ad.get('bedrooms') is not None and row.get('bedrooms') is not None else 0
        if actual and ad.get('size_sqm'):
            score += round(max(0.0, 2.0 - abs(actual - ad['size_sqm'])), 4)
        candidate = dict(row, diagnostic_score=score, area_discrepancy=area_discrepancy)
        retained_candidates.append(candidate)
        if area_discrepancy:
            area_discrepancy_count += 1
        else:
            candidates.append(candidate)
    sort_key = lambda x: (-x['diagnostic_score'], str(x.get('unit') or ''), str(x.get('property_id') or ''))
    candidates.sort(key=sort_key)
    retained_candidates.sort(key=sort_key)
    return {'status': 'candidates_only' if retained_candidates else 'no_compatible_rows',
            'candidates': candidates, 'retained_candidates': retained_candidates,
            'count': len(candidates), 'retained_count': len(retained_candidates),
            'area_discrepancy_count': area_discrepancy_count,
            'scope': scope, 'records_examined': len(records), 'snapshot': {
                'source': snapshot.get('source'), 'as_of': snapshot.get('as_of'), 'rows': snapshot.get('rows'),
                'coverage': snapshot.get('coverage')}}


def _registry_id_unambiguous(db_path, property_id, unit):
    """A DLD property ID must not be reused for conflicting registered units."""
    if not property_id:
        return False
    try:
        url = f'file:{Path(db_path).resolve()}?mode=ro'
        with closing(sqlite3.connect(url, uri=True)) as db:
            entries = db.execute('SELECT record FROM units WHERE property_id=? LIMIT 100',
                                 (str(property_id),)).fetchall()
        if not entries or len(entries) >= 100:
            return False
        identities = {(norm(json.loads(r[0]).get('unit')),
                       norm(json.loads(r[0]).get('project') or json.loads(r[0]).get('building')),
                       norm(json.loads(r[0]).get('area'))) for r in entries}
        return len(identities) == 1 and next(iter(identities))[0] == norm(unit)
    except (sqlite3.Error, ValueError, TypeError):
        return False


def _identity_from_explicit_advert(ad, registry):
    """Published self-disclosures are evidence, but not DLD-backed identity proof."""
    unit = ad.get('unit_disclosure')
    if not unit or registry['status'] != 'candidates_only':
        return {'status': 'not_established'}
    matched = [row for row in registry.get('retained_candidates', registry['candidates']) if norm(row.get('unit')) == norm(unit)]
    if len(matched) != 1:
        return {'status': 'not_established', 'reason': 'Advert disclosure not unique in registry candidate scope.'}
    row = matched[0]
    return {'status': 'advert_self_disclosure', 'unit': row.get('unit'),
            'property_id': row.get('property_id'), 'source': 'advert_description',
            'warning': 'The advertiser disclosed a unit number; Kuku has not authenticated the underlying property link.'}


def _identity_from_crosswalk(ad, permit, registry, crosswalk_path, db_path):
    """Require exact GUID, compatible permit transaction, registry ID and unit."""
    guid = ad.get('permit_guid')
    link = lookup_permit_link(crosswalk_path, guid)
    if not link or registry['status'] != 'candidates_only':
        return {'status': 'not_established'}
    if permit and permit.get('guid') == guid and permit.get('transaction_number'):
        if link.get('transaction_number') and link['transaction_number'] != permit['transaction_number']:
            return {'status': 'not_established', 'reason': 'Crosswalk transaction conflicts with the captured permit.'}
    if ad.get('building') and norm(link['building']) != norm(ad['building']):
        return {'status': 'not_established', 'reason': 'Crosswalk building conflicts with the advert.'}
    compatible = [row for row in registry.get('retained_candidates', registry['candidates']) if
                  str(row.get('property_id')) == link['property_id'] and
                  norm(row.get('unit')) == norm(link['unit_number'])]
    if len(compatible) != 1:
        return {'status': 'not_established', 'reason': 'Crosswalk does not identify one compatible registry record.'}
    row = compatible[0]
    if not _registry_id_unambiguous(db_path, link['property_id'], row.get('unit')):
        return {'status': 'not_established', 'reason': 'DLD property ID is ambiguous in the snapshot.'}
    if permit:
        direct_unit = permit.get('property_unit_number')
        legacy_number = permit.get('property_number')
        if direct_unit and legacy_number and norm(direct_unit) != norm(legacy_number):
            return {'status': 'not_established', 'reason': 'Permit fields disagree or have different property-number namespaces.'}
        if (direct_unit or legacy_number) and norm(direct_unit or legacy_number) != norm(row.get('unit')):
            return {'status': 'not_established', 'reason': 'Crosswalk contradicts explicit permit unit number.'}
    if permit and permit.get('property_id_claim') and permit['property_id_claim'] != link['property_id']:
        return {'status': 'not_established', 'reason': 'Crosswalk contradicts explicit permit property ID.'}
    return {'status': 'external_crosswalk_claim', 'unit': row.get('unit'),
            'property_id': row.get('property_id'), 'evidence_source': link['source_name'],
            'evidence_reference': link['source_reference'], 'observed_date': link['observed_date'],
            'warning': 'Matching authorized external source claim; origin not independently authenticated by this tool.'}


def _identity_from_permit(permit, correlation, registry, db_path):
    """Only an explicit permit field can identify a unit; no score-based promotion."""
    if not permit or correlation != 'guid_correlated' or registry['status'] != 'candidates_only':
        return {'status': 'not_established'}
    direct_unit = permit.get('property_unit_number')
    legacy_number = permit.get('property_number')
    # A conflict is not evidence that the fields are interchangeable. Abstain
    # until their namespace semantics are independently established.
    if direct_unit and legacy_number and norm(direct_unit) != norm(legacy_number):
        return {'status': 'not_established', 'reason': 'Different permit property-number namespaces; unit not proven.'}
    unit, pid = direct_unit or legacy_number, permit.get('property_id_claim')
    if not unit and not pid:
        return {'status': 'not_established'}
    matching = [r for r in registry.get('retained_candidates', registry['candidates']) if
                (not unit or norm(r.get('unit')) == norm(unit)) and
                (not pid or str(r.get('property_id')) == str(pid))]
    if len(matching) != 1:
        return {'status': 'not_established', 'reason': 'Permit property number does not match a unique compatible registry row.'}
    row = matching[0]
    if pid and not _registry_id_unambiguous(db_path, pid, row.get('unit')):
        return {'status': 'not_established', 'reason': 'Permit property ID not unique in registry.'}
    if permit.get('floor') and str(row.get('floor', '')) != str(permit['floor']):
        return {'status': 'not_established', 'reason': 'Permit floor conflicts with registry row.'}
    return {'status': 'permit_record_claim', 'unit': row.get('unit'), 'property_id': row.get('property_id'),
            'warning': 'The copied permit explicitly identifies this unit; its origin has not been independently authenticated by Kuku.'}


def _store_and_correlate(path, ad):
    """Deduplicate by exact GUID, never by price/size alone. Property identity is unchanged."""
    if not path:
        return {'same_permit': [], 'possible_relists': [], 'notice': 'Local dedup history not enabled.'}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS adverts
            (url TEXT PRIMARY KEY, portal TEXT, guid TEXT, building TEXT, area TEXT,
             bedrooms INTEGER, size_sqm REAL, observed_at TEXT)''')
        others = db.execute('SELECT url,portal,guid,building,area,bedrooms,size_sqm FROM adverts WHERE url<>?',
                            (ad['url'],)).fetchall()
        db.execute('''INSERT INTO adverts(url,portal,guid,building,area,bedrooms,size_sqm,observed_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
            portal=excluded.portal,guid=excluded.guid,building=excluded.building,
            area=excluded.area,bedrooms=excluded.bedrooms,size_sqm=excluded.size_sqm,
            observed_at=excluded.observed_at''',
                   (ad['url'], ad['portal'], ad.get('permit_guid'), ad.get('building'), ad.get('area'),
                    ad.get('bedrooms'), ad.get('size_sqm'), datetime.now(timezone.utc).isoformat()))
    same = []
    possible = []
    for url, portal, guid, building, area, beds, size in others:
        if ad.get('permit_guid') and guid == ad['permit_guid']:
            same.append(url)
        elif (norm(ad.get('building')) and norm(ad.get('building')) == norm(building)
              and ad.get('bedrooms') is not None and ad['bedrooms'] == beds
              and ad.get('size_sqm') and size and abs(size - ad['size_sqm']) <= max(1.0, .02 * size)):
            possible.append(url)
    return {'same_permit': sorted(same), 'possible_relists': sorted(possible),
            'notice': 'Same GUID means same validation token, not necessarily a uniquely identified DLD unit.'}


def analyze(ad_payload, db_path, *, permit_payload=None, cache_path=None, crosswalk_path=None,
            owner_db=None, include_owner=False, url=None, max_returned=12,
            document_db=None):
    """Return a machine-readable audit, including abstention and candidate ambiguity."""
    advert = ingest_advert(ad_payload, url=url)
    permit = ingest_permit(permit_payload) if permit_payload is not None else None
    connection = corroborate(advert, permit)
    effective_permit = permit if connection['status'] == 'guid_correlated' else None
    # Refuse external crosswalk promotion when an explicitly supplied permit conflicts.
    if permit_payload is not None and connection['status'] != 'guid_correlated':
        crosswalk_path = None
    registry = _registry_records(db_path, advert, effective_permit, connection['status'])
    identity = _identity_from_permit(effective_permit, connection['status'], registry, db_path)
    # Crosswalk claims and advert self-disclosures never become independently verified
    # solely because the selected registry row is unique.
    if identity['status'] == 'not_established':
        identity = _identity_from_crosswalk(advert, effective_permit, registry, crosswalk_path, db_path)
    if identity['status'] == 'not_established':
        identity = _identity_from_explicit_advert(advert, registry)
    # A separately authorised listing-specific document may corroborate a
    # registered unit. Never use this to invent a GUID, property_id or owner.
    document = {'status': 'not_supplied'}
    if document_db and advert.get('permit_guid') and registry['status'] == 'candidates_only':
        document = lookup_document_claim(advert['url'], document_db, db_path)
        if document.get('status') == 'document_claim_registry_corroborated':
            claimed_guid = document.get('listing_guid')
            conflicts = (
                claimed_guid != advert['permit_guid']
                or (permit_payload is not None and connection['status'] != 'guid_correlated')
                or (effective_permit and effective_permit.get('property_unit_number')
                    and norm(effective_permit['property_unit_number']) != norm(document['unit_number']))
                or (effective_permit and effective_permit.get('property_number')
                    and norm(effective_permit['property_number']) != norm(document['unit_number']))
                or (effective_permit and effective_permit.get('property_id_claim')
                    and str(effective_permit['property_id_claim']) != str(document['dld_property_id']))
                or (advert.get('building') and norm(advert['building']) != norm(document['building']))
                or (advert.get('area') and norm(advert['area']) != norm(document['area']))
            )
            if conflicts:
                document = {'status': 'document_claim_conflicts_with_advert_or_permit'}
            else:
                rows = [r for r in registry.get('retained_candidates', registry['candidates'])
                        if str(r.get('property_id')) == str(document['dld_property_id'])
                        and norm(r.get('unit')) == norm(document['unit_number'])]
                if len(rows) == 1 and _registry_id_unambiguous(db_path,
                        document['dld_property_id'], document['unit_number']):
                    identity = {
                        'status': 'authorised_document_claim',
                        'unit': document['unit_number'],
                        'property_id': document['dld_property_id'],
                        'evidence_source': document['evidence_type'],
                        'evidence_reference': document['evidence_reference'],
                        'observed_date': document['observed_date'],
                        'warning': 'Local authorised document claim agrees with the registry, not independent verification of current ownership.'
                    }
                else:
                    document = {'status': 'document_claim_not_unique_in_candidate_scope'}
    dedup = _store_and_correlate(cache_path, advert)
    owner = {'status': 'not_requested_or_no_independent_unit_link'}
    # A source report does not establish current registered ownership.
    # Release contacts only with *matching* property-specific owner consent and
    # an explicitly supplied, corroborated document evidence reference.
    if owner_db and include_owner:
        if identity.get('status') == 'authorised_document_claim':
            owner = lookup_authorised_contacts(
                owner_db, identity['property_id'], identity['evidence_reference'],
                authorised=True)
        else:
            owner = {'status': 'current_owner_not_independently_verified'}
    # Only allowlisted aggregate candidate data leaves the engine.
    candidates = registry.pop('candidates', [])
    retained = registry.pop('retained_candidates', [])
    # Preserve records eliminated only by a conflicting area measurement.
    # These are explicitly NOT verified units or high-confidence predictions.
    registry['top_retained_area_alternatives'] = [
        {k: item[k] for k in ('unit', 'property_id', 'floor', 'size_sqm', 'bedrooms', 'diagnostic_score', 'area_discrepancy') if k in item}
        for item in retained if item.get('area_discrepancy')
    ][:max_returned]
    registry['top_candidates'] = [
        {k: item[k] for k in ('unit', 'property_id', 'floor', 'size_sqm', 'bedrooms', 'diagnostic_score') if k in item}
        for item in candidates[:max_returned]
    ]
    registry['top_candidates_note'] = 'Rankings show descriptor similarity, NOT identification confidence or probabilities.'
    return {'schema_version': 1, 'advert': _compact_profile(advert),
            'permit_check': connection, 'permit': permit,
            'registry': registry, 'identity': identity, 'document': document, 'dedup': dedup, 'owner': owner,
            'final_status': {'permit_record_claim': 'permit_explicit_unverified',
                             'external_crosswalk_claim': 'crosswalk_source_claim',
                             'authorised_document_claim': 'document_source_claim',
                             'advert_self_disclosure': 'advert_self_disclosure_unverified'}.get(identity['status'], 'unresolved'),
            'deployment_ready': False}


def message(report):
    advert, p, r, i, dedup = [report[k] for k in ('advert', 'permit_check', 'registry', 'identity', 'dedup')]
    lines = ['Kuku four-stage research (NOT an exact-unit guarantee)',
             f"Advert: {advert['url']}",
             f"Stage 1 — {advert['portal']} public/authorised capture; GUID: {'present' if advert.get('permit_guid') else 'not captured'}",
             f"Stage 2 — {r['status']}; compatible registry rows: {r.get('count', 'unavailable')}; scope: {r.get('scope')}",
             f"Stage 3 — permit: {p['status']}",
             f"Stage 4 — {len(dedup['same_permit'])} same-GUID adverts; {len(dedup['possible_relists'])} possible relists (not verified)"]
    if r.get('area_discrepancy_count'):
        lines.append(
            f"Area discrepancy: {r['area_discrepancy_count']} registered-unit rivals were retained "
            f"despite area differences; retained rivals: {r.get('retained_count', 0)}. "
            "These are NOT independently identified units.")
    if p['discrepancies']:
        lines.append('Permit checks: ' + '; '.join(p['discrepancies']))
    if i['status'] in {'permit_record_claim', 'external_crosswalk_claim', 'advert_self_disclosure', 'authorised_document_claim'}:
        lines.append(f"Source-reported unit {i['unit']} (registry ID {i['property_id']}); not independently authenticated.")
        if i.get('evidence_source'):
            lines.append(f"Recorded source: {i['evidence_source']} ({i['evidence_reference']}).")
    else:
        lines.append('EXACT UNIT NOT VERIFIED — no independently supported permit-to-property join.')
    owner = report.get('owner', {})
    if owner.get('status') == 'authorised_source_reported_contacts':
        lines.append('CONSENTED PROPERTY CONTACTS (NOT verified current title owners):')
        for contact in owner['contacts'][:10]:
            lines.append(f"Name: {contact['contact_name']} — {contact['contact_role']}")
            lines.append(f"Phone: {contact['contact_phone']} — Email: {contact['contact_email']}")
        lines.append('Owner identity has not been independently verified against current DLD registration.')
    if r['status'] == 'data_unavailable':
        lines.append('Load a permitted local DLD unit snapshot to obtain candidates.')
    return '\n'.join(lines)[:3900]


def main(argv=None):
    parser = argparse.ArgumentParser(description='Kuku independent four-stage local research, no Telegram deployment.')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--advert-json', help='PF Next-data capture or an authorised normalized public listing JSON')
    source.add_argument('--url', help='Single permitted public URL; requires --fetch-public')
    parser.add_argument('--fetch-public', action='store_true', help='Opt in to one public HTML GET; no CAPTCHA or auth bypass')
    parser.add_argument('--permit', help='Copied Trakheesi text or the JSON record from Web Inspector')
    parser.add_argument('--database', default='data/units.sqlite')
    parser.add_argument('--cache', help='Optional local dedup SQLite store (public facts only)')
    parser.add_argument('--crosswalk', help='Optional opt-in, authorised permit-to-property evidence SQLite store')
    parser.add_argument('--owner-db', help='Optional user-authorised, local owner contact store')
    parser.add_argument('--include-owner', action='store_true', help='Explicitly opt in to sourced owner output when a direct crosswalk claim is present')
    parser.add_argument('--output', help='Optional path for the full research report')
    args = parser.parse_args(argv)
    if args.url and not args.fetch_public:
        parser.error('--url requires explicit --fetch-public; manual captured JSON needs no network')
    if args.advert_json:
        ad = json.loads(Path(args.advert_json).read_text())
    else:
        ad = fetch_public_listing(args.url)
    permit = None
    if args.permit:
        text = Path(args.permit).read_text()
        try:
            permit = json.loads(text)
        except ValueError:
            permit = text
    report = analyze(ad, args.database, permit_payload=permit, cache_path=args.cache,
                     crosswalk_path=args.crosswalk, owner_db=args.owner_db,
                     include_owner=args.include_owner)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(message(report))
    return report


if __name__ == '__main__':
    main()