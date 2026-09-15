import csv
import json
import tempfile
import subprocess
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import bot
import setup_app
from matching import DLD_URL, import_csv, match, parse_details, listing_url, format_result

URL = 'https://www.propertyfinder.ae/en/plp/buy/apartment-for-sale-dubai-demo-12345678.html'


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'units.sqlite'
        self.rows = [
            {'property_id': '101', 'Unit Number': '1001', 'Building Name': 'Demo Tower', 'Area': 'Dubai Marina', 'Property Size (sq.m)': '100', 'Room Type': '2 B/R', 'Floor': '10'},
            {'property_id': '102', 'Unit Number': '1101', 'Building Name': 'Demo Tower', 'Area': 'Dubai Marina', 'Property Size (sq.m)': '100', 'Room Type': '2 B/R', 'Floor': '11'},
        ]
        self.facts = {'area': 'Dubai Marina', 'building': 'Demo Tower', 'size_sqm': 100.0, 'bedrooms': 2}
        self.load()

    def load(self, days=0):
        p = self.root / 'input.csv'
        with p.open('w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=self.rows[0].keys())
            w.writeheader()
            w.writerows(self.rows)
        return import_csv(p, self.db, DLD_URL, (date.today() - timedelta(days=days)).isoformat())

    def test_tied_sizes_withhold_units(self):
        r = match(self.db, self.facts)
        self.assertEqual((r['status'], r['count']), ('possible', 2))
        self.assertNotIn('1001', format_result(r))
        self.assertNotIn('property_id', r['records'][0])

    def test_single_size_match_is_not_exact(self):
        self.rows.pop()
        self.load()
        self.assertEqual(match(self.db, self.facts)['status'], 'possible')

    def test_unit_number_alone_is_not_exact(self):
        self.assertEqual(match(self.db, dict(self.facts, unit='1001'))['status'], 'possible')

    def test_unique_id_location_match(self):
        r = match(self.db, dict(self.facts, property_id='101'))
        self.assertEqual(r['status'], 'unique_record')
        self.assertEqual(r['records'][0]['unit'], '1001')

    def test_conflicting_floor_rejects(self):
        self.assertEqual(match(self.db, dict(self.facts, property_id='101', floor='11'))['status'], 'no_match')

    def test_missing_rival_size_retained(self):
        self.rows[1]['Property Size (sq.m)'] = ''
        self.load()
        self.assertEqual(match(self.db, self.facts)['count'], 2)

    def test_missing_id_rival_prevents_unique(self):
        self.rows[1]['property_id'] = ''
        self.load()
        self.assertEqual(match(self.db, dict(self.facts, property_id='101'))['status'], 'possible')

    def test_duplicate_id_elsewhere_prevents_unique(self):
        self.rows[1]['property_id'] = '101'
        self.rows[1]['Building Name'] = 'Other Tower'
        self.load()
        self.assertEqual(match(self.db, dict(self.facts, property_id='101'))['status'], 'possible')

    def test_identical_duplicate_not_rival(self):
        self.rows.append(self.rows[0].copy())
        self.load()
        self.assertEqual(match(self.db, dict(self.facts, property_id='101'))['status'], 'unique_record')

    def test_stale_data_prevents_unique(self):
        self.load(days=31)
        self.assertEqual(match(self.db, dict(self.facts, property_id='101'))['status'], 'possible')

    def test_missing_corroboration_prevents_unique(self):
        self.rows[0]['Floor'] = ''
        self.load()
        self.assertEqual(match(self.db, dict(self.facts, property_id='101', floor='10'))['status'], 'possible')

    def test_floor_zero_is_preserved(self):
        self.rows[0]['Floor'] = '0'
        self.load()
        self.assertEqual(match(self.db, dict(self.facts, property_id='101', floor='0'))['status'], 'unique_record')

    def test_bad_import_rolls_back(self):
        self.rows[1]['Area'] = ''
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(match(self.db, self.facts)['count'], 2)

    def test_owner_fields_not_imported(self):
        for r in self.rows:
            r['Owner Name'] = 'PRIVATE OWNER'
        self.load()
        self.assertNotIn('PRIVATE OWNER', format_result(match(self.db, dict(self.facts, property_id='101'))))

    def test_unknown_and_missing_data(self):
        self.assertEqual(match(self.root / 'absent.db', self.facts)['status'], 'unavailable')
        self.assertEqual(match(self.db, {})['status'], 'needs_details')
        self.assertEqual(match(self.db, dict(self.facts, building='Not Here'))['status'], 'no_match')

    def test_normalisation_and_sqft(self):
        f = parse_details('area: DUBAI MARINA\nbuilding: demo tower\nbedrooms: 2\nsize_sqft: 1,076.39')
        self.assertEqual(match(self.db, f)['count'], 2)
        self.assertEqual(parse_details('bedrooms: Studio')['bedrooms'], 0)

    def test_bad_fields_rejected(self):
        for t in ['size_sqm: nan', 'size_sqm: -2', 'size_sqft: inf', 'size_sqm: 10\nsize_sqft: 100', 'bedrooms: many', 'unit: 1\nunit: 2', 'permit: 101']:
            with self.subTest(t=t), self.assertRaises(ValueError):
                parse_details(t)

    def test_url_validation(self):
        self.assertEqual(listing_url(URL + '?tracking=secret'), URL)
        for u in ['http://127.0.0.1/a', 'https://propertyfinder.ae.evil.com/en/plp/a.html', 'https://evil.com', 'https://user@propertyfinder.ae/en/plp/a.html', 'https://www.propertyfinder.ae:8443/en/plp/a.html', 'https://www.propertyfinder.ae/en/search.html']:
            with self.subTest(u=u), self.assertRaises(ValueError):
                listing_url(u)

    def test_bot_url_followup(self):
        c = bot.Conversation(self.db, self.root / 'listings.json')
        self.assertIn('Link accepted', c.handle(1, URL))
        answer = c.handle(1, 'area: Dubai Marina\nbuilding: Demo Tower\nbedrooms: 2\nsize_sqm: 100')
        self.assertIn('Possible match only', answer)
        self.assertIn('Send the listing URL first', c.handle(2, 'area: Dubai Marina'))
        c.handle(1, '/cancel')
        self.assertIn('Send the listing URL first', c.handle(1, 'area: Dubai Marina'))

    def test_saved_url_only_flow(self):
        p = self.root / 'listings.json'
        bot.save_json(p, {URL: {'facts': self.facts, 'saved_at': time.time()}})
        self.assertIn('Possible match only', bot.Conversation(self.db, p).handle(1, URL))

    def test_stale_listing_requests_new_details(self):
        p = self.root / 'listings.json'
        bot.save_json(p, {URL: {'facts': self.facts, 'saved_at': time.time() - 31 * 86400}})
        self.assertIn('older than 30 days', bot.Conversation(self.db, p).handle(1, URL))


class WorkerTests(unittest.TestCase):
    def test_mac_transport_keeps_token_off_command_line(self):
        token = '123456:abcdefghijklmnopqrstuvw'
        response = subprocess.CompletedProcess([], 0, '{"ok":true,"result":{"id":123456}}', '')
        with patch.object(bot.subprocess, 'run', return_value=response) as call:
            self.assertEqual(bot.mac_api(token, 'getMe', {})['id'], 123456)
            args, kwargs = call.call_args
            self.assertNotIn(token, ' '.join(args[0]))
            self.assertNotIn('--insecure', args[0])
            self.assertIn(token, kwargs['input'])

    def test_mac_transport_redacts_errors_and_preserves_rate_limit(self):
        token = '123456:abcdefghijklmnopqrstuvw'
        response = subprocess.CompletedProcess([], 60, '', 'secret details')
        with patch.object(bot.subprocess, 'run', return_value=response), self.assertRaises(bot.TelegramError) as error:
            bot.mac_api(token, 'getMe', {})
        self.assertNotIn('secret', str(error.exception))
        response = subprocess.CompletedProcess([], 0, '{"ok":false,"error_code":429,"parameters":{"retry_after":30}}', '')
        with patch.object(bot.subprocess, 'run', return_value=response), self.assertRaises(bot.TelegramError) as error:
            bot.mac_api(token, 'getMe', {})
        self.assertEqual(error.exception.retry_after, 30)

    def test_pairing_and_private_owner_only(self):
        with tempfile.TemporaryDirectory() as d, patch.object(bot, 'DATA', Path(d)):
            bot.save_json(bot.DATA / 'config.json', {'token': 'fake', 'pair_code': 'secret-code', 'pair_expires': time.time() + 300})
            stop = threading.Event()
            sent = []
            def update(i, user, text, private=True):
                return {'update_id': i, 'message': {'from': {'id': user}, 'chat': {'id': user, 'type': 'private' if private else 'group'}, 'text': text}}
            def fake_api(token, method, payload):
                if method == 'getMe': return {'username': 'test_bot'}
                if method == 'getWebhookInfo': return {}
                if method == 'sendMessage':
                    sent.append(payload)
                    return {}
                stop.set()
                return [update(1, 8, '/pair wrong'), update(2, 9, '/pair secret-code', False),
                        update(3, 7, '/pair secret-code'), update(4, 8, URL), update(5, 7, URL)]
            with patch.object(bot, 'api', side_effect=fake_api):
                bot.run(stop)
            self.assertEqual(bot.config()['owner'], 7)
            self.assertEqual([s['chat_id'] for s in sent], [7, 7])
            self.assertEqual(bot.read_json(bot.DATA / 'offset.json', {})['offset'], 6)
            self.assertNotIn('pair_code', bot.config())
            self.assertEqual((bot.DATA / 'config.json').stat().st_mode & 0o777, 0o600)


class SetupTests(unittest.TestCase):
    def test_setup_host_origin_csrf_and_escaped_output(self):
        server = setup_app.ThreadingHTTPServer(('127.0.0.1', 0), setup_app.Handler)
        port = server.server_address[1]
        origin = f'http://127.0.0.1:{port}'
        with patch.object(setup_app, 'PORT', port), patch.object(setup_app, 'ORIGIN', origin):
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                with urlopen(origin) as r:
                    self.assertIn('Dubai property bot', r.read().decode())
                    self.assertEqual(r.headers['Cache-Control'], 'no-store')
                for headers in [{'Host': 'evil.example'}, {'Origin': 'https://evil.example'}, {'Origin': origin}]:
                    request = Request(origin + '/clear', data=b'csrf=wrong', headers=headers)
                    with self.assertRaises(HTTPError) as err:
                        urlopen(request)
                    self.assertEqual(err.exception.code, 403)
                    err.exception.close()
                self.assertIn('&lt;script&gt;', setup_app.page('<script>'))
            finally:
                server.shutdown()
                server.server_close()
                t.join()


if __name__ == '__main__':
    unittest.main()
