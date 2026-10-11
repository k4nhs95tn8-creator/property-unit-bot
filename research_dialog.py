"""Opt-in, local-only Telegram conversation for the four-stage research engine.

This module never starts Telegram or contacts a site. bot.py selects it only with
KUKU_RESEARCH_MODE=1 after the user explicitly authorises activation.
"""
import json
import os
import time
from pathlib import Path

from four_stage import (analyze, canonical_url, fetch_public_listing, ingest_advert,
                        ingest_permit, message, number, bedroom_count, SQM_PER_SQFT)
from owner_records import lookup as lookup_owner

DETAILS = ('Send publicly visible details as:\n'
           'area: Al Barsha South Fifth\nbuilding: Seslia Tower\n'
           'bedrooms: 2\nsize_sqft: 978\n'
           'You can also paste the Trakheesi permit card copied with Command+A.\n'
           'Do NOT supply a known unit number or registry property ID during blind testing.')


def parse_visible_details(text):
    allowed = {'building', 'project', 'area', 'bedrooms', 'size_sqft', 'size_sqm'}
    parsed = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(':')
        key = key.strip().lower().replace(' ', '_')
        if not sep or key not in allowed or not value.strip() or key in parsed:
            raise ValueError('Use the visible area, building, bedrooms and size fields. Do not supply the known unit number.')
        parsed[key] = value.strip()
    if not (parsed.get('building') or parsed.get('project')):
        raise ValueError('Please provide the visible building/project name.')
    if parsed.get('size_sqm') and parsed.get('size_sqft'):
        raise ValueError('Use one size unit, not both.')
    if parsed.get('bedrooms') is not None:
        bedrooms = bedroom_count(parsed['bedrooms'])
        if bedrooms is None:
            raise ValueError('Bedrooms must be Studio or an integer.')
        parsed['bedrooms'] = bedrooms
    if parsed.get('size_sqft'):
        size = number(parsed.pop('size_sqft'))
        if not size:
            raise ValueError('Use a positive finite property size.')
        parsed['size_sqm'] = size * SQM_PER_SQFT
    elif parsed.get('size_sqm'):
        size = number(parsed['size_sqm'])
        if not size:
            raise ValueError('Use a positive finite property size.')
        parsed['size_sqm'] = size
    return parsed


