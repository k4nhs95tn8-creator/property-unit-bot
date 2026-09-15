"""Conservative matching against a locally imported, attributed DLD snapshot."""
import csv
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DLD_URL = 'https://dubailand.gov.ae/en/open-data/real-estate-data/'
PULSE_URL = 'https://www.dubaipulse.gov.ae/data/dld-registration/dld_units-open'
SOURCES = {DLD_URL, PULSE_URL}


def norm(value):
    return ' '.join(re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', str(value)).casefold()).split())


def listing_url(value):
    p = urlsplit(value.strip())
    if (p.scheme != 'https' or p.hostname not in {'propertyfinder.ae', 'www.propertyfinder.ae'}
            or p.username or p.password or p.port not in (None, 443)
            or not re.fullmatch(r'/(en|ar)/(plp|buy|rent)/[a-zA-Z0-9_/-]+\.html', p.path)):
        raise ValueError('Use a full https://www.propertyfinder.ae/en/plp/…html listing link.')
    return urlunsplit(('https', 'www.propertyfinder.ae', p.path, '', ''))


ALIASES = {
    'property_id': ['property_id'],
    'unit': ['unit_number', 'unit number', 'property_number'],
    'building': ['building_name_en', 'building_name', 'building name'],
    'building_id': ['parent_property_id'],
    'building_number': ['building_number', 'building number'],
    'area': ['area_name_en', 'area name', 'area'],
    'project': ['project_name_en', 'project_name', 'project name'],
    'project_number': ['project_number', 'project number'],
    'size_sqm': ['actual_area', 'property size (sq.m)', 'size_sqm'],
    # Pulse's `rooms` is a category ID, not a bedroom count.
    'bedrooms': ['rooms_en', 'room type', 'bedrooms'],
    'floor': ['floor', 'floor_number'],
    'property_type': ['property_sub_type_en', 'property sub type'],
    'land_number': ['land_number', 'land number'],
    'land_sub_number': ['land_sub_number', 'land sub number'],
    'freehold': ['is_free_hold', 'is free hold?'],
}


def rooms(value):
    value = norm(value)
    if value in {'studio', '0', '0 br', '0 b r'}:
        return 0
    m = re.fullmatch(r'(\d{1,2})(?: bedrooms?| beds?| br| b r| rooms?)?', value)
    return int(m[1]) if m else None


def number(value):
    try:
        result = float(str(value).replace(',', ''))
    except (TypeError, ValueError):
        raise ValueError('Size must be a positive number.') from None
    if not math.isfinite(result) or result <= 0 or result > 1000000:
        raise ValueError('Size must be a positive number below 1,000,000.')
    return result


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS units (area TEXT, building TEXT, property_id TEXT, record TEXT)')
    if 'project' not in {r[1] for r in db.execute('PRAGMA table_info(units)')}:
        db.execute("ALTER TABLE units ADD COLUMN project TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE units SET project=lower(COALESCE(json_extract(record,'$.project'),''))")
    db.execute('CREATE INDEX IF NOT EXISTS location ON units(area, building)')
    db.execute('CREATE INDEX IF NOT EXISTS identity ON units(property_id)')
    db.execute('CREATE INDEX IF NOT EXISTS project_location ON units(project, area)')
    db.execute('CREATE TABLE IF NOT EXISTS metadata (value TEXT)')
    return db


def import_csv(path, db_path, source, as_of, *, bulk=False, provenance=None, progress=None):
    if source not in SOURCES:
        raise ValueError('Choose the official DLD or Dubai Pulse unit source.')
    if as_of and date.fromisoformat(as_of) > date.today():
        raise ValueError('The snapshot date cannot be in the future.')
    if not as_of and not bulk:
        raise ValueError('Supply the snapshot date.')
    if bulk and source != PULSE_URL:
        raise ValueError('Bulk mode is only for the official Pulse unit dataset.')
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    with closing(connect(db_path)) as db, db, open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        headers = {norm(h): h for h in reader.fieldnames or []}
        if len(headers) != len(reader.fieldnames or []):
            raise ValueError('Duplicate CSV headers; dataset not replaced.')
        mapping = {k: next((headers[norm(a)] for a in aliases if norm(a) in headers), None)
                   for k, aliases in ALIASES.items()}
        if not mapping['unit'] or not mapping['area'] or not (mapping['building'] or (bulk and mapping['project'])):
            raise ValueError('The unit CSV needs unit and area columns, plus building name or a Pulse project name.')
        db.execute('DELETE FROM units')
        if bulk:
            # Preserve every public source column and duplicate row, without a primary key.
            quote = lambda name: '"' + name.replace('"', '""') + '"'
            columns = reader.fieldnames
            db.execute('DROP TABLE IF EXISTS source_units')
            db.execute('CREATE TABLE source_units (' + ','.join(quote(c) + ' TEXT' for c in columns) + ')')
            raw_insert = 'INSERT INTO source_units VALUES (' + ','.join('?' for _ in columns) + ')'

        count = 0
        incomplete = 0
        unknown_sizes = 0
        unnamed_buildings = 0
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f'Malformed CSV row {line}; previous dataset retained.')
            if bulk:
                db.execute(raw_insert, [row[c] for c in columns])
            record = {k: row[h].strip() for k, h in mapping.items() if h and row[h].strip()}
            if not bulk and (not record.get('unit') or not record.get('area') or not record.get('building')):
                raise ValueError(f'Missing unit, area or building on row {line}; previous dataset retained.')
            if not record.get('area') or not (record.get('building') or record.get('project')) or not record.get('unit'):
                incomplete += 1
            if not record.get('building'):
                unnamed_buildings += 1
            if record.get('size_sqm'):
                try:
                    record['size_sqm'] = number(record['size_sqm'])
                except ValueError:
                    if not bulk:
                        raise
                    record.pop('size_sqm')
                    unknown_sizes += 1
            if record.get('bedrooms'):
                raw = record['bedrooms']
                record['bedrooms'] = rooms(raw)
                record['room_description'] = raw
            db.execute('INSERT INTO units(area,building,property_id,record,project) VALUES (?,?,?,?,?)',
                       (norm(record.get('area', '')), norm(record.get('building', '')), record.get('property_id', ''), json.dumps(record), norm(record.get('project', ''))))
            count += 1
            if progress and count % 100000 == 0:
                progress(f'Imported {count:,} unit rows into staging database.')
        if not count:
            raise ValueError('Empty dataset; previous dataset retained.')
        if bulk:
            for column in ('property_id', 'unit_number', 'project_name_en', 'area_name_en',
                           'parent_property_id', 'building_number', 'land_number', 'rooms_en'):
                if column in columns:
                    db.execute('CREATE INDEX ' + quote('source_' + column) + ' ON source_units(' + quote(column) + ')')
        meta = {'source': source, 'as_of': as_of, 'sha256': digest.hexdigest(), 'rows': count,
                'incomplete_identity_rows': incomplete, 'unknown_size_rows': unknown_sizes,
                'unnamed_building_rows': unnamed_buildings,
                'coverage': 'Not independently verified; results apply only to this imported snapshot.'}
        if bulk:
            meta['source_columns'] = columns
            meta['schema_version'] = 2
        if provenance:
            meta['download'] = provenance
        db.execute('DELETE FROM metadata')
        db.execute('INSERT INTO metadata VALUES (?)', (json.dumps(meta),))
    return meta


