"""Transient, read-only access to verification mail. No messages leave this module."""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import math
import os
import re
import ssl
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from jobhunt.apply import accounts
from jobhunt.apply.errors import ApplyError

SERVICE = 'big-penalty-imap'
PROVIDERS = {'gmail.com': ('imap.gmail.com', 993), 'googlemail.com': ('imap.gmail.com', 993)}
ATS_DOMAINS = frozenset('''myworkday.com myworkdayjobs.com myworkdaysite.com successfactors.com successfactors.eu sap.com
talent-soft.com profils.org smartrecruiters.com teamtailor.com greenhouse.io greenhouse.com lever.co workable.com
recruitee.com icims.com taleo.net oracle.com oraclecloud.com digitalrecruiters.com gestmax.fr gestmax.com flatchr.io'''.split())
SKEW = timedelta(minutes=2)
RESUME_WINDOW = timedelta(hours=24)
_MONTHS = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
_VERIFY = re.compile(r'verif(?:y|ication|izieren)|vérifi|confirm|activat|activ[ea]r|valid[ae]|bestätig', re.I)
_ACCOUNT = re.compile(r'account|compte|konto|e-?mail|courriel', re.I)
_NOT_ACTIVATION = re.compile(r'reset|password|mot de passe|passwort|sign[ -]?in|log[ -]?in|connexion|anmeld|magic|otp', re.I)
CODE_LABEL = re.compile(r'code|one.time.password|\botp\b|verif|vérifi|bestätig', re.I)
VERIFY_ENDPOINT = re.compile(r'(?:^|/)(?:verify(?:[-_]?email|[-_]?code)?|confirm(?:[-_]?email|[-_]?code)?|'
                             r'email[-_]?(?:verification|confirmation)|verification|otp|activate)/?$', re.I)
MAX_MESSAGES = 10
_CODE = re.compile(r'(?:(?:verification|security|confirmation|one.time)\s+code|code(?:\s+(?:de\s+)?(?:v[ée]rification|confirmation|s[ée]curit[ée]))?|(?:bestätigungs|verifizierungs)[ -]?code|bestätigung|v[ée]rification|\botp\b)'
                   r'(?:\s+(?:is|est|ist|lautet))?[\s:=–—-]{1,12}([A-Z0-9]{4,8})\b', re.I)
_PLAIN_URL = re.compile(r'https://[^\s<>"\']+', re.I)
_TOKEN = re.compile(rb'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^()\s"{}]+)')


class MailUnavailable(Exception):
    """Do not propagate backend, IMAP or parsing exception text."""


def _keychain():
    if sys.platform != 'darwin':
        raise ApplyError('Trousseau macOS requis.')
    from keyring.backends.macOS import Keyring
    backend = Keyring()
    return accounts.KeyringBackend(backend)


def provider(address):
    return PROVIDERS.get(address.rpartition('@')[2].lower()) if address else None


def set_password(address, value):
    value = value.strip().replace(' ', '')
    if not provider(address) or not re.fullmatch(r'[A-Za-z0-9]{16}', value):
        raise ApplyError('Adresse Gmail ou mot de passe d’application invalide.')
    _keychain().set(SERVICE, address, value, SERVICE)


def forget(address):
    backend = _keychain()
    if backend.get(SERVICE, address):
        backend.delete(SERVICE, address)
        if backend.get(SERVICE, address):
            raise ApplyError('Suppression du mot de passe impossible.')


def profile_address(profile_id):
    from jobhunt import db
    from jobhunt.apply.facts import identity_answers
    from jobhunt.cv.master import MasterError, load_master

    profile = db.get_profile(profile_id)
    if profile is None:
        raise ApplyError('Profil introuvable.')
    try:
        master = load_master(profile_id)
    except MasterError:
        master = None
    address = identity_answers(profile, master if master and master.is_confirmed else None, {})['email']
    if not provider(address):
        raise ApplyError('Adresse Gmail requise.')
    return address


def _secret(address):
    if not provider(address) or sys.platform != 'darwin':
        return None
    try:
        return _keychain().get(SERVICE, address)
    except Exception:
        pass
    raise MailUnavailable()


