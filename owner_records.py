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
    """Replace the private owner/contact store atomically with authorised rows.

    Joint owners and owner representatives can share a DLD property ID.
    A record is a *contact claim*, never independent title verification.
    """
    rows = []
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if (not reader.fieldnames or set(reader.fieldnames) != COLUMN_SET
                or len(reader.fieldnames) != len(COLUMN_SET)):
            raise ValueError('Owner CSV must have exactly the expected columns.')
        for i, raw in enumerate(reader, 2):
            if i > 250002:
                raise ValueError('Owner import exceeds safety limit.')
            rows.append(_clean(raw))
    if not rows:
        raise ValueError('Empty owner import refused.')
    keys = [(r['property_id'], r['source_reference'], r['contact_role'], r['contact_name']) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate property/contact evidence keys; nothing imported.')
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = not target.exists()
    if created:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
    os.chmod(target, 0o600)
    try:
        with closing(sqlite3.connect(target)) as db, db:
            db.execute('DROP TABLE IF EXISTS owner_records_new')
            db.execute('''CREATE TABLE owner_records_new (
                property_id TEXT NOT NULL, contact_name TEXT NOT NULL,
                contact_phone TEXT NOT NULL, contact_email TEXT NOT NULL,
                contact_role TEXT NOT NULL, source_name TEXT NOT NULL,
                source_reference TEXT NOT NULL, observed_date TEXT NOT NULL,
                use_basis TEXT NOT NULL, authorised_for_lookup TEXT NOT NULL,
                PRIMARY KEY(property_id, source_reference, contact_role, contact_name))''')
            db.executemany('INSERT INTO owner_records_new VALUES (?,?,?,?,?,?,?,?,?,?)',
                           [tuple(row[k] for k in ('property_id', 'contact_name', 'contact_phone',
                              'contact_email', 'contact_role', 'source_name', 'source_reference',
                              'observed_date', 'use_basis', 'authorised_for_lookup')) for row in rows])
            db.execute('DROP TABLE IF EXISTS owner_records')
            db.execute('ALTER TABLE owner_records_new RENAME TO owner_records')
            db.execute('CREATE INDEX IF NOT EXISTS contacts_property ON owner_records(property_id)')
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    return {'imported': len(rows), 'location': str(target),
            'warning': 'Multiple owner/contact claims can exist. None proves current registered ownership.'}


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
            raw = db.execute('SELECT * FROM owner_records WHERE property_id=? ORDER BY observed_date DESC LIMIT 2',
                             (str(property_id),)).fetchall()
    except sqlite3.Error:
        return {'status': 'no_authorised_owner_dataset'}
    if not raw:
        return {'status': 'no_owner_record'}
    if len(raw) > 1:
        return {'status': 'multiple_source_reported_contacts', 'property_id': str(property_id),
                'warning': 'Property has multiple contact records; do not choose one as the current registered owner.'}
    row = dict(raw[0])
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


def lookup_authorised_contacts(db_path, property_id, source_reference, *,
                               authorised=False, max_age_days=90):
    """Read only fresh, expressly consented contacts for the SAME evidence chain.

    Called only after a separately documented listing-to-unit link was established.
    Returns source-reported contacts; *never* independently verified current owners.
    No network calls and no joining contacts by similar names or buildings.
    """
    if not authorised:
        return {'status': 'not_authorised'}
    if not re.fullmatch(r'[0-9]{1,15}', str(property_id or '')):
        return {'status': 'invalid_property_id'}
    if not isinstance(source_reference, str) or not source_reference.strip():
        return {'status': 'missing_evidence_reference'}
    path = Path(db_path) if db_path else None
    if not path or not path.is_file():
        return {'status': 'no_authorised_owner_dataset'}
    try:
        with closing(sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            records = db.execute('''SELECT * FROM owner_records
                                    WHERE property_id=? AND source_reference=?
                                    ORDER BY contact_name ASC LIMIT 51''',
                                 (str(property_id), source_reference)).fetchall()
    except sqlite3.Error:
        return {'status': 'no_authorised_owner_dataset'}
    if not records:
        return {'status': 'no_consent_matched_to_listing_evidence'}
    if len(records) > 50:
        return {'status': 'too_many_contact_claims'}
    contacts = []
    stale = 0
    for raw in records:
        row = dict(raw)
        try:
            observed = _date(row['observed_date'])
        except ValueError:
            return {'status': 'invalid_source_date'}
        age = (date.today() - observed).days
        if age > max_age_days:
            stale += 1
            continue
        if row['authorised_for_lookup'] != 'yes' or row['use_basis'] != 'owner_consent':
            return {'status': 'no_current_contact_consent'}
        contacts.append({
            'contact_name': row['contact_name'], 'contact_role': row['contact_role'],
            'contact_phone': row['contact_phone'], 'contact_email': row['contact_email'],
            'observed_date': observed.isoformat(), 'source_name': row['source_name']
        })
    if stale:
        return {'status': 'contact_evidence_stale', 'stale_count': stale}
    if not contacts:
        return {'status': 'no_consent_matched_to_listing_evidence'}
    return {'status': 'authorised_source_reported_contacts',
            'property_id': str(property_id), 'contacts': contacts,
            'ownership_status': 'not_independently_verified_current_owner',
            'warning': 'Contact use is authorised for this property. Current DLD title ownership remains unverified.'}


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