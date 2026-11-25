from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from typer.testing import CliRunner

from jobhunt import db
from jobhunt.apply import accounts, engine, mail, service, store
from jobhunt.apply.types import DispatchDiagnostics, FormField, PageSnapshot, RunResult, StepLog
from jobhunt.cli import app as cli
from jobhunt.config import get_settings
from jobhunt.cv import store as cvstore

from test_apply_service import new_app
from cv_fakes import save_confirmed

ADDRESS = 'candidate@gmail.com'
PASSWORD = 'Nopq0123FakePass'
POSTING = 'https://jobs.employer.test/jobs/123'
LINK = 'https://employer.test/verify-account?token=only-in-memory'
CODE = '418726'


class FakeIMAP:
    def __init__(self, *args, messages=None, auth_error=False, **kwargs):
        self.commands = []
        self.messages = messages or {}
        self.auth_error = auth_error
        self.args = args
        self.tls = kwargs['ssl_context']

    def login(self, user, password):
        self.commands.append(('LOGIN',))
        assert (user, password) == (ADDRESS, PASSWORD)
        if self.auth_error:
            raise mail.imaplib.IMAP4.error(f'{PASSWORD} {ADDRESS}')
        return 'OK', [b'']

    def select(self, mailbox, *, readonly):
        assert readonly
        self.commands.append(('EXAMINE', mailbox))
        return 'OK', [str(len(self.messages)).encode()]

    def uid(self, action, uid, *args):
        self.commands.append((action, uid, *args))
        if action == 'SEARCH':
            return 'OK', [b' '.join(self.messages)]
        assert action == 'FETCH'
        date, sender, body = self.messages[uid]
        if args[0].startswith('(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM)]'):
            stamp = date.strftime('%d-%b-%Y %H:%M:%S %z').encode()
            return 'OK', [(b'INTERNALDATE "' + stamp + b'"', f'From: {sender}\r\n\r\n'.encode())]
        if args[0] == '(BODYSTRUCTURE)':
            return 'OK', [b'1 (BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "8BIT" 100 2)'
                          b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 1000) "MIXED"))']
        assert args[0] == '(BODY.PEEK[1]<0.32768>)'
        return 'OK', [(b'BODY[1]', body.encode())]

    def logout(self):
        self.commands.append(('LOGOUT',))


def fake_imap(monkeypatch, **kwargs):
    instances = []

    def create(*args, **opts):
        client = FakeIMAP(*args, **opts, **kwargs)
        instances.append(client)
        return client

    monkeypatch.setattr(mail.imaplib, 'IMAP4_SSL', create)
    monkeypatch.setattr(mail, '_secret', lambda _: PASSWORD)
    return instances


async def test_mail_reads_newest_matching_message_only(monkeypatch, caplog, capsys):
    started = datetime.now(UTC).replace(microsecond=0)
    sender = 'private@mail.employer.test'
    instances = fake_imap(monkeypatch, messages={
        b'1': (started - timedelta(seconds=30), sender, f'Verify your account: {LINK}'),
        b'2': (started - timedelta(minutes=3), sender, 'Verification code: 900001'),
        b'3': (started, '"employer.test" <private@employer.test.evil.test>', 'Verification code: 900002'),
        b'4': (started - timedelta(seconds=10), sender, f'Verification code: {CODE}'),
    })
    result, status = await mail.poll(ADDRESS, POSTING, started)
    assert (result.kind, result.value, status) == ('code', CODE, None)
    client, = instances
    assert client.args == ('imap.gmail.com', 993)
    assert client.tls.check_hostname and client.tls.verify_mode == mail.ssl.CERT_REQUIRED
    assert [command[0] for command in client.commands] == ['LOGIN', 'EXAMINE', 'SEARCH', 'FETCH', 'FETCH', 'FETCH',
                                                           'FETCH', 'FETCH', 'FETCH', 'LOGOUT']
    search = client.commands[2]
    assert search[2:4] == ('SINCE', (started - mail.SKEW).strftime('%d-%b-%Y'))
    assert 'FROM "employer.test"' in search[4] and 'FROM "myworkday.com"' in search[4]
    assert client.commands[-3:-1] == [('FETCH', b'4', '(BODYSTRUCTURE)'), ('FETCH', b'4', '(BODY.PEEK[1]<0.32768>)')]
    report = capsys.readouterr()
    for secret in (sender, LINK, CODE, PASSWORD, ADDRESS):
        assert secret not in report.out + report.err + caplog.text + repr(result)


