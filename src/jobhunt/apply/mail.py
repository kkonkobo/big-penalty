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
import unicodedata
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
talent-soft.com profils.org smartrecruiters.com teamtailor.com teamtailor-mail.com greenhouse.io greenhouse.com lever.co workable.com
recruitee.com icims.com taleo.net oracle.com oraclecloud.com digitalrecruiters.com gestmax.fr gestmax.com flatchr.io
eightfold.ai softy.pro softymails.fr'''.split())
SKEW = timedelta(minutes=2)
RESUME_WINDOW = timedelta(hours=24)
_MONTHS = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
_VERIFY = re.compile(r'verif(?:y|ication|izieren)|vérifi|confirm|activat|activ[ea]r|valid[ae]|bestätig', re.I)
_ACCOUNT = re.compile(r'account|compte|konto|e-?mail|courriel', re.I)
# (the path of a link that signs the candidate in: asked for only after a passwordless sign-in was requested on the site)
_SIGN_IN = re.compile(r'(?:^|/)(?:login|log-in|signin|sign-in|magic|auth|connexion)[-_]?link(?:/|$)|(?:^|/)magic[-_]?(?:login|signin)(?:/|$)|passwordless', re.I)
_NOT_ACTIVATION = re.compile(r'reset|password|mot de passe|passwort|sign[ -]?in|log[ -]?in|connexion|anmeld|magic|otp', re.I)
CODE_LABEL = re.compile(r'code|one.time.password|\botp\b|verif|vérifi|bestätig', re.I)
VERIFY_ENDPOINT = re.compile(r'(?:^|/)(?:verify(?:[-_]?email|[-_]?code)?|confirm(?:[-_]?email|[-_]?code)?|'
                             r'email[-_]?(?:verification|confirmation)|verification|otp|activate)/?$', re.I)
MAX_MESSAGES = 10
_CODE = re.compile(r'(?:(?:verification|security|confirmation|one.time)\s+(?:pass)?code|pass(?:code|word)(?:\s+for\s+[a-z ]{3,40})?|code(?:\s+(?:de\s+)?(?:v[ée]rification|confirmation|s[ée]curit[ée]))?|(?:bestätigungs|verifizierungs)[ -]?code|bestätigung|v[ée]rification|\botp\b)'
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


# An employer often mails from its name under another ending than the one of its careers site ("employer.fr" for a site
# on "employer.com"). Those sibling domains are searched too - for names long enough to be a brand, never for a
# platform's own name. What is read from such a mail is still checked against the site itself (`same_recipient`).
_SIBLING_ENDINGS = ('com', 'fr', 'eu', 'net', 'org', 'io', 'co', 'de', 'ch', 'be')


def _siblings(bound):
    domain = accounts.registrable_host(bound.hostname)
    label, _, ending = domain.partition('.')
    if len(label) < 5 or '.' in ending or any(_under(bound.hostname, provider_domain) for provider_domain in ATS_DOMAINS):
        return frozenset()
    return frozenset(f'{label}.{other}' for other in _SIBLING_ENDINGS if other != ending)


def search_domains(bound_url):
    bound = _url(bound_url)
    return ATS_DOMAINS | {accounts.registrable_host(bound.hostname)} | _siblings(bound) if bound else frozenset()


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


def extract(text, bound_url, *, html=False, links_only=False, sender_domain=None, sign_in=False):
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
        if sign_in:
            if _SIGN_IN.search(unquote(parsed.path)):
                found.add(link)
            continue
        evidence = unquote(parsed.path) + ' ' + label
        if (_VERIFY.search(evidence) and not re.search(r'reset|forgot|unsubscribe|désabonn', evidence, re.I)
                and (not links_only or (_ACCOUNT.search(evidence) and not _NOT_ACTIVATION.search(evidence)))):
            found.add(link)
    if len(found) == 1:
        return Verification('link', found.pop())
    if links_only or found or sign_in:
        return None
    if sender_domain is not None:
        host = urlsplit(bound_url).hostname
        bound_domain = accounts.registrable_host(host)
        if any(_under(host, domain) for domain in ATS_DOMAINS):
            # (an ATS mails from its own domain, not from the tenant's host: "noreply@<ats>" for "<tenant>.<ats>")
            vendor = any(_under(host, domain) and _under(sender_domain, domain) for domain in ATS_DOMAINS)
            if not vendor and not _under(sender_domain, host) and not any(same_recipient(bound_url, link) for link, _ in links):
                return None
        elif not _under(sender_domain, bound_domain) and not any(_under(sender_domain, sibling) for sibling in _siblings(urlsplit(bound_url))):
            # An employer whose careers site runs on a vendor's platform under the employer's own domain: the vendor
            # mails the code from its domain, and the mail links to the employer's space there ("<employer>.<vendor>").
            label = bound_domain.split('.')[0]
            vendor = next((domain for domain in ATS_DOMAINS if _under(sender_domain, domain)), None)
            spaces = [(urlsplit(link).hostname or '').lower() for link, _ in links]
            if not (vendor and len(label) >= 4 and any(_under(space, vendor) and label in space.split('.') for space in spaces)):
                return None
    codes = {match[1] for match in _CODE.finditer(text) if any(char.isdigit() for char in match[1]) or match[1].isupper()}
    if not codes and re.search(r'\bcode\b', text, re.I):
        # ("Enter the following code below to complete the verification process: 123456": the one number of the mail
        # that stands alone - two of them and nothing is read)
        codes = {match.group() for match in re.finditer(r'(?<![\w#/=.:-])\d{5,8}(?![\w/:-]|\.\w)', text)}
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


def _search(client, bound_url, started, examined, *, links_only=False, sign_in=False):
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
                             links_only=links_only, sender_domain=sender, sign_in=sign_in)
            if result:
                matches.add(result)
        links = {result for result in matches if result.kind == 'link'}
        selected = links or matches
        if len(selected) == 1:
            return selected.pop()
        if selected:
            return None
    return None


def _lookup(address, password, bound_url, started, examined, links_only, sign_in=False):
    try:
        with _inbox(address, password) as (client, _):
            return _search(client, bound_url, started, examined, links_only=links_only, sign_in=sign_in)
    except Exception:
        pass
    raise MailUnavailable()


# ---------------------------------------------------------------- receipt of an application, by mail
_RECEIPT = re.compile(r'candidature|application|applying|applied|postul|bewerbung|candidatura|solicitud', re.I)
_NOT_RECEIPT = re.compile(r'incompl|inachev|ach[eè]vement|finali[sz]|complete your|terminer votre|v[ée]rifi|verif|password|'
                          r'mot de passe|r[ée]initialis|reset|activ|rappel|reminder|brouillon|draft|bloqu|blocked', re.I)
_TITLE_NOISE = frozenset('stage stagiaire alternance alternant alternante internship intern apprentissage apprenti ingenieur '
                         'ingenieure engineer avec pour dans chez'.split())


def _words(text):
    folded = ''.join(char for char in unicodedata.normalize('NFKD', str(text)) if not unicodedata.combining(char))
    return re.sub(r'[^a-z0-9]+', ' ', folded.lower()).split()


def names_posting(text, title):
    """Does a mail name this posting: most of the telling words of its title (at least two) stand in it."""
    words = [word for word in dict.fromkeys(_words(title)) if len(word) >= 4 and word not in _TITLE_NOISE]
    if len(words) < 2:
        return False
    have = set(_words(text))
    return sum(word in have for word in words) >= max(2, math.ceil(0.6 * len(words)))


def _receipt_lookup(address, password, bound_url, since, title):
    domains = search_domains(bound_url)
    if not domains:
        return False
    parser = BytesParser(policy=policy.default)
    terms = [f'FROM "{domain}"' for domain in sorted(domains)]
    day = f'{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}'
    with _inbox(address, password) as (client, _):
        status, rows = client.uid('SEARCH', None, 'SINCE', day, 'OR ' * (len(terms) - 1) + ' '.join(terms))
        if status != 'OK' or not rows or not rows[0]:
            return False
        uids = [item for item in rows[0].split() if re.fullmatch(rb'[0-9]{1,10}', item)]
        for uid in sorted(uids, key=int, reverse=True)[:MAX_MESSAGES]:
            status, data = client.uid('FETCH', uid, '(INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])')
            meta, raw = _literal(data)
            metadata = b' '.join([meta, *(item for item in data or () if isinstance(item, bytes))])
            stamp = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
            size = re.search(rb'RFC822\.SIZE ([0-9]+)', metadata)
            if status != 'OK' or not stamp:
                continue
            received = datetime.strptime(stamp[1].decode('ascii').strip(), '%d-%b-%Y %H:%M:%S %z')
            if received < since - timedelta(seconds=30):
                break  # (newest first: the rest is older than the send)
            headers = parser.parsebytes(raw, headersonly=True)
            senders = headers.get_all('From', [])
            if len(senders) != 1 or senders[0].defects or len(senders[0].addresses) != 1:
                continue
            if not any(_under(senders[0].addresses[0].domain.lower(), domain) for domain in domains):
                continue
            subject = str(headers.get('Subject', ''))
            if _NOT_RECEIPT.search(subject) or (size and int(size[1]) > 400_000):
                continue
            status, data = client.uid('FETCH', uid, '(BODY.PEEK[])')
            _, whole = _literal(data)
            if status != 'OK' or not whole:
                continue
            body = parser.parsebytes(whole).get_body(preferencelist=('plain', 'html'))
            try:
                text = re.sub(r'<[^>]+>', ' ', body.get_content()) if body is not None else ''
            except Exception:
                text = ''
            if _RECEIPT.search(subject + ' ' + text[:3000]) and names_posting(subject + ' ' + text, title):
                return True
    return False


async def receipt(address, bound_url, since, title):
    """Did the site acknowledge the application by mail? True when a message received since the send click, from the
    employer's own domain or its recruiting platform's, speaks of an application and names this posting. Read-only,
    nothing is kept. Waits up to JOBHUNT_RECEIPT_MAIL_WAIT_S (90 s) for the mail to arrive; False on any doubt."""
    try:
        password = await asyncio.to_thread(_secret, address)
    except MailUnavailable:
        return False
    if not password or not str(title or '').strip():
        return False
    interval = _seconds('JOBHUNT_MAIL_POLL_S', 10, 0.1, 180)
    deadline = asyncio.get_running_loop().time() + _seconds('JOBHUNT_RECEIPT_MAIL_WAIT_S', 90, 0, 600)
    while True:
        try:
            if await asyncio.wait_for(asyncio.to_thread(_receipt_lookup, address, password, bound_url, since, title), timeout=30):
                return True
        except (MailUnavailable, TimeoutError):
            return False
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(interval)


def _seconds(name, default, minimum, maximum):
    try:
        value = float(os.environ.get(name, default))
        if math.isfinite(value) and minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    return default


async def poll(address, bound_url, started, *, links_only=False, sign_in=False):
    """Return one ephemeral verification and, on failure, a fixed diagnostic code. `sign_in`: the site was just asked for a
    passwordless sign-in and mails a link that signs in - only such a link is looked for."""
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
            result = await asyncio.wait_for(asyncio.to_thread(_lookup, address, password, bound_url, started, examined, links_only, sign_in),
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

    async def poll(self, *, sign_in=False):
        return await (self.lookup(sign_in=True) if sign_in else self.lookup())

    async def open(self, page, link, recheck):
        return await self.follow(page, link, recheck)
