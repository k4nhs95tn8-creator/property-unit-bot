"""Download validated public GitHub release data; never contact DLD from the bot."""
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from data_errors import UpdateError


def repository(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', value):
        raise ValueError('Enter your public GitHub repository as username/repository.')
    return value


def fetch(url, path, maximum):
    result = subprocess.run(['curl', '--disable', '--fail', '--silent', '--show-error',
        '--location', '--max-redirs', '5', '--proto', '=https', '--proto-redir', '=https',
        '--connect-timeout', '30', '--max-time', '1800', '--max-filesize', str(maximum),
        '--output', str(path), url], capture_output=True, timeout=1810)
    if result.returncode:
        raise UpdateError('GitHub release download unavailable. Previous database retained; check that the first cloud run succeeded.')
    if path.stat().st_size > maximum:
        raise UpdateError('GitHub asset exceeds the permitted size.')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def install(archive, manifest, destination, minimum=100000):
    from update_data import validate_database, previous_rows, CSV_URL
    from matching import PULSE_URL
    if manifest.get('schema_version') != 2 or manifest.get('metadata', {}).get('source') != PULSE_URL:
        raise UpdateError('Unsupported database schema or unofficial source.')
    if manifest['metadata'].get('download', {}).get('url') != CSV_URL:
        raise UpdateError('Database lacks official bulk-download provenance.')
    if archive.stat().st_size != manifest['archive_bytes'] or sha(archive) != manifest['archive_sha256']:
        raise UpdateError('Compressed database checksum failed; previous data retained.')
    maximum = manifest['database_bytes']
    if not isinstance(maximum, int) or not 0 < maximum <= 12_000_000_000:
        raise UpdateError('Invalid database size.')
    if shutil.disk_usage(destination.parent).free < maximum + 100_000_000:
        raise UpdateError('Insufficient disk space for safe database replacement.')
    staged = archive.parent / 'verified.sqlite'
    count = 0
    with gzip.open(archive, 'rb') as src, staged.open('wb') as dst:
        while chunk := src.read(1024 * 1024):
            count += len(chunk)
            if count > maximum:
                raise UpdateError('Expanded database exceeds declared size.')
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    if count != maximum or sha(staged) != manifest['database_sha256']:
        raise UpdateError('Database checksum failed; previous data retained.')
    meta = validate_database(staged, minimum=minimum, old_rows=previous_rows(destination))
    if meta != manifest['metadata'] or meta['rows'] != manifest['rows']:
        raise UpdateError('Database provenance differs from release manifest.')
    import sqlite3
    with sqlite3.connect(staged) as db:
        if db.execute('SELECT COUNT(*) FROM source_units').fetchone()[0] != meta['rows']:
            raise UpdateError('Raw source rows are incomplete.')
    os.replace(staged, destination)
    return meta


def sync(data_dir, progress=print):
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    config_path = data / 'github_source.json'
    value = os.environ.get('DLD_GITHUB_REPO')
    if not value and config_path.exists():
        value = json.loads(config_path.read_text()).get('repository')
    if not value:
        raise UpdateError('Waiting for GitHub authorization and your first successful cloud database release.')
    repo = repository(value)
    base = f'https://github.com/{repo}/releases'
    with (data / 'update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError('Database sync already running.') from None
        with tempfile.TemporaryDirectory(prefix='github-staging-', dir=data) as tmp:
            tmp = Path(tmp)
            progress('Checking your public GitHub database release…')
            fetch(base + '/latest/download/manifest.json', tmp / 'manifest.json', 1_000_000)
            manifest = json.loads((tmp / 'manifest.json').read_text())
            tag = manifest.get('release_tag', '')
            if not re.fullmatch(r'data-[0-9]+-[0-9]+', tag):
                raise UpdateError('Invalid release tag.')
            destination = data / 'units.sqlite'
            if destination.exists() and sha(destination) == manifest.get('database_sha256'):
                progress('Your database is already up to date with the latest GitHub release.')
                return manifest['metadata']
            progress('Downloading the compressed database from GitHub…')
            archive = tmp / 'units.sqlite.gz'
            fetch(base + f'/download/{tag}/units.sqlite.gz', archive, 2_000_000_000)
            meta = install(archive, manifest, destination)
            progress(f"Loaded {meta['rows']:,} official DLD unit records from GitHub. Matches remain candidates unless identity is verified.")
            return meta


def auto_sync(data, stop):
    def progress(message):
        import bot
        bot.save_json(Path(data) / 'update_status.json', {'message': message, 'updated_at': time.time()})
    while not stop.is_set():
        try:
            sync(data, progress)
        except Exception as exc:
            progress(str(exc) if isinstance(exc, (UpdateError, ValueError)) else 'GitHub sync failed; previous database retained.')
        stop.wait(3600)