@contextlib.contextmanager
def _inbox(address, password):
    client = None
    try:
        host, port = provider(address)
        client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=5)
        client.debug = 0
        client.login(address, password)
        status, rows = client.select('INBOX', readonly=True)
        if status != 'OK':
            raise MailUnavailable()
        yield client, int(rows[0])
        return
    except (OSError, imaplib.IMAP4.error, ValueError, MailUnavailable):
        pass
    finally:
        if client is not None:
            with contextlib.suppress(OSError, imaplib.IMAP4.error):
                client.logout()
    raise MailUnavailable()


def test_inbox(address):
    password = _secret(address)
    if not password:
        raise MailUnavailable()
    with _inbox(address, password) as (_, count):
        return count


def _under(host, domain):
    return bool(host and domain and (host == domain or host.endswith('.' + domain)))


def _url(value):
    if not value or len(value) > 4096 or any(ord(char) < 33 or char == '\\' for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == 'https' and parsed.netloc == parsed.hostname and parsed.hostname
                and not re.search(r'%(?:00|2f|5c|2e)|(?:^|/)\.{1,2}(?:/|$)', parsed.path, re.I)):
            return parsed
    except ValueError:
        pass
    return None


def _tenant(parsed):
    host = parsed.hostname
    if accounts.is_workday_host(host):
        scope = accounts.workday_site(urlunsplit(parsed._replace(query='', fragment='')))
        if scope:
            return 'workday', scope if ':' in scope else (host, scope.casefold())
        return None
    if _under(host, 'smartrecruiters.com'):
        match = re.match(r'^/(?:oneclick-ui/company/)?([A-Za-z0-9_-]+)/', parsed.path)
        if match and (host in {'jobs.smartrecruiters.com', 'careers.smartrecruiters.com'}
                      or parsed.path.startswith('/oneclick-ui/company/')):
            return 'smartrecruiters', match[1].casefold()
    if host in {'boards.greenhouse.io', 'job-boards.greenhouse.io', 'boards.eu.greenhouse.io',
                'job-boards.eu.greenhouse.io', 'jobs.lever.co', 'jobs.eu.lever.co'}:
        match = re.match(r'^/([A-Za-z0-9_-]+)/', parsed.path)
        if match:
            return host, match[1].casefold()
    return None


def same_recipient(bound_url, target):
    bound, candidate = _url(bound_url), _url(target)
    if not bound or not candidate:
        return False
    left, right = _tenant(bound), _tenant(candidate)
    if left or right or accounts.is_workday_host(bound.hostname):
        return left is not None and left == right
    domain = accounts.registrable_host(bound.hostname)
    if any(_under(bound.hostname, provider_domain) for provider_domain in ATS_DOMAINS):
        return bound.hostname == candidate.hostname and bound.hostname != domain and not _shared_host(bound.hostname)
    return _under(candidate.hostname, domain)


def _shared_host(host):
    return host.split('.')[0] in {'www', 'jobs', 'careers', 'apply', 'boards', 'job-boards', 'recruiting', 'recruitment'}


def search_domains(bound_url):
    bound = _url(bound_url)
    return ATS_DOMAINS | {accounts.registrable_host(bound.hostname)} if bound else frozenset()


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.label = [], [], []
        self.href = ''
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'template'}:
            self.hidden += 1
        if not self.hidden and tag == 'a':
            self.href = dict(attrs).get('href') or ''
            self.label = []

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'template'}:
            self.hidden = max(0, self.hidden - 1)
        if tag == 'a' and self.href:
            self.links.append((self.href, ' '.join(self.label)))
            self.href = ''

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)
            if self.href:
                self.label.append(data)


@dataclass(frozen=True)
class Verification:
    kind: str
    value: str = field(repr=False)


