"""Offline, opt-in direct property-identity claims for Kuku.

This is NOT an advert-to-unit discovery algorithm. It only makes an
independently supplied authorised listing-to-DLD-ID document claim queryable,
then checks whether the claimed DLD identity is consistent with the local
registry. No owner data, online requests, passwords or Telegram calls.
"""
import argparse
import csv
import json
import os
import re
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

GUID = re.compile(r'^[a-z0-9]{49}$')
IDENTIFIER = re.compile(r'^[0-9]{5,20}$')
UNIT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 /_-]{0,29}$')
FIELDS = ('listing_url', 'listing_guid', 'property_id', 'unit_number', 'building',
          'area', 'land_number', 'evidence_type', 'evidence_reference',
          'observed_date', 'authorised_for_lookup')
EVIDENCE_TYPES = {'signed_listing_mandate', 'official_permit_property_record',
                  'authorised_agency_export', 'owner_authorised_property_record'}


def canonical_url(url):
    p = urlsplit((url or '').strip())
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Only public HTTPS listing URLs are supported.')
    host = (p.hostname or '').lower()
    path = p.path
    if host in {'propertyfinder.ae', 'www.propertyfinder.ae'}:
        if not re.fullmatch(r'/(en|ar)/(plp|buy|rent)/[A-Za-z0-9_/-]+\.html', path):
            raise ValueError('Not a recognised Property Finder listing URL.')
        host = 'www.propertyfinder.ae'
    elif host in {'bayut.com', 'www.bayut.com'}:
        if not re.fullmatch(r'/property/details-[0-9]+\.html', path):
            raise ValueError('Not a recognised Bayut property details URL.')
        host = 'www.bayut.com'
    elif host in {'dubai.dubizzle.com', 'www.dubizzle.com'}:
        if not (path.startswith('/property-for-rent/') or path.startswith('/property-for-sale/')):
            raise ValueError('Not a recognised Dubizzle property listing URL.')
    else:
        raise ValueError('Unsupported listing portal.')
    return urlunsplit(('https', host, path, '', ''))


def normalize(value):
    return ' '.join(str(value or '').casefold().replace('-', ' ').split())


def validate_claim(raw):
    if not isinstance(raw, dict) or any(k not in raw for k in FIELDS):
        raise ValueError('Missing a required evidence field.')
    row = {k: str(raw[k] or '').strip() for k in FIELDS}
    row['listing_url'] = canonical_url(row['listing_url'])
    if not GUID.fullmatch(row['listing_guid']):
        raise ValueError('ListingGUID must be the complete 49-character token.')
    if not IDENTIFIER.fullmatch(row['property_id']) or not UNIT.fullmatch(row['unit_number']):
        raise ValueError('A canonical numeric DLD property ID and unit number are required.')
    if not row['building'] or not row['area'] or not re.fullmatch(r'[A-Za-z0-9_.-]{6,80}', row['evidence_reference']):
        raise ValueError('Building, area and a non-sensitive evidence reference ID are required.')
    if row['evidence_type'] not in EVIDENCE_TYPES:
        raise ValueError('Unrecognised independently sourced evidence type.')
    if row['authorised_for_lookup'].casefold() != 'yes':
        raise ValueError('Documented authorisation for this lookup is required.')
    try:
        when = date.fromisoformat(row['observed_date'])
        if when > date.today():
            raise ValueError('Evidence observation date cannot be in the future.')
    except (ValueError, TypeError):
        raise ValueError('Evidence observation date must be YYYY-MM-DD, not future.') from None
    if row['land_number'] and not re.fullmatch(r'[A-Za-z0-9/-]{1,24}', row['land_number']):
        raise ValueError('Invalid land number.')
    return row