def parse_details(text):
    allowed = {'area', 'building', 'project', 'unit', 'floor', 'bedrooms', 'size_sqm', 'size_sqft', 'property_id'}
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(':')
        key = key.strip().lower().replace(' ', '_')
        if not sep or key not in allowed or not value.strip() or key in result:
            raise ValueError('Use one field per line: area:, building: or project:, bedrooms:, size_sqft: (or size_sqm:), and optionally unit:, floor:, property_id:.')
        result[key] = value.strip()
    if 'size_sqm' in result and 'size_sqft' in result:
        raise ValueError('Supply size in either square feet or square metres, not both.')
    if 'size_sqft' in result:
        result['size_sqm'] = number(result.pop('size_sqft')) * 0.09290304
    elif 'size_sqm' in result:
        result['size_sqm'] = number(result['size_sqm'])
    if 'bedrooms' in result:
        result['bedrooms'] = rooms(result['bedrooms'])
        if result['bedrooms'] is None:
            raise ValueError('Bedrooms must be Studio or a whole number.')
    return result


def match(db_path, facts, *, all_candidates=False):
    if not Path(db_path).exists():
        return {'status': 'unavailable', 'message': 'No official DLD snapshot has been loaded yet.'}
    if not (facts.get('building') or facts.get('project')):
        return {'status': 'needs_details', 'message': 'Please supply the official building or project name and, when known, area. A URL or advertising permit is not a DLD unit identifier.'}
    with closing(connect(db_path)) as db, db:
        meta_row = db.execute('SELECT value FROM metadata').fetchone()
        if not meta_row:
            return {'status': 'unavailable', 'message': 'No official DLD snapshot has been loaded yet.'}
        meta = json.loads(meta_row[0])
        # A project is not a building. Search either name but mark project-only
        # retrieval explicitly; broadening candidate retrieval never proves identity.
        building = norm(facts.get('building', ''))
        project = norm(facts.get('project') or facts.get('building', ''))
        query = 'SELECT record FROM units WHERE ((building<>\'\' AND building=?) OR (project<>\'\' AND project=?))'
        params = [building, project]
        if facts.get('area'):
            query += " AND (area=? OR area='')"
            params.append(norm(facts['area']))
        rows = db.execute(query, params)
        unique = {r[0]: json.loads(r[0]) for r in rows}
        records = list(unique.values())
        id_rows = []
        if facts.get('property_id'):
            id_rows = list(db.execute('SELECT DISTINCT record FROM units WHERE property_id=?', (facts['property_id'],)))
    candidates = []
    for r in records:
        # Missing attributes do not eliminate a rival. This prevents false uniqueness.
        conflicts = any(r.get(k) not in (None, '') and norm(r[k]) != norm(facts[k])
                        for k in ('unit', 'floor', 'property_id', 'bedrooms') if k in facts)
        if 'size_sqm' in facts and r.get('size_sqm'):
            conflicts |= abs(r['size_sqm'] - facts['size_sqm']) > max(1.0, facts['size_sqm'] * .02)
        if not conflicts:
            candidates.append(r)
    project_scope = not facts.get('building') or any(not r.get('building') or norm(r['building']) != building for r in records)
    result = {'metadata': meta, 'count': len(candidates), 'building_records': len(records),
              'scope': 'project — may include multiple buildings' if project_scope else 'building'}
    if not candidates:
        return dict(result, status='no_match', message='No compatible record in this snapshot. Check the official building/area spelling or refresh the data; this does not prove the property is absent from DLD.')
    strong = False
    if len(candidates) == 1:
        r = candidates[0]
        supplied = [k for k in ('unit', 'floor', 'property_id', 'bedrooms', 'size_sqm') if k in facts]
        corroborated = all(r.get(k) not in (None, '') for k in supplied)
        fresh = bool(meta.get('as_of') and 0 <= (date.today() - date.fromisoformat(meta['as_of'])).days <= 30)
        # Exact identity is only a globally unique DLD ID, corroborated by location.
        # Unit number + size alone NEVER upgrades to an exact match.
        strong = bool(facts.get('property_id') and facts.get('area') and r.get('area') and
                      not project_scope and len(id_rows) == 1 and corroborated and fresh and
                      not meta.get('incomplete_identity_rows', 0))
    if strong:
        return dict(result, status='unique_record', message='Unique DLD identifier match in the imported snapshot. This confirms the supplied record ID and location, not independent verification that the advert is that unit.', records=candidates)
    if all_candidates:
        return dict(result, status='possible', records=candidates,
                    message='All compatible candidates below; none is a verified identification of the advertised unit. Candidate IDs and unit numbers are alternatives, not an exact match.')
    # Keep the compact Telegram summary separate from the full candidate report.
    summaries = [{k: r[k] for k in ('area', 'building', 'project', 'size_sqm', 'bedrooms', 'property_type') if k in r}
                 for r in candidates[:5]]
    return dict(result, status='possible', message='Possible match only — the exact unit is unverified. Even a single size/bedroom match can be coincidental or reflect incomplete data. Unit numbers are withheld. Supply a genuine DLD property ID for an identifier check.', records=summaries)


def format_result(result):
    lines = [result['message']]
    if 'count' in result:
        lines.append(f"Compatible records: {result['count']} of {result['building_records']} records checked. Search scope: {result.get('scope', 'building')}.")
    for r in result.get('records', []):
        lines.append('\n' + '\n'.join(f'{k.replace("_", " ")}: {v}' for k, v in r.items()))
    if 'metadata' in result:
        m = result['metadata']
        lines += [f"\nSnapshot date: {m.get('as_of') or 'unknown; download date does not prove freshness'}", m['coverage'], f"Source: Dubai Land Department — {m['source']}"]
    return '\n'.join(lines)[:3900]