async def test_poll_does_not_fetch_same_message_twice(monkeypatch):
    started = datetime.now(UTC)
    messages = {b'1': (started, 'sender@employer.test', 'No verification')}
    instances = fake_imap(monkeypatch, messages=messages)
    monkeypatch.setenv('JOBHUNT_MAIL_POLL_S', '0.1')
    monkeypatch.setenv('JOBHUNT_MAIL_TIMEOUT_S', '0.3')
    assert await mail.poll(ADDRESS, POSTING, started) == (None, 'email_not_found')
    assert len(instances) >= 2
    assert sum(command[0] == 'FETCH' for client in instances for command in client.commands) == 3


@pytest.mark.parametrize(('bound', 'candidate', 'allowed'), [
    (POSTING, LINK, True),
    (POSTING, 'http://employer.test/verify', False),
    (POSTING, 'https://employer.test.evil.test/verify', False),
    (POSTING, 'https://employer.test@evil.test/verify', False),
    (POSTING, 'https://employer.test/apply', False),
    ('https://jobs.smartrecruiters.com/Company/123',
     'https://www.smartrecruiters.com/oneclick-ui/company/Company/verify', True),
    ('https://jobs.smartrecruiters.com/Company/123',
     'https://jobs.smartrecruiters.com/Other/verify', False),
    ('https://company.wd1.myworkdayjobs.com/en-US/Careers/job/Role_R123',
     'https://company.wd1.myworkdayjobs.com/en-US/Careers/verify?token=abc', True),
    ('https://company.wd1.myworkdayjobs.com/en-US/Careers/job/Role_R123',
     'https://company.wd1.myworkdayjobs.com/en-US/Other/verify?token=abc', False),
])
def test_link_scope(bound, candidate, allowed):
    result = mail.extract(f'<a href="{candidate}">Open</a>', bound, html=True)
    assert bool(result) == allowed


def test_account_resume_rejects_magic_links_and_codes():
    assert mail.extract(f'Activate your account:\n{LINK}', POSTING, links_only=True).kind == 'link'
    assert mail.extract('Confirm sign-in: https://employer.test/confirm-login', POSTING, links_only=True) is None
    assert mail.extract(f'Code de vérification : {CODE}', POSTING, links_only=True) is None
    assert mail.extract('Bestätigungscode: A2B3', POSTING).value == 'A2B3'


@pytest.mark.parametrize('auth_error', [True, False])
async def test_unavailable_mail_preserves_handoff(monkeypatch, auth_error, caplog, capsys):
    instances = fake_imap(monkeypatch, auth_error=auth_error)
    monkeypatch.setenv('JOBHUNT_MAIL_TIMEOUT_S', '0.1')
    monkeypatch.setenv('JOBHUNT_MAIL_POLL_S', '0.1')
    flow = fake_flow()
    flow.email_verification = mail.BrowserVerification(lambda: mail.poll(ADDRESS, POSTING, db.now()), AsyncMock())
    assert not await flow._verify_email(PageSnapshot(url=POSTING), Mock())
    result = await flow._email_hold(StepLog(index=1, url=POSTING, page_kind='email_verification'))
    assert result.status == 'needs_email'
    assert result.dispatch_diagnostics.email == ('email_auth_failed' if auth_error else 'email_not_found')
    assert instances[0].commands[-1][0] == 'LOGOUT'
    output = capsys.readouterr()
    assert PASSWORD not in result.model_dump_json() + output.out + output.err + caplog.text