def extract(text, bound_url, *, html=False, links_only=False, sender_domain=None):
    if html:
        markup = _HTML()
        markup.feed(text)
        text = ' '.join(markup.text)
        links = markup.links
    else:
        links = [(match.group().rstrip('.,);]'), text[max(0, match.start() - 100):match.start()].split('\n\n')[-1])
                 for match in _PLAIN_URL.finditer(text)]
    found = set()
    for link, label in links:
        parsed = _url(link)
        if not parsed or not same_recipient(bound_url, link):
            continue
        evidence = unquote(parsed.path) + ' ' + label
        if (_VERIFY.search(evidence) and not re.search(r'reset|forgot|unsubscribe|désabonn', evidence, re.I)
                and (not links_only or (_ACCOUNT.search(evidence) and not _NOT_ACTIVATION.search(evidence)))):
            found.add(link)
    if len(found) == 1:
        return Verification('link', found.pop())
    if links_only or found:
        return None
    if sender_domain is not None:
        host = urlsplit(bound_url).hostname
        bound_domain = accounts.registrable_host(host)
        if any(_under(host, domain) for domain in ATS_DOMAINS):
            if not _under(sender_domain, host) and not any(same_recipient(bound_url, link) for link, _ in links):
                return None
        elif not _under(sender_domain, bound_domain):
            return None
    codes = {match[1] for match in _CODE.finditer(text) if any(char.isdigit() for char in match[1]) or match[1].isupper()}
    return Verification('code', codes.pop()) if len(codes) == 1 else None


def _literal(rows):
    for item in rows or ():
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item
    return b'', b''


def _structure(raw):
    parsed = []
    stack = [parsed]
    pos = 0
    while len(stack) > 1 or not parsed:
        match = _TOKEN.match(raw, pos)
        if not match or len(stack) > 24:
            return []
        token = match[1]
        pos = match.end()
        if token == b'(':
            child = []
            stack[-1].append(child)
            stack.append(child)
        elif token == b')':
            stack.pop()
        else:
            stack[-1].append(token.strip(b'"'))
    return parsed[0] if parsed and isinstance(parsed[0], list) else []


def _text_parts(node, section=''):
    if not node:
        return
    if isinstance(node[0], list):
        for index, child in enumerate(node, 1):
            if not isinstance(child, list):
                break
            yield from _text_parts(child, f'{section}.{index}' if section else str(index))
    elif (len(node) >= 8 and node[0].lower() == b'text' and isinstance(node[1], bytes)
          and node[1].lower() in {b'plain', b'html'} and isinstance(node[5], bytes)
          and not (len(node) > 9 and isinstance(node[9], list) and node[9] and node[9][0].lower() == b'attachment')
          and not (isinstance(node[2], list) and any(isinstance(key, bytes) and key.lower() == b'name' for key in node[2][::2]))):
        params = node[2] if isinstance(node[2], list) else []
        charset = next((value for key, value in zip(params[::2], params[1::2])
                        if isinstance(key, bytes) and key.lower() == b'charset' and isinstance(value, bytes)
                        and re.fullmatch(rb'[\w-]{1,40}', value)), b'utf-8')
        encoding = node[5].lower()
        if encoding in {b'7bit', b'8bit', b'binary', b'base64', b'quoted-printable'}:
            yield section or '1', node[1].lower(), charset, encoding


