"""Private Telegram bot. Standard library only; no scraping or CAPTCHA endpoints."""
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from matching import listing_url, parse_details, match, format_result

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('BOT_DATA_DIR', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
STATUS = {'telegram': 'Waiting for your Telegram bot token.'}


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save_json(path, obj):
    with LOCK:
        temp = Path(str(path) + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(obj, f)
        os.replace(temp, path)


def config():
    with LOCK:
        c = read_json(DATA / 'config.json', {})
        if os.environ.get('TELEGRAM_BOT_TOKEN'):
            c['token'] = os.environ['TELEGRAM_BOT_TOKEN']
        if os.environ.get('TELEGRAM_OWNER_ID'):
            c['owner'] = int(os.environ['TELEGRAM_OWNER_ID'])
        return c


class TelegramError(Exception):
    def __init__(self, code, retry_after=5):
        self.code = code
        self.retry_after = min(max(retry_after, 1), 300)
        super().__init__(f'Telegram request failed ({code}).')


def api(token, method, payload):
    if sys.platform == 'darwin':
        return mac_api(token, method, payload)
    request = Request(f'https://api.telegram.org/bot{token}/{method}',
                      data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=40) as response:
            body = json.load(response)
    except HTTPError as e:
        try:
            body = json.loads(e.read(65536))
        except (ValueError, OSError):
            body = {}
        raise TelegramError(e.code, body.get('parameters', {}).get('retry_after', 5)) from None
    except (URLError, TimeoutError, OSError, ValueError):
        # Never include request URLs: they contain the bot token.
        raise TelegramError('network') from None
    if not body.get('ok'):
        raise TelegramError(body.get('error_code', 'unknown'))
    return body['result']


def mac_api(token, method, payload):
    """Use macOS HTTPS trust; keep credentials out of process arguments and logs."""
    if not re.fullmatch(r'\d{5,15}:[A-Za-z0-9_-]{20,100}', token) or method not in {
            'getMe', 'getWebhookInfo', 'getUpdates', 'sendMessage'}:
        raise TelegramError('configuration')
    # curl config is passed privately over stdin, never saved or placed in argv.
    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    cfg = '\n'.join([
        'url = ' + quote(f'https://api.telegram.org/bot{token}/{method}'),
        'header = "Content-Type: application/json"',
        'data = ' + quote(json.dumps(payload, ensure_ascii=True)),
    ])
    try:
        response = subprocess.run(
            ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--max-time', '40',
             '--proto', '=https', '--config', '-'],
            input=cfg, capture_output=True, text=True, timeout=45, check=False)
        if response.returncode:
            raise TelegramError('network')
        body = json.loads(response.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise TelegramError('network') from None
    if not body.get('ok'):
        raise TelegramError(body.get('error_code', 'unknown'), body.get('parameters', {}).get('retry_after', 5))
    return body['result']


DETAILS = ('Paste the visible listing details in this format (use the actual values):\n'
           'area: …\nbuilding: … (or use project:)\nbedrooms: …\nsize_sqft: …\n'
           'Optional: unit:, floor:, property_id: (a genuine DLD ID, not the listing or permit number).\n'
           'The bot does not fetch restricted Property Finder pages or bypass CAPTCHA.')


class Conversation:
    def __init__(self, db_path, listing_path):
        self.db_path = db_path
        self.listing_path = listing_path
        self.pending = {}

    def handle(self, user, text):
        if len(text) > 8000:
            return 'Please send a shorter message.'
        if text in ('/start', '/help'):
            return ('Send a Property Finder Dubai listing link. I compare the available details with an imported official DLD snapshot. '
                    'If no authorised listing record is saved, I will ask for the visible details.\n'
                    '/cancel clears the current listing. /privacy explains stored data.')
        if text == '/privacy':
            return ('Private bot: only the paired Telegram account can search. Pending listing details stay in memory for 30 minutes; '
                    '/cancel clears them. No message bodies or bot tokens are logged. Telegram processes messages under its own policy. '
                    'Locally saved listing records persist until removed. Only public property fields are imported; no owner names or contacts.')
        if text == '/cancel':
            self.pending.pop(user, None)
            return 'Current listing cleared.'
        if text.startswith('https://') or text.startswith('http://'):
            try:
                url = listing_url(text)
            except ValueError as e:
                return str(e)
            self.pending[user] = (url, time.monotonic())
            record = read_json(self.listing_path, {}).get(url)
            if record and time.time() - record.get('saved_at', 0) <= 30 * 86400:
                return 'Using saved listing details.\n' + format_result(match(self.db_path, record['facts']))
            if record:
                return 'Saved listing details are older than 30 days. Please confirm the current visible details.\n' + DETAILS
            return 'Link accepted. No authorised listing data is saved for this link.\n' + DETAILS
        pending = self.pending.get(user)
        if not pending or time.monotonic() - pending[1] > 1800:
            self.pending.pop(user, None)
            return 'Send the listing URL first; listing sessions expire after 30 minutes.'
        try:
            facts = parse_details(text)
        except ValueError as e:
            return str(e)
        return 'Using the listing details you supplied.\n' + format_result(match(self.db_path, facts))


def run(stop=None):
    stop = stop or threading.Event()
    from release_data import auto_sync
    threading.Thread(target=auto_sync, args=(DATA, stop), daemon=True).start()
    conversation = Conversation(DATA / 'units.sqlite', DATA / 'listings.json')
    offset = 0
    active_token = None
    last_request = 0
    while not stop.is_set():
        c = config()
        token = c.get('token')
        if not token:
            stop.wait(2)
            continue
        try:
            if token != active_token:
                me = api(token, 'getMe', {})
                hook = api(token, 'getWebhookInfo', {})
                if hook.get('url'):
                    STATUS['telegram'] = 'This bot already has a webhook. Use a new BotFather bot for this project.'
                    stop.wait(10)
                    continue
                active_token = token
                fingerprint = hashlib.sha256(token.encode()).hexdigest()
                saved_offset = read_json(DATA / 'offset.json', {})
                offset = saved_offset.get('offset', 0) if saved_offset.get('bot') == fingerprint else 0
                STATUS['telegram'] = f"Connected to @{me['username']}. " + ('Paired.' if c.get('owner') else 'Waiting for pairing.')
            updates = api(token, 'getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message']})
            for update in updates:
                message = update.get('message', {})
                user = message.get('from', {}).get('id')
                chat = message.get('chat', {})
                text = message.get('text', '').strip()
                c = config()
                reply = None
                if chat.get('type') == 'private' and user == chat.get('id'):
                    if not c.get('owner') and c.get('pair_code') and time.time() < c.get('pair_expires', 0):
                        if secrets.compare_digest(text, '/pair ' + c['pair_code']):
                            with LOCK:
                                c['owner'] = user
                                c.pop('pair_code', None)
                                c.pop('pair_expires', None)
                                save_json(DATA / 'config.json', c)
                            STATUS['telegram'] = 'Telegram connected and paired. Ready for listing links.'
                            reply = 'Paired successfully. Only your Telegram account can use this bot. Send a Property Finder listing link.'
                    elif user == c.get('owner'):
                        if time.monotonic() - last_request < 1:
                            reply = 'Please wait a second before your next request.'
                        else:
                            last_request = time.monotonic()
                            reply = conversation.handle(user, text) if text else 'Please send a listing link or text details.'
                if reply:
                    try:
                        api(token, 'sendMessage', {'chat_id': user, 'text': reply, 'link_preview_options': {'is_disabled': True}})
                    except TelegramError as e:
                        if e.code != 403:
                            raise
                offset = update['update_id'] + 1
                save_json(DATA / 'offset.json', {'offset': offset, 'bot': fingerprint})
        except TelegramError as e:
            STATUS['telegram'] = {401: 'Bot token rejected. Enter a fresh token from BotFather.',
                                  409: 'Another copy of this bot is running. Stop it before continuing.'}.get(e.code, 'Telegram unavailable; retrying automatically.')
            stop.wait(e.retry_after)
        except Exception:
            STATUS['telegram'] = 'Local bot error; check the dataset and restart. No private details were logged.'
            stop.wait(5)


if __name__ == '__main__':
    if not config().get('token') or not config().get('owner'):
        raise SystemExit('Complete local setup and pairing before running the deployment worker.')
    run()
