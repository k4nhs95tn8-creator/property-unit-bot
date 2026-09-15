"""Loopback-only setup page; secrets never returned to the browser after saving."""
import html
import json
import os
import re
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import bot
from matching import DLD_URL, PULSE_URL, listing_url, parse_details, import_csv, match, format_result
from update_data import update, UpdateError
from release_data import repository

CSRF = secrets.token_urlsafe(32)
PORT = int(os.environ.get('SETUP_PORT', '8765'))
ORIGIN = f'http://127.0.0.1:{PORT}'
UPDATE_LOCK = threading.Lock()


def refresh_data():
    def status(message):
        bot.save_json(bot.DATA / 'update_status.json', {'message': message, 'updated_at': time.time()})
    try:
        status('Checking your GitHub database release…')
        update(bot.DATA, status)
    except Exception as exc:
        message = str(exc) if isinstance(exc, (UpdateError, ValueError)) else 'Local import failed. Previous data retained.'
        status(message)
    finally:
        UPDATE_LOCK.release()


def page(message=''):
    c = bot.config()
    paired = bool(c.get('owner'))
    pairing = ''
    if c.get('token') and not paired:
        if time.time() < c.get('pair_expires', 0):
            pairing = f'<p>In your new bot’s Telegram chat, send: <code>/pair {html.escape(c["pair_code"])}</code></p><p>This private pairing code expires in 15 minutes.</p>'
        else:
            pairing = '<p>Pairing expired. Save your bot token again for a new code.</p>'
    hidden = f'<input type="hidden" name="csrf" value="{CSRF}">'
    def form(action, body):
        return f'<form method="post" action="/{action}">{hidden}{body}</form>'
    token_form = form('token', '<label>BotFather token<input name="token" type="password" autocomplete="off" required></label><button>Connect Telegram</button>')
    facts = '<label>Listing link<input name="url" type="url" placeholder="https://www.propertyfinder.ae/en/plp/…html" required></label><label>Listing details<textarea name="details" rows="7" placeholder="area: Dubai Marina&#10;building: Actual building name&#10;bedrooms: 2&#10;size_sqft: 1200" required></textarea></label>'
    test_form = form('match', facts + '<button>Test a match locally</button>')
    save_form = form('listing', facts + '<label><input type="checkbox" name="authorised" value="yes" required> I am authorised to use and save these listing details.</label><button>Save listing for future URL-only requests</button>')
    clear_form = form('clear', '<button>Delete saved listing details</button>')
    refresh_form = form('refresh', '<button>Download latest GitHub database</button>')
    github_form = form('github-source', '<label>Your public GitHub repository<input name="repository" placeholder="username/property-unit-bot" required></label><button>Save GitHub repository</button>')
    data_status = bot.read_json(bot.DATA / 'update_status.json', {}).get('message', 'Official dataset not downloaded yet.')
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Dubai property bot · Setup</title>
    <style>body{{font:16px system-ui;background:#f3f5f7;color:#182738;max-width:850px;margin:40px auto;padding:0 22px}}h1{{font-size:32px}}section{{background:white;border:1px solid #dae0e7;border-radius:14px;padding:24px;margin:18px 0}}label{{display:block;margin:12px 0}}input:not([type=checkbox]),textarea,select{{display:block;box-sizing:border-box;width:100%;padding:11px;border:1px solid #aab8c8;border-radius:6px;margin-top:6px;font:inherit}}button{{background:#145b57;color:white;border:0;border-radius:7px;padding:12px 18px;cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#e8efee;padding:16px;border-radius:9px}}small{{color:#456}}code{{overflow-wrap:anywhere}}a{{color:#145b57}}</style>
    <h1>Dubai property bot</h1><p><strong>GitHub cloud data · v5</strong></p><p>Your private Telegram assistant. Official public data, cautious matches.</p>
    <pre>{html.escape(message or bot.STATUS['telegram'])}</pre>
    <section><h2>1. Connect your Telegram bot</h2><p>Open <a href="https://t.me/BotFather">@BotFather</a>, send <code>/newbot</code>, choose a name and username, then paste its token below. The token stays on this computer.</p>{'<p>Telegram account paired.</p>' if paired else token_form}{pairing}<p><a href="/">Refresh connection status</a></p></section>
    <section><h2>2. Download your GitHub database</h2><p>GitHub’s cloud runner downloads the official DLD Units dataset, builds the indexed database and publishes a compressed release every Sunday. Your bot checks GitHub automatically at startup and hourly.</p><pre>{html.escape(data_status)}</pre>{github_form}{refresh_form}<p><a href="/">Refresh download status</a></p><p>The first cloud run requires your GitHub account. Use a public repository and standard runner for free Actions. Your Telegram token stays on this computer.</p><p>A single size or bedroom match does not identify an exact unit. All compatible candidates are included in the cloud test report.</p></section>
    <section><h2>3. Try a listing</h2><p>Paste a Dubai listing URL and its visible details. A single similar-sized unit is still only a possible match.</p>{test_form}</section>
    <section><h2>Optional: save authorised listing details</h2><p>Saved details let the bot answer that URL directly next time. These are your supplied facts, not independently verified Property Finder records.</p>{save_form}<hr>{clear_form}</section>
    <p>Runs locally on this computer. Telegram requires an internet connection. Keep the setup address private.</p>
    </html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, body, status=200, kind='text/html; charset=utf-8'):
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-App-Version', 'github-cloud-v5')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{CSRF}'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        return self.headers.get('Host') == f'127.0.0.1:{PORT}'

    def do_GET(self):
        if not self.trusted():
            return self.respond('Forbidden', 403)
        if urlsplit(self.path).path != '/':
            return self.respond('Not found', 404)
        self.respond(page())

    def do_POST(self):
        if not self.trusted() or self.headers.get('Origin') != ORIGIN:
            return self.respond('Forbidden', 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > (250000000 if self.path == '/dataset' else 20000):
                return self.respond('Request too large or empty', 413)
            if self.path == '/dataset':
                if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), CSRF):
                    return self.respond('Forbidden', 403)
                with tempfile.NamedTemporaryFile(suffix='.csv') as tmp:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError('Incomplete upload.')
                        tmp.write(chunk)
                        remaining -= len(chunk)
                    tmp.flush()
                    meta = import_csv(tmp.name, bot.DATA / 'units.sqlite', self.headers.get('X-Source', ''), self.headers.get('X-As-Of', ''))
                return self.respond(f"Loaded {meta['rows']:,} official-source unit rows. Snapshot date: {meta['as_of']}. Coverage is not independently verified.", kind='text/plain; charset=utf-8')
            fields = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            if not secrets.compare_digest(fields.get('csrf', ''), CSRF):
                return self.respond('Forbidden', 403)
            if self.path == '/github-source':
                repo = repository(fields.get('repository', '').strip())
                bot.save_json(bot.DATA / 'github_source.json', {'repository': repo})
                message = 'GitHub repository saved. Download its latest database after the first cloud run succeeds.'
            elif self.path == '/refresh':
                if UPDATE_LOCK.acquire(blocking=False):
                    threading.Thread(target=refresh_data, daemon=True).start()
                    message = 'GitHub database sync started. Refresh this page to see progress.'
                else:
                    message = 'An official download is already running.'
            elif self.path == '/token':
                token = fields.get('token', '').strip()
                if not re.fullmatch(r'\d{5,15}:[A-Za-z0-9_-]{20,100}', token):
                    raise ValueError('That does not look like a BotFather token.')
                with bot.LOCK:
                    if bot.config().get('owner'):
                        raise ValueError('Already paired. Token changes are disabled in this setup page.')
                    bot.save_json(bot.DATA / 'config.json', {'token': token, 'pair_code': secrets.token_urlsafe(18), 'pair_expires': time.time() + 900})
                message = 'Token saved. Send the pairing command below to your new bot.'
            elif self.path in ('/match', '/listing'):
                url = listing_url(fields.get('url', ''))
                facts = parse_details(fields.get('details', ''))
                if self.path == '/listing':
                    if fields.get('authorised') != 'yes':
                        raise ValueError('Confirm that you are authorised to save these details.')
                    if not (facts.get('building') or facts.get('project')):
                        raise ValueError('Include the building or project name.')
                    with bot.LOCK:
                        entries = bot.read_json(bot.DATA / 'listings.json', {})
                        entries[url] = {'facts': facts, 'provenance': 'User supplied authorised listing details', 'saved_at': time.time()}
                        bot.save_json(bot.DATA / 'listings.json', entries)
                    message = 'Listing details saved. Send this URL to your bot after pairing.'
                else:
                    message = format_result(match(bot.DATA / 'units.sqlite', facts))
            elif self.path == '/clear':
                bot.save_json(bot.DATA / 'listings.json', {})
                message = 'Saved listing details deleted.'
            else:
                return self.respond('Not found', 404)
            self.respond(page(message))
        except (ValueError, UnicodeError) as e:
            self.respond(page(str(e)), 400)
        except Exception:
            self.respond(page('The local operation failed. No token or private details were logged. Please retry.'), 500)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    threading.Thread(target=bot.run, daemon=True).start()
    print(f'Setup is ready: {ORIGIN}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
