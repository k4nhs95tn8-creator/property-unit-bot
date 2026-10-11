"""Opt-in, separately sourced permit/GUID-to-DLD-property crosswalk.

No mapping is guessed or generated here. Records must originate in an explicitly
permitted external source; this importer checks internal consistency and keeps
source claims separately labelled. The importer cannot authenticate a source or
turn a source-reported value into an official government fact.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

GUID = re.compile(r'[a-z0-9]{40,80}\Z')
FIELDS = ('listing_guid', 'transaction_number', 'property_id', 'unit_number',
          'building', 'source_name', 'source_reference', 'observed_date',
          'use_basis', 'authorised_for_lookup')
BASIS = {'authorised_agency', 'licensed_data_agreement', 'official_export_authorised'}


def _validate(raw):
    row = {k: str(raw.get(k) or '').strip() for k in FIELDS}
    if not GUID.fullmatch(row['listing_guid']):
        raise ValueError('Permit link requires a Trakheesi-format ListingGUID.')
    if row['transaction_number'] and not re.fullmatch(r'[0-9]{1,18}', row['transaction_number']):
        raise ValueError('Transaction number is malformed.')
    if not re.fullmatch(r'[0-9]{1,15}', row['property_id']):
        raise ValueError('A genuine numeric DLD property_id is required.')
    if not re.fullmatch(r'[a-zA-Z0-9/-]{1,30}', row['unit_number']):
        raise ValueError('A separate unit number is required.')
    if not row['building'] or not row['source_name'] or not row['source_reference']:
        raise ValueError('Each link requires a building and auditable source reference.')
    if row['use_basis'] not in BASIS or row['authorised_for_lookup'].lower() != 'yes':
        raise ValueError('Explicitly authorised evidence use is required.')
    try:
        observed = date.fromisoformat(row['observed_date'])
    except ValueError:
        raise ValueError('observed_date must be YYYY-MM-DD.') from None
    if observed > date.today():
        raise ValueError('Future-dated evidence refused.')
    if len(json.dumps(row)) > 6000 or any(len(v) > 1000 for v in row.values()):
        raise ValueError('Evidence record too large.')
    row['authorised_for_lookup'] = 'yes'
    return row


def import_links(csv_path, db_path):
    records = []
    with open(csv_path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not set(FIELDS).issubset(reader.fieldnames or ()): 
            raise ValueError('Crosswalk input missing required fields.')
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError('Duplicate headers refused.')
        for i, record in enumerate(reader, 2):
            if i > 250002:
                raise ValueError('Crosswalk CSV too large.')
            records.append(_validate(record))
    if not records:
        raise ValueError('Empty crosswalk refused.')
    if len({r['listing_guid'] for r in records}) != len(records):
        raise ValueError('Conflicting or duplicate GUID mappings require manual review; nothing imported.')
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = not target.exists()
    if created:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
    os.chmod(target, 0o600)
    try:
        with closing(sqlite3.connect(target)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS permit_links (
                listing_guid TEXT PRIMARY KEY, transaction_number TEXT, property_id TEXT,
                unit_number TEXT, building TEXT, source_name TEXT, source_reference TEXT,
                observed_date TEXT, use_basis TEXT, authorised_for_lookup TEXT)''')
            db.execute('DELETE FROM permit_links')
            db.executemany('INSERT INTO permit_links VALUES (?,?,?,?,?,?,?,?,?,?)',
                           [tuple(r[k] for k in FIELDS) for r in records])
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    return {'imported': len(records), 'note': 'Records are external source claims, not source authenticity verification.'}


def lookup(db_path, guid):
    if not isinstance(guid, str) or not GUID.fullmatch(guid):
        return None
    p = Path(db_path) if db_path else None
    if not p or not p.is_file():
        return None
    try:
        with closing(sqlite3.connect(f'file:{p.resolve()}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM permit_links WHERE listing_guid=?', (guid,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row['authorised_for_lookup'] != 'yes' or row['use_basis'] not in BASIS:
        return None
    return dict(row)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Import authorised evidence-bearing permit-to-property rows.')
    parser.add_argument('--import-csv', required=True)
    parser.add_argument('--database', default='data/permit-links.sqlite')
    opts = parser.parse_args(argv)
    print(json.dumps(import_links(opts.import_csv, opts.database)))


if __name__ == '__main__':
    main()