def import_claims(csv_path, db_path):
    """Atomic import of narrowly scoped, authorised, non-personal identity claims."""
    with open(csv_path, encoding='utf-8-sig', newline='') as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or set(reader.fieldnames) != set(FIELDS):
            raise ValueError('Unexpected CSV columns (do not add owner details).')
        claims = [validate_claim(x) for x in reader]
    if not claims:
        raise ValueError('No claims supplied.')
    key_to_identity = {}
    for row in claims:
        key = (row['listing_url'], row['listing_guid'])
        value = (row['property_id'], normalize(row['unit_number']))
        if key in key_to_identity and key_to_identity[key] != value:
            raise ValueError('Conflicting unit identity claims for one URL/GUID.')
        key_to_identity[key] = value
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(db_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    with sqlite3.connect(db_path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS claims (
            listing_url TEXT NOT NULL, listing_guid TEXT NOT NULL,
            property_id TEXT NOT NULL, unit_number TEXT NOT NULL,
            building TEXT NOT NULL, area TEXT NOT NULL, land_number TEXT NOT NULL,
            evidence_type TEXT NOT NULL, evidence_reference TEXT NOT NULL,
            observed_date TEXT NOT NULL, authorised_for_lookup TEXT NOT NULL,
            UNIQUE(listing_url, listing_guid, evidence_reference))''')
        for row in claims:
            previous = db.execute('SELECT DISTINCT property_id, unit_number FROM claims WHERE listing_url=? AND listing_guid=?',
                                  (row['listing_url'], row['listing_guid'])).fetchall()
            if previous and any((p, normalize(u)) != (row['property_id'], normalize(row['unit_number'])) for p, u in previous):
                raise ValueError('Existing evidence conflicts; no claims imported.')
        db.executemany('''INSERT OR IGNORE INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       ([row[k] for k in FIELDS] for row in claims))
    os.chmod(db_path, 0o600)
    return len(claims)


def lookup_claim(listing_url, claim_db_path, registry_db_path):
    """Never upgrades a document claim into independent advert identity proof."""
    url = canonical_url(listing_url)
    if not Path(claim_db_path).is_file():
        return {'status': 'no_authorised_document_link'}
    with sqlite3.connect('file:' + Path(claim_db_path).resolve().as_posix() + '?mode=ro', uri=True) as db:
        try:
            rows = db.execute('''SELECT listing_guid, property_id, unit_number, building,
                               area, land_number, evidence_type, evidence_reference, observed_date
                               FROM claims WHERE listing_url=?''', (url,)).fetchall()
        except sqlite3.Error:
            return {'status': 'evidence_store_invalid'}
    if not rows:
        return {'status': 'no_authorised_document_link'}
    identities = {(r[1], normalize(r[2])) for r in rows}
    guids = {r[0] for r in rows}
    if len(identities) != 1 or len(guids) != 1:
        return {'status': 'conflicting_document_claims'}
    if not Path(registry_db_path).is_file():
        return {'status': 'registry_not_available'}
    with sqlite3.connect('file:' + Path(registry_db_path).resolve().as_posix() + '?mode=ro', uri=True) as db:
        try:
            results = db.execute('SELECT record FROM units WHERE property_id=? LIMIT 501', (rows[0][1],)).fetchall()
        except sqlite3.Error:
            return {'status': 'registry_schema_unsupported'}
    if not results or len(results) >= 501:
        return {'status': 'registry_not_corroborated'}
    try:
        registered = [json.loads(r[0]) for r in results]
    except (ValueError, TypeError):
        return {'status': 'registry_invalid_rows'}
    registered_ids = {(normalize(r.get('unit')), normalize(r.get('area')),
                       normalize(r.get('building') or r.get('project')),
                       normalize(r.get('land_number'))) for r in registered}
    expected = rows[0]
    unit, area, project, land = normalize(expected[2]), normalize(expected[4]), normalize(expected[3]), normalize(expected[5])
    if len({r[0] for r in registered_ids}) != 1 or any(r[0] != unit or r[1] != area for r in registered_ids):
        return {'status': 'registry_identity_conflict'}
    if any(r[2] != project for r in registered_ids):
        return {'status': 'registry_scope_unconfirmed'}
    if land and any(r[3] not in ('', land) for r in registered_ids):
        return {'status': 'registry_land_conflict'}
    return {
        'status': 'document_claim_registry_corroborated',
        'unit_number': expected[2], 'dld_property_id': expected[1],
        'listing_guid': expected[0], 'evidence_type': expected[6],
        'evidence_reference': expected[7], 'observed_date': expected[8],
        'notice': 'Attributable document claim, matching local DLD registry. Not independently proven current advert identity or current ownership.'
    }


def main():
    p = argparse.ArgumentParser(description='Import or review only separately authorised direct listing-to-unit document claims.')
    sub = p.add_subparsers(dest='command', required=True)
    importer = sub.add_parser('import')
    importer.add_argument('--csv', required=True)
    importer.add_argument('--db', required=True)
    checker = sub.add_parser('lookup')
    checker.add_argument('--url', required=True)
    checker.add_argument('--db', required=True)
    checker.add_argument('--registry', required=True)
    args = p.parse_args()
    result = import_claims(args.csv, args.db) if args.command == 'import' else lookup_claim(args.url, args.db, args.registry)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