def fake_flow():
    flow = engine._Run.__new__(engine._Run)
    flow.email_attempted = False
    flow.final_clicked = flow.final_awaiting = flow.app_touched = False
    flow.before_submit = AsyncMock(return_value=[])
    flow.autonomous = SimpleNamespace(write_recipient=lambda _: True, document_nonce='nonce', target=AsyncMock())
    flow.page = SimpleNamespace(url=POSTING, evaluate=AsyncMock(return_value='nonce'), is_closed=lambda: False)
    flow.diagnostics = DispatchDiagnostics()
    flow.secrets, flow.consent_codes = set(), set()
    flow._shot = AsyncMock()
    return flow


async def test_code_is_typed_only_into_the_guarded_field():
    flow = fake_flow()
    flow.email_verification = mail.BrowserVerification(AsyncMock(return_value=(mail.Verification('code', CODE), None)), AsyncMock())
    element = SimpleNamespace(evaluate=AsyncMock(return_value={'ok': True, 'lone': True, 'name': 'code',
                                                                 'label': 'one-time-code', 'size': 6, 'value': ''}),
                              fill=AsyncMock())
    flow._el = AsyncMock(return_value=element)
    snap = PageSnapshot(url=POSTING, fields=[FormField(ref='otp', kind='text', label='Code', name='code',
                                                     autocomplete='one-time-code')])
    assert not await flow._verify_email(snap, Mock())
    flow.autonomous.target.assert_awaited_once_with(element)
    element.fill.assert_awaited_once_with(CODE, timeout=engine.ACTION_TIMEOUT_MS)
    assert flow.diagnostics.email == 'email_code_used' and CODE not in flow._r(f'Code: {CODE}')
    assert not await flow._verify_email(snap, Mock())
    element.fill.assert_awaited_once()


async def test_context_get_checks_redirect_before_request():
    response = SimpleNamespace(status=302, headers={'location': 'https://foreign.test/verify'}, dispose=AsyncMock())
    browser = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
    assert not await mail.open_link(browser, POSTING, LINK, AsyncMock(return_value=[]))
    browser.request.get.assert_awaited_once_with(LINK, max_redirects=0, timeout=5000)
    response.dispose.assert_awaited_once()


async def test_resume_unblocks_only_the_bound_application(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), 'apply_autonomous', True)
    master = save_confirmed(profile.id)
    job, application = new_app(profile, master, tmp_path, approve=True, url=POSTING)
    job = db.get_job(job.id)
    run = store.start_run(application.id, application.current_rev, 'submit', 'user', lambda _: tmp_path,
                          origin_sha=service.origin_digest(job.origin_url))
    dispatch = service._reserve(run, job, 'submit', 'user')
    held = store.finish_and_settle(run.id, RunResult(status='needs_email'), browser_attempt=dispatch.attempt, browser_clear=True)
    monkeypatch.setattr(mail, 'profile_address', lambda _: ADDRESS)
    monkeypatch.setattr(accounts, 'set_state', Mock())
    monkeypatch.setattr(mail, 'poll', AsyncMock(return_value=(mail.Verification('link', LINK), None)))
    response = SimpleNamespace(status=200, headers={}, dispose=AsyncMock())
    context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
    pool = SimpleNamespace(context=AsyncMock(return_value=context))
    assert store.has_site_handoff(application.id)
    assert await service.resume_email(profile.id, pool=pool) == 1
    mail.poll.assert_awaited_once_with(ADDRESS, POSTING, held.started_at, links_only=True)
    pool.context.assert_awaited_once_with(profile.id, 'apply-auto-jobs.employer.test', headless=True)
    next_run = store.get_run(held.id)
    assert next_run.email_resumed and next_run.result.dispatch_diagnostics.email == 'email_link_used'
    assert not store.has_site_handoff(application.id)
    assert not service._waiting_on_user(cvstore.get_application(application.id), db.get_job(job.id), next_run)
    assert LINK not in next_run.model_dump_json()
    assert await service.resume_email(profile.id, pool=pool) == 0


def test_cli_password_stdin_never_argv(monkeypatch):
    runner = CliRunner()
    backend = Mock()
    monkeypatch.setattr(mail, '_keychain', lambda: backend)
    monkeypatch.setattr(mail, 'profile_address', lambda _: ADDRESS)
    secret = '0123456789abcdef'
    result = runner.invoke(cli, ['mail', 'set-password', '1'], input=secret + '\n')
    assert result.exit_code == 0 and secret not in result.output
    backend.set.assert_called_once_with(mail.SERVICE, ADDRESS, secret, mail.SERVICE)
    rejected = runner.invoke(cli, ['mail', 'set-password', '1', secret])
    assert rejected.exit_code and secret not in rejected.output
    backend.set.assert_called_once()


