"""Official public Pulse CSV -> validated staging database -> atomic replacement.

The public resource URL and bulk-download approach were identified by inspecting
NABILNET-ORG/dld-unit-finder/convert_csv_to_db.py and its update_data.yml workflow.
This is an independent implementation; no upstream scraper or release DB is used.
"""
import argparse
import csv
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from matching import PULSE_URL, import_csv
from data_errors import UpdateError

CSV_URL = ('https://www.dubaipulse.gov.ae/dataset/'
           '85462a5b-08dc-4325-9242-676a0de4afc4/resource/'
           '7d4deadf-c9bc-47a4-85de-998d0ce38bf3/download/units.csv')
RESOURCE_ID = '7d4deadf-c9bc-47a4-85de-998d0ce38bf3'
MAX_BYTES = 2_000_000_000
MIN_ROWS = 100_000


def allowed_url(url):
    p = urlsplit(url)
    return (p.scheme == 'https' and p.hostname == 'www.dubaipulse.gov.ae'
            and p.port in (None, 443) and not p.username and not p.password
            and '/resource/' + RESOURCE_ID + '/download/' in p.path)


def parse_headers(text):
    # Some HTTPS transports prepend a CONNECT response; keep final header block.
    blocks = text.replace('\r\n', '\n').strip().split('\n\n')
    block = next((b for b in reversed(blocks) if b.startswith('HTTP/')), '')
    return {key.lower().strip(): value.strip() for line in block.splitlines()[1:]
            for key, sep, value in [line.partition(':')] if sep}


def validate_csv(path, headers):
    size = Path(path).stat().st_size
    if not size or size > MAX_BYTES:
        raise UpdateError('Official download was empty or exceeded the size limit; old data retained.')
    expected = headers.get('content-length')
    if expected and (not expected.isdigit() or int(expected) != size):
        raise UpdateError('Download size does not match the official response; old data retained.')
    content_type = headers.get('content-type', '').lower()
    if 'html' in content_type or 'json' in content_type:
        raise UpdateError('Official server returned a page rather than CSV. No access challenge will be bypassed.')
    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            fields = next(csv.reader(f))
    except (UnicodeError, StopIteration, csv.Error):
        raise UpdateError('Official download is not a readable CSV.') from None
    required = {'unit_number', 'property_id', 'area_name_en', 'project_name_en', 'actual_area'}
    if not required.issubset({x.strip().lower() for x in fields}):
        raise UpdateError('Official response does not have the expected Pulse unit schema; no dataset replaced.')
    return size


def download(path, progress=print):
    url = CSV_URL
    headers_path = Path(str(path) + '.headers')
    for _ in range(4):
        if not allowed_url(url):
            raise UpdateError('Official resource redirects outside the approved public download. Stopped for review.')
        progress('Connecting to the official public Dubai Pulse unit CSV…')
        command = [shutil.which('curl') or '/usr/bin/curl', '--disable', '--silent', '--show-error',
                   '--proto', '=https', '--connect-timeout', '120', '--max-time', '1800',
                   '--speed-limit', '1024', '--speed-time', '60', '--max-filesize', str(MAX_BYTES),
                   '--dump-header', str(headers_path), '--output', str(path), '--write-out', '%{http_code}', url]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=1810)
        except (OSError, subprocess.TimeoutExpired):
            raise UpdateError('The official download connection failed or timed out; previous data retained.') from None
        if result.returncode:
            raise UpdateError(f'Official Dubai Pulse download failed (connection code {result.returncode}). No data was replaced; no CAPTCHA or authentication was bypassed.')
        headers = parse_headers(headers_path.read_text())
        status = result.stdout.strip()
        if status in ('301', '302', '303', '307', '308'):
            url = urljoin(url, headers.get('location', ''))
            continue
        if status != '200':
            raise UpdateError(f'Official server returned HTTP {status}. Stopped; no authentication, rate limit or access controls bypassed.')
        size = validate_csv(path, headers)
        return {'url': url, 'downloaded_at': datetime.now(timezone.utc).isoformat(),
                'bytes': size, 'http_last_modified': headers.get('last-modified'),
                'etag': headers.get('etag'), 'access': 'public HTTPS; no credentials or cookies'}
    raise UpdateError('Too many official download redirects; stopped.')


def previous_rows(path):
    if not Path(path).exists():
        return 0
    with closing(sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)) as db:
        row = db.execute('SELECT value FROM metadata').fetchone()
        return json.loads(row[0]).get('rows', 0) if row else 0


def validate_database(path, minimum=MIN_ROWS, old_rows=0):
    with closing(sqlite3.connect(path)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise UpdateError('Staging database failed integrity checks.')
        count = db.execute('SELECT COUNT(*) FROM units').fetchone()[0]
        metadata = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
        if count != metadata['rows'] or count < minimum:
            raise UpdateError('Downloaded unit dataset has too few rows or failed row-count checks; previous data retained.')
        if old_rows and count < old_rows * .8:
            raise UpdateError('New dataset is over 20% smaller than the existing snapshot; review required before replacement.')
        return metadata


def update_bulk(data_dir, progress=print):
    return _update(data_dir, progress, download)


def update(data_dir, progress=print):
    from release_data import sync
    return sync(data_dir, progress)


def _update(data_dir, progress, fetch):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    # OS lock prevents concurrent refreshes, including separate CLI and setup jobs.
    with open(data_dir / 'update.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError('An official dataset update is already running.') from None
        destination = data_dir / 'units.sqlite'
        if shutil.disk_usage(data_dir).free < 5_000_000_000:
            raise UpdateError('At least 5 GB of free space is needed for the public download and safe staging import.')
        with tempfile.TemporaryDirectory(prefix='pulse-staging-', dir=data_dir) as tmp:
            raw = Path(tmp) / 'units.csv'
            staging = Path(tmp) / 'units.sqlite'
            provenance = fetch(raw, progress)
            progress('Download complete. Validating and importing official unit rows…')
            # The HTTP transfer date is not the date of the underlying records.
            import_csv(raw, staging, PULSE_URL, None, bulk=True, provenance=provenance, progress=progress)
            meta = validate_database(staging, old_rows=previous_rows(destination))
            if provenance.get('rows_received', meta['rows']) != meta['rows']:
                raise UpdateError('Imported row count differs from received API rows; previous data retained.')
            with open(staging, 'rb') as f:
                os.fsync(f.fileno())
            os.replace(staging, destination)
            progress(f"Loaded {meta['rows']:,} official unit rows. Snapshot age remains unknown; exact identity claims stay restricted.")
            return meta


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Refresh official public DLD unit data without manual CSV downloads.')
    parser.add_argument('--data-dir', default=os.environ.get('BOT_DATA_DIR', Path(__file__).resolve().parent / 'data'))
    parser.add_argument('--source', choices=['github', 'bulk'], default='github')
    args = parser.parse_args()
    try:
        if args.source == 'bulk' and os.environ.get('GITHUB_ACTIONS') != 'true':
            raise UpdateError('Official bulk downloads run only in GitHub Actions. Use the GitHub release sync locally.')
        runner = update if args.source == 'github' else update_bulk
        runner(args.data_dir, lambda message: print(message, flush=True))
    except (UpdateError, ValueError, sqlite3.Error) as exc:
        print(f'Update stopped: {exc}', flush=True)
        raise SystemExit(1)
