"""Export public dataset provenance; never reads local bot configuration."""
import gzip
import shutil
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def manifest(database):
    database = Path(database)
    digest = hashlib.sha256()
    with database.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    with closing(sqlite3.connect(f'file:{database.resolve()}?mode=ro', uri=True)) as db:
        metadata = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
        count = db.execute('SELECT COUNT(*) FROM units').fetchone()[0]
        if count != metadata['rows']:
            raise ValueError('Database row count does not match provenance.')
    return {'schema_version': 2, 'database_bytes': database.stat().st_size, 'database_sha256': digest.hexdigest(), 'rows': count, 'metadata': metadata,
            'workflow_commit': os.environ.get('GITHUB_SHA'),
            'attribution': 'Dubai Land Department via Dubai Pulse public open data',
            'identity_note': 'Candidate matching is not independent proof of the advertised unit.'}


if __name__ == '__main__':
    output = Path('cloud-output')
    database = output / 'units.sqlite'
    compressed = output / 'units.sqlite.gz'
    with database.open('rb') as src, gzip.open(compressed, 'wb', compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    result = manifest(database)
    digest = hashlib.sha256()
    with compressed.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    result.update(archive_sha256=digest.hexdigest(), archive_bytes=compressed.stat().st_size,
                  release_tag=os.environ.get('RELEASE_TAG'))
    if result['archive_bytes'] >= 2_000_000_000:
        raise ValueError('Compressed database exceeds GitHub single release asset limit.')
    (output / 'manifest.json').write_text(json.dumps(result, indent=2))
    print('Public dataset checksum and source manifest prepared.')
