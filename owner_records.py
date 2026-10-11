"""Local, opt-in owner contact store; never scrapes people or assumes registry ownership.

This module intentionally does not discover owners. It accepts records that the
operator has a documented right to use, keeps them outside Git and labels them
as source-reported, not independently verified title-deed ownership.
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

ALLOWED_BASES = {'owner_consent', 'authorised_agency', 'licensed_data_agreement'}
ALLOWED_ROLES = {'registered_owner_claim', 'authorised_contact', 'owner_representative'}
COLUMN_SET = {'property_id', 'contact_name', 'contact_phone', 'contact_email',
              'contact_role', 'source_name', 'source_reference', 'observed_date',
              'use_basis', 'authorised_for_lookup'}


def _date(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('observed_date must be YYYY-MM-DD.') from None
    if parsed > date.today():
        raise ValueError('observed_date cannot be in the future.')
    return parsed


def _clean(raw):
    if not isinstance(raw, dict):
        raise ValueError('Expected a row dictionary.')
    item = {k: str(raw.get(k) or '').strip() for k in COLUMN_SET}
    if not re.fullmatch(r'[0-9]{1,15}', item['property_id']):
        raise ValueError('A valid DLD property_id is required (not listing/permit number).')
    item['authorised_for_lookup'] = item['authorised_for_lookup'].lower()
    if item['authorised_for_lookup'] != 'yes':
        raise ValueError('Record must explicitly be authorised for this lookup purpose.')
    if item['use_basis'] not in ALLOWED_BASES:
        raise ValueError('Record must have a recognised use basis.')
    if item['contact_role'] not in ALLOWED_ROLES:
        raise ValueError('Contact role must distinguish claimed owner from representative.')
    if not item['source_name'] or not item['source_reference']:
        raise ValueError('Each record requires attributable source and reference.')
    if not item['contact_name'] and not item['contact_phone'] and not item['contact_email']:
        raise ValueError('Record has no contact data to store.')
    _date(item['observed_date'])
    if len(json.dumps(item)) > 10000 or any(len(v) > 1000 for v in item.values()):
        raise ValueError('Owner input is too large.')
    return item


def import_records(path, db_path):
    """Replace all local owner records transactionally from explicitly authorised CSV.

    Caller must ensure the original file may lawfully be used for this purpose.
    Never import the personal pocket inventory automatically.
    """
    rows = []
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not COLUMN_SET.issubset(set(reader.fieldnames or ())):
            raise ValueError('Owner CSV is missing required columns; nothing imported.')
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError('Duplicate owner CSV headers.')
        for i, raw in enumerate(reader, 2):
            if i > 250002:
                raise ValueError('Owner import exceeds safety limit.')
            rows.append(_clean(raw))
    if not rows:
        raise ValueError('Empty owner import refused.')
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = not target.exists()
    if created:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
    os.chmod(target, 0o600)
    try:
        with closing(sqlite3.connect(target)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS owner_records (
                property_id TEXT PRIMARY KEY, contact_name TEXT, contact_phone TEXT,
                contact_email TEXT, contact_role TEXT, source_name TEXT,
                source_reference TEXT, observed_date TEXT, use_basis TEXT,
                authorised_for_lookup TEXT)''')
            db.execute('DELETE FROM owner_records')
            db.executemany('INSERT INTO owner_records VALUES (?,?,?,?,?,?,?,?,?,?)',
                           [tuple(row[k] for k in ('property_id', 'contact_name', 'contact_phone',
                              'contact_email', 'contact_role', 'source_name', 'source_reference',
                              'observed_date', 'use_basis', 'authorised_for_lookup')) for row in rows])
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    return {'imported': len(rows), 'location': str(target),
            'warning': 'Source-reported contacts are not proof of current registered ownership.'}


def lookup(db_path, property_id, *, authorised=False):
    """Only a caller with explicit opt-in and an independently chosen property ID may see a record."""
    if not authorised:
        return {'status': 'not_authorised'}
    if not re.fullmatch(r'[0-9]{1,15}', str(property_id or '')):
        return {'status': 'invalid_property_id'}
    path = Path(db_path)
    if not path.is_file():
        return {'status': 'no_authorised_owner_dataset'}
    try:
        with closing(sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            raw = db.execute('SELECT * FROM owner_records WHERE property_id=?', (str(property_id),)).fetchone()
    except sqlite3.Error:
        return {'status': 'no_authorised_owner_dataset'}
    if raw is None:
        return {'status': 'no_owner_record'}
    row = dict(raw)
    try:
        observed = _date(row['observed_date'])
    except ValueError:
        return {'status': 'invalid_source_date'}
    if row['authorised_for_lookup'] != 'yes' or row['use_basis'] not in ALLOWED_BASES:
        return {'status': 'not_authorised'}
    return {'status': 'source_reported_contact',
            'property_id': row['property_id'], 'contact_name': row['contact_name'],
            'contact_phone': row['contact_phone'], 'contact_email': row['contact_email'],
            'contact_role': row['contact_role'], 'observed_date': observed.isoformat(),
            'source_name': row['source_name'], 'source_reference': row['source_reference'],
            'age_days': (date.today() - observed).days,
            'warning': 'This is a source-reported contact, NOT independently authenticated current title-deed ownership.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Opt-in, local owner-contact records, never internet scraping.')
    parser.add_argument('--database', default='data/owners.sqlite')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--import-csv', help='Import an explicitly authorised, user-provided dataset')
    group.add_argument('--property-id', help='Look up an already independently known property ID')
    args = parser.parse_args(argv)
    if args.import_csv:
        result = import_records(args.import_csv, args.database)
        print(json.dumps(result))
    else:
        result = lookup(args.database, args.property_id, authorised=True)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()