import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from matching import import_csv, match, PULSE_URL
from update_data import CSV_URL
from cloud.export_manifest import manifest
from release_data import install, sha, repository, sync
from data_errors import UpdateError

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        raw = self.root / 'source.csv'
        with raw.open('w', newline='') as stream:
            w = csv.writer(stream)
            w.writerow(['property_id','unit_number','area_name_en','project_name_en','actual_area','rooms_en','extra_public_field'])
            for i in range(12):
                w.writerow([str(i), str(i+100),'Test Area','Seslia Tower','36.88','Studio','preserve me'])
        self.database = self.root / 'source.sqlite'
        import_csv(raw, self.database, PULSE_URL, None, bulk=True, provenance={'url': CSV_URL})
        self.archive = self.root / 'units.sqlite.gz'
        self.archive.write_bytes(gzip.compress(self.database.read_bytes()))
        self.m = manifest(self.database)
        self.m.update(archive_sha256=sha(self.archive), archive_bytes=self.archive.stat().st_size, release_tag='data-123-1')
        self.destination = self.root / 'units.sqlite'

    def test_all_candidates_and_all_columns_preserved(self):
        import sqlite3
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_units').fetchone()[0], 12)
            self.assertEqual(db.execute('SELECT extra_public_field FROM source_units LIMIT 1').fetchone()[0], 'preserve me')
        r=match(self.database, {'building':'Seslia Tower','bedrooms':0,'size_sqm':36.8825}, all_candidates=True)
        self.assertEqual(len(r['records']),12)
        self.assertEqual(r['status'],'possible')
        self.assertIn('unit',r['records'][0])

    def test_verified_archive_installed(self):
        self.assertEqual(install(self.archive,self.m,self.destination,minimum=1)['rows'],12)
        self.assertEqual(sha(self.destination),self.m['database_sha256'])

    def test_corruption_preserves_previous_database(self):
        self.destination.write_bytes(self.database.read_bytes())
        self.archive.write_bytes(self.archive.read_bytes()+b'bad')
        with self.assertRaises(UpdateError): install(self.archive,self.m,self.destination,minimum=1)
        self.assertEqual(sha(self.destination),self.m['database_sha256'])

    def test_decompression_limit_preserves_previous_database(self):
        self.destination.write_bytes(self.database.read_bytes())
        self.m['database_bytes']=10
        with self.assertRaises(UpdateError): install(self.archive,self.m,self.destination,minimum=1)
        self.assertEqual(self.destination.read_bytes(),self.database.read_bytes())

    def test_wrong_provenance_rejected(self):
        self.m['metadata']['download']['url']='https://example.com/units.csv'
        with self.assertRaises(UpdateError): install(self.archive,self.m,self.destination,minimum=1)
        self.assertFalse(self.destination.exists())

    def test_sync_downloads_same_version_asset_from_github_only(self):
        (self.root/'github_source.json').write_text(json.dumps({'repository':'example/units'}))
        urls=[]
        def fetch(url,path,maximum):
            urls.append(url)
            path.write_bytes(json.dumps(self.m).encode() if url.endswith('manifest.json') else self.archive.read_bytes())
        with patch('release_data.fetch',side_effect=fetch), patch('release_data.install',return_value={'rows':12}):
            sync(self.root,lambda _:None)
        self.assertEqual(urls,['https://github.com/example/units/releases/latest/download/manifest.json',
                               'https://github.com/example/units/releases/download/data-123-1/units.sqlite.gz'])

    def test_repository_validation(self):
        for value in ['https://evil.example/x','foo/bar/../x','foo/bar?x=1','foo/bar\n']:
            with self.assertRaises(ValueError): repository(value)
        self.assertEqual(repository('test/units'),'test/units')