def _search(client, bound_url, started, examined, *, links_only=False):
    since = started.astimezone(UTC) - SKEW
    domains = search_domains(bound_url)
    if not domains or len(examined) >= MAX_MESSAGES:
        return None
    terms = [f'FROM "{domain}"' for domain in sorted(domains)]
    day = f'{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}'
    status, rows = client.uid('SEARCH', None, 'SINCE', day, 'OR ' * (len(terms) - 1) + ' '.join(terms))
    if status != 'OK' or not rows or not rows[0]:
        return None
    parser = BytesParser(policy=policy.default)
    candidates = []
    uids = {item for item in rows[0].split() if re.fullmatch(rb'[0-9]{1,10}', item)} - examined
    for uid in sorted(uids, key=int, reverse=True)[:MAX_MESSAGES - len(examined)]:
        examined.add(uid)
        status, data = client.uid('FETCH', uid, '(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM)]<0.2048>)')
        meta, raw = _literal(data)
        metadata = b' '.join([meta, *(item for item in data or () if isinstance(item, bytes))])
        stamp = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
        if status != 'OK' or not stamp:
            continue
        received = datetime.strptime(stamp[1].decode('ascii').strip(), '%d-%b-%Y %H:%M:%S %z')
        if not since <= received <= min(datetime.now(UTC) + SKEW, started + RESUME_WINDOW):
            continue
        headers = parser.parsebytes(raw, headersonly=True).get_all('From', [])
        if len(headers) != 1 or headers[0].defects or len(headers[0].addresses) != 1:
            continue
        sender = headers[0].addresses[0].domain.lower()
        if any(_under(sender, domain) for domain in domains):
            candidates.append((received, uid, sender))
    for _, uid, sender in sorted(candidates, reverse=True):
        status, data = client.uid('FETCH', uid, '(BODYSTRUCTURE)')
        if status != 'OK' or not data or not isinstance(data[0], bytes):
            continue
        _, sep, raw = data[0].partition(b'BODYSTRUCTURE ')
        if not sep or len(raw) > 32768:
            continue
        matches = set()
        for section, subtype, charset, encoding in list(_text_parts(_structure(raw)))[:4]:
            status, data = client.uid('FETCH', uid, f'(BODY.PEEK[{section}]<0.32768>)')
            _, raw = _literal(data)
            if status != 'OK' or not raw or len(raw) >= 32768:
                continue
            part = parser.parsebytes(b'Content-Type: text/' + subtype + b'; charset=' + charset +
                                     b'\r\nContent-Transfer-Encoding: ' + encoding + b'\r\n\r\n' + raw)
            result = extract(part.get_content(errors='replace'), bound_url, html=subtype == b'html',
                             links_only=links_only, sender_domain=sender)
            if result:
                matches.add(result)
        links = {result for result in matches if result.kind == 'link'}
        selected = links or matches
        if len(selected) == 1:
            return selected.pop()
        if selected:
            return None
    return None


def _lookup(address, password, bound_url, started, examined, links_only):
    try:
        with _inbox(address, password) as (client, _):
            return _search(client, bound_url, started, examined, links_only=links_only)
    except Exception:
        pass
    raise MailUnavailable()


def _seconds(name, default, minimum, maximum):
    try:
        value = float(os.environ.get(name, default))
        if math.isfinite(value) and minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    return default


async def poll(address, bound_url, started, *, links_only=False):
    """Return one ephemeral verification and, on failure, a fixed diagnostic code."""
    try:
        password = await asyncio.to_thread(_secret, address)
    except MailUnavailable:
        return None, 'email_auth_failed'
    if not password:
        return None, None
    interval = _seconds('JOBHUNT_MAIL_POLL_S', 10, 0.1, 180)
    timeout = _seconds('JOBHUNT_MAIL_TIMEOUT_S', 180, 0, 600)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    examined: set[bytes] = set()
    while True:
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_lookup, address, password, bound_url, started, examined, links_only),
                                            timeout=10 if links_only else max(0.05, deadline - loop.time()))
        except MailUnavailable:
            return None, 'email_auth_failed'
        except TimeoutError:
            return None, 'email_not_found'
        if result:
            return result, None
        if links_only or len(examined) >= MAX_MESSAGES or loop.time() >= deadline:
            return None, 'email_not_found'
        await asyncio.sleep(min(interval, max(0, deadline - loop.time())))
        if loop.time() >= deadline:
            return None, 'email_not_found'


async def open_link(context, bound_url, link, recheck):
    """Use the context's cookie jar, with scripts disabled and every redirect checked before a GET."""
    try:
        for _ in range(6):
            if not same_recipient(bound_url, link) or await recheck():
                return False
            response = await context.request.get(link, max_redirects=0, timeout=5000)
            try:
                if response.status not in {301, 302, 303, 307, 308}:
                    return 200 <= response.status < 300
                location = response.headers.get('location')
                if not location:
                    return False
                link = urljoin(link, location)
            finally:
                await response.dispose()
    except Exception:
        pass
    return False


@dataclass
class BrowserVerification:
    lookup: Callable[[], Awaitable[tuple[Verification | None, str | None]]]
    follow: Callable[..., Awaitable[bool]]

    async def poll(self):
        return await self.lookup()

    async def open(self, page, link, recheck):
        return await self.follow(page, link, recheck)