def test_a_code_mailed_by_the_platform_vendor_for_an_employers_own_careers_domain_is_read():
    """The employer's careers site runs on a vendor's platform under the employer's domain: the vendor mails the code and
    links to the employer's space on its own domain. The lone number of such a mail is the code. A vendor mail that does
    not point at this employer, or one with two numbers, gives nothing."""
    page = 'https://careers.exempleco.test/careers/apply'
    body = ('<h1>Verify your email</h1><p>Enter the following code below to complete the verification process:</p>'
            '<p>482913</p><p>The code will expire after 15 minutes.</p><a href="https://exempleco.eightfold.ai/privacy">Privacy Policy</a>')
    found = mail.extract(body, page, html=True, sender_domain='eightfold.ai')
    assert found == mail.Verification('code', '482913')
    other = body.replace('exempleco.eightfold.ai', 'autreco.eightfold.ai')
    assert mail.extract(other, page, html=True, sender_domain='eightfold.ai') is None
    assert mail.extract(body, page, html=True, sender_domain='unrelated.test') is None
    assert mail.extract(body.replace('15 minutes', '15 minutes. Reference 771203'), page, html=True, sender_domain='eightfold.ai') is None


class ReceiptIMAP(FakeIMAP):
    """Messages: uid -> (date, sender, subject, body)."""

    def uid(self, action, uid, *args):
        self.commands.append((action, uid, *args))
        if action == 'SEARCH':
            return 'OK', [b' '.join(self.messages)]
        date, sender, subject, body = self.messages[uid]
        if args[0].startswith('(INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]'):
            stamp = date.strftime('%d-%b-%Y %H:%M:%S %z').encode()
            return 'OK', [(b'INTERNALDATE "' + stamp + b'" RFC822.SIZE 2000', f'From: {sender}\r\nSubject: {subject}\r\n\r\n'.encode())]
        assert args[0] == '(BODY.PEEK[])'
        return 'OK', [(b'BODY[]', f'From: {sender}\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}'.encode())]


TITLE = 'Stage - Ingénieur conception de cartes électroniques embarquées F/H'


@pytest.mark.parametrize(('sender', 'subject', 'body', 'minutes', 'found'), [
    ('rh@employer.test', f'Votre candidature - {TITLE}', 'Nous avons bien reçu votre candidature.', 1, True),
    ('noreply@talent-soft.com', 'Your application', f'Thank you for applying to: {TITLE}', 1, True),          # the platform's domain
    ('rh@employer.test', 'Votre candidature - Stage assistant marketing digital', 'Bien reçue.', 1, False),   # another posting
    ('rh@employer.test', f'Votre candidature - {TITLE}', 'Bien reçue.', -20, False),                           # older than the send
    ('rh@employer.test', f'Finalisez votre candidature - {TITLE}', 'Votre candidature est incomplète.', 1, False),
    ('news@unrelated.test', f'Votre candidature - {TITLE}', 'Bien reçue.', 1, False),
])
def test_a_receipt_mail_since_the_send_that_names_the_posting_confirms_it(monkeypatch, sender, subject, body, minutes, found):
    """What a person checking the mailbox would conclude: a mail from the employer or its platform, received since the send
    click, that speaks of an application and names this posting. Read-only (EXAMINE, PEEK)."""
    clicked = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    clients = []

    def create(*args, **opts):
        clients.append(ReceiptIMAP(*args, **opts, messages={b'7': (clicked + timedelta(minutes=minutes), sender, subject, body)}))
        return clients[-1]

    monkeypatch.setattr(mail.imaplib, 'IMAP4_SSL', create)
    assert mail._receipt_lookup(ADDRESS, PASSWORD, POSTING, clicked, TITLE) is found
    assert ('EXAMINE', 'INBOX') in clients[0].commands and not any('STORE' in str(command) for command in clients[0].commands)