class ResearchConversation:
    """Messages are session-scoped. Only whitelisted public fields reach storage."""

    def __init__(self, db_path, listing_path):
        self.db_path = Path(db_path)
        self.listing_path = Path(listing_path)
        self.cache_path = self.listing_path.parent / 'observation-index.sqlite'
        self.owner_store = self.listing_path.parent / 'owners.sqlite'
        self.crosswalk_store = self.listing_path.parent / 'permit-links.sqlite'
        self.pending = {}

    def _saved(self, url):
        try:
            entries = json.loads(self.listing_path.read_text())
        except (OSError, ValueError):
            return None
        entry = entries.get(url)
        if not isinstance(entry, dict) or not isinstance(entry.get('facts'), dict):
            return None
        if time.time() - entry.get('saved_at', 0) > 30 * 86400:
            return None
        return {'url': url, 'facts': entry['facts'], 'source': 'authorised_local_listing_details'}

    def _report(self, session):
        if not session.get('advert'):
            return 'Permit captured, but the advert still needs visible building and size information.\n' + DETAILS
        try:
            result = analyze(session['advert'], self.db_path,
                             permit_payload=session.get('permit'), cache_path=self.cache_path,
                             crosswalk_path=self.crosswalk_store,
                             owner_db=self.owner_store,
                             include_owner=(os.environ.get('KUKU_OWNER_LOOKUP') == '1' and
                                            os.environ.get('KUKU_AUTO_OWNER_LOOKUP') == '1'),
                             url=session['url'])
            return message(result)
        except (ValueError, TypeError):
            return 'Public capture could not be validated. Please send visible details.\n' + DETAILS

    def handle(self, user, text):
        text = text.strip()
        if len(text) > 8000:
            return 'Text is too long for Telegram. Save the public capture locally and run four_stage.py, or send concise visible details.'
        if text in ('/start', '/help'):
            return ('Kuku four-stage research (OFFLINE-FRIENDLY, no guaranteed unit detection).\n'
                    'Send a Property Finder, Bayut or Dubizzle advert URL. The bot checks local authorised captures '
                    'and the DLD snapshot. If needed, paste the Trakheesi card or visible details.\n'
                    'No account, paid API, anti-bot bypass or background scraping.\n'
                    '/owner <DLD property ID> reads an explicitly authorised local contact (if enabled).\n'
                    '/cancel clears the current session. /privacy describes what is stored.')
        if text == '/cancel':
            self.pending.pop(user, None)
            return 'Current research session cleared.'
        if text == '/privacy':
            return ('Research sessions stay in memory for 30 minutes. The local observation-index.sqlite stores '
                    'only advert URLs, permit GUIDs and public size/building/bedroom descriptors for deduplication; '
                    'no owners, contact details, bot tokens or identity answers are saved. '
                    'Optional owners.sqlite contains separately authorised contact data, read only with '
                    '/owner and the KUKU_OWNER_LOOKUP opt-in. Telegram receives replies you request. '
                    'Research mode is off by default. Telegram processes messages under its own policy.')
        if text == '/owner' or text.startswith('/owner '):
            if os.environ.get('KUKU_OWNER_LOOKUP') != '1':
                return 'Owner contact lookup is disabled. No owner contacts were retrieved.'
            parts = text.split()
            if len(parts) != 2:
                return 'Use /owner <genuine DLD property ID>; marketing permit numbers are not property IDs.'
            result = lookup_owner(self.owner_store, parts[1], authorised=True)
            if result['status'] != 'source_reported_contact':
                return 'Owner lookup: ' + result['status'].replace('_', ' ') + '. No owner details available.'
            return ('LOCAL SOURCE-REPORTED CONTACT (NOT VERIFIED CURRENT OWNER)\n'
                    f"DLD property ID (supplied, not derived from advert): {result['property_id']}\n"
                    f"Name: {result['contact_name']}\nRole: {result['contact_role']}\n"
                    f"Phone: {result['contact_phone']}\nEmail: {result['contact_email']}\n"
                    f"Observed: {result['observed_date']} ({result['age_days']} days ago)\n"
                    f"Source: {result['source_name']} / {result['source_reference']}\n"
                    'This does not establish advert-to-unit identity or current title-deed ownership.')
        if text.startswith(('http://', 'https://')):
            try:
                _, canonical = canonical_url(text)
            except ValueError as error:
                return str(error)
            session = {'url': canonical, 'created': time.monotonic(), 'advert': self._saved(canonical), 'permit': None}
            self.pending[user] = session
            if not session['advert'] and os.environ.get('KUKU_FETCH_PUBLIC') == '1':
                try:
                    session['advert'] = fetch_public_listing(canonical)
                except ValueError:
                    pass  # Public access unavailable. Do not bypass or retry.
            if not session['advert']:
                return 'Advert accepted. No locally saved public facts available.\n' + DETAILS
            return self._report(session)
        session = self.pending.get(user)
        if not session or time.monotonic() - session['created'] > 1800:
            self.pending.pop(user, None)
            return 'Send a supported advert URL first. Sessions expire after 30 minutes.'
        if 'Transaction Number' in text and 'Listing Number' in text:
            try:
                session['permit'] = ingest_permit(text)
            except ValueError as error:
                return str(error)
            return self._report(session)
        if text.startswith('{'):
            try:
                data = json.loads(text)
                # JSON can be a permit record or a compact public advert capture.
                if isinstance(data, dict) and ('property_records' in data or 'PermitNumber' in data):
                    session['permit'] = ingest_permit(data)
                else:
                    session['advert'] = ingest_advert(data, url=session['url'])
            except (ValueError, TypeError):
                return 'Malformed public JSON. Send concise visible details instead.\n' + DETAILS
            return self._report(session)
        try:
            facts = parse_visible_details(text)
        except ValueError as error:
            return str(error)
        session['advert'] = {'url': session['url'], 'facts': facts}
        return self._report(session)