def test_a_title_is_named_by_most_of_its_telling_words():
    assert mail.names_posting('Candidature : ingénieur conception cartes électroniques embarquées', TITLE)
    assert not mail.names_posting('Candidature : stage ingénieur', TITLE)       # only the words every posting carries
    assert not mail.names_posting(f'Candidature - {TITLE}', 'Stage')            # a title that says nothing


def test_a_link_that_signs_in_is_read_only_when_a_sign_in_was_asked_for():
    """A passwordless site mails a link that signs the candidate in, on the employer's own space of the platform. It is
    taken only when the engine has just asked for that sign-in, and only from that space; a verification or reset link of
    the same mail is not taken for it."""
    page = 'https://exempleco.softy.pro/offers/100'
    link = 'https://exempleco.softy.pro/login-link/12345?expires=1&redirect=%2Foffers%2F100&signature=abcdef'
    body = f'<p>Bonjour,</p><a href="{link}">Se connecter</a><a href="https://exempleco.softy.pro/privacy">Confidentialité</a>'
    assert mail.extract(body, page, html=True, sender_domain='softymails.fr') is None
    assert mail.extract(body, page, html=True, sender_domain='softymails.fr', sign_in=True) == mail.Verification('link', link)
    other = body.replace('exempleco.softy.pro/login-link', 'autreco.softy.pro/login-link')
    assert mail.extract(other, page, html=True, sender_domain='softymails.fr', sign_in=True) is None
    reset = body.replace('/login-link/12345', '/password/reset/12345')
    assert mail.extract(reset, page, html=True, sender_domain='softymails.fr', sign_in=True) is None
    assert 'softymails.fr' in mail.search_domains(page)


def test_the_employers_name_under_another_ending_is_a_sender_that_is_searched():
    """The careers site is on "exempleco.com" and its mails come from "exempleco.fr": that sender is searched, and its
    verification link - which still has to point at the site itself - is read. A short name, a platform's own name and an
    unrelated sender get no such treatment."""
    site = 'https://recrutement.exempleco.com/my-account/log-in.aspx'
    assert {'exempleco.com', 'exempleco.fr', 'exempleco.eu'} <= mail.search_domains(site)
    assert 'abcd.fr' not in mail.search_domains('https://jobs.abcd.com/apply')
    assert 'recruitee.fr' not in mail.search_domains('https://exempleco.recruitee.com/o/poste')
    link = 'https://recrutement.exempleco.com/my-account/confirm-registration.aspx?LCID=1036&TOKEN=abcdef'
    assert mail.extract(f'<a href="{link}">Confirmer mon inscription</a>', site, html=True, sender_domain='exempleco.fr') == mail.Verification('link', link)
    elsewhere = link.replace('recrutement.exempleco.com', 'recrutement.exempleco.fr.unrelated.test')
    assert mail.extract(f'<a href="{elsewhere}">Confirmer mon inscription</a>', site, html=True, sender_domain='exempleco.fr') is None
    assert mail.extract('Votre code de vérification : 482913', site, sender_domain='exempleco.fr') == mail.Verification('code', '482913')
    assert mail.extract('Votre code de vérification : 482913', site, sender_domain='unrelated.test') is None


def test_the_mail_domain_of_a_platform_that_runs_employers_own_careers_sites_is_searched():
    """An employer's careers site on its own domain is run by a platform that mails from a domain of its own: that sender
    is searched, and its verification link - which points at the employer's site - is read."""
    site = 'https://carrieres.exempleco.test/fr/jobs/100-stage'
    assert 'teamtailor-mail.com' in mail.search_domains(site)
    link = 'https://carrieres.exempleco.test/fr/jobs/100-stage/applications/confirm_email/abcdef?candidate_uuid=1'
    body = f'<a href="{link}">Vérifiez votre adresse email</a><a href="https://www.teamtailor.com/?utm_source=mail">En savoir plus</a>'
    assert mail.extract(body, site, html=True, sender_domain='exempleco.teamtailor-mail.com') == mail.Verification('link', link)
