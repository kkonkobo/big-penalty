import json
import os
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from test_apply_dispatch_safety import approved_fixture, fast, server  # noqa: F401
from test_ats_adapter_contract import PUBLICATION_PATH, SMART_URL, WORKDAY_POSTING
from jobhunt.apply import service, smartrecruiters, store, workday
from jobhunt.apply.adapters import Registry
from jobhunt.apply.facts import save_answers
from jobhunt.browser import BrowserPool


@pytest.fixture(params=[True, False], ids=['headless', 'headful'])
async def adapter_pool(request):
    if not request.param and os.environ.get('JOBHUNT_FORCE_HEADLESS', '').strip() == '1':
        pytest.skip('headful production-browser test: runs in CI, skipped on the operator\'s desktop')
    pool = BrowserPool(headless=request.param)
    try:
        yield pool
    finally:
        await pool.close()


def install_adapter(monkeypatch, server, workday_site):
    module = workday if workday_site else smartrecruiters
    resolver_name = '_posting' if workday_site else '_identity'
    original = getattr(module, resolver_name)
    original_url = WORKDAY_POSTING if workday_site else SMART_URL
    real_origin = f'https://{urlsplit(original_url).hostname}'

    def resolve_loopback(url, **kwargs):
        if url.startswith(server.base + '/'):
            result = original(real_origin + url[len(server.base):], **kwargs)
            return (*result[:2], server.base) if result and workday_site else result
        return original(url, **kwargs)

    monkeypatch.setattr(module, resolver_name, resolve_loopback)
    base = workday.WorkdayAdapter if workday_site else smartrecruiters.SmartRecruitersAdapter

    class LoopbackAdapter(base):
        def resolve(self, url, markers):
            if workday_site:
                return super().resolve(url, markers)
            requisition = super().resolve(real_origin + url[len(server.base):], markers) if url.startswith(server.base + '/') else None
            return replace(requisition, endpoints=tuple((boundary, endpoint.replace(real_origin, server.base))
                           for boundary, endpoint in requisition.endpoints)) if requisition else None

    registry = Registry()
    registry.register(LoopbackAdapter(), host='127.0.0.1', auto_enroll='first_party_confirmation')
    monkeypatch.setattr(service, 'registry', registry)
    monkeypatch.setattr(service.get_settings(), 'apply_autonomous', True)


def workday_flow(server, scenario):
    path = urlsplit(WORKDAY_POSTING).path
    endpoint = '/wday/cxs/demo.wd3.myworkdayjobs.com%2Fexternal/job/JR123/'
    for action in ('draft', 'account', 'submit', 'attachments'):
        server.post_json[endpoint + action] = {'status': 'ok'}
    steps = [
        ('', 'Posting', '<a data-automation-id="applyButton" href="' + path + '/apply">Apply</a>'),
        ('/apply', 'Apply', '<a data-automation-id="applyManually" href="' + path + '/apply/signIn">Apply Manually</a>'),
        ('/apply/signIn', 'Sign in', '<label>Email<input type="email" name="email" required></label>'
         '<label>Password<input type="password" name="password" required></label>'
         '<button data-automation-id="signInButton">Sign In</button>'),
        ('/apply/myInformation', 'My Information', '<label>First name<input data-automation-id="legalNameCountry-firstName" required></label>'),
        ('/apply/myExperience', 'My Experience', '<label>Resume<input type="file" data-automation-id="resumeUpload" required></label>'),
        ('/apply/applicationQuestions', 'Application Questions', '<p>Nothing to add</p>'),
        ('/apply/voluntaryDisclosures', 'Voluntary Disclosures', '<label>Race / ethnicity<select name="ethnicity" required><option value="">Select</option>'
         '<option value="black">Black or African American</option><option value="optout">Prefer not to say</option></select></label>'),
        ('/apply/selfIdentify', 'Self Identify', '<label>Disability<select required><option value="">Select</option>'
         '<option value="optout">Prefer not to say</option></select></label>'),
        ('/apply/review', 'Review', '<button data-automation-id="submitButton">Submit</button>'),
    ]
    next_button = '<button data-automation-id="bottom-navigation-next-button">Save and Continue</button>'
    for index, (suffix, heading, fields) in enumerate(steps):
        if index < 2:
            server.html[path + suffix] = '<h1>' + heading + '</h1>' + fields
            continue
        next_path = path + steps[index + 1][0] if index + 1 < len(steps) else path + '/apply/review/confirmation'
        if index == 2 and scenario == 'otp':
            next_path = path + '/apply/signIn'
        if index == 3 and scenario == 'changed':
            next_path = next_path.replace('JR123', 'JR999')
        content = ('<h1>Verification code</h1><input autocomplete="one-time-code">' if scenario == 'otp' and index == 2
                   else '<h1>Your application has been submitted</h1>' if index == 8 and scenario != 'missing'
                   else '<h1>Processing</h1>' if index == 8 else '')
        if index == 8 and scenario == 'candidate_home':
            next_path = path.split('/job/', 1)[0] + '/candidateHome'
            content = (f'<article data-automation-id="candidateHomeApplication"><a href="{path}">Engineer</a>'
                       '<span data-automation-id="applicationStatus">Submitted</span></article>')
        action = 'account' if index == 2 else 'submit' if index == 8 else 'draft'
        buttons = next_button if 3 <= index < 8 else ''
        server.html[path + suffix] = (
            f'<h1>{heading}</h1><form>{fields}{buttons}</form><script>document.forms[0].onsubmit = async event => {{'
            'event.preventDefault(); const data = new FormData(); for (const input of event.target.elements) '
            'if (input.type !== "file" && input.type !== "submit") data.append(input.name || input.type, input.value);'
            f'await fetch({json.dumps(endpoint + action)}, {{method:"POST", body:data}});'
            + (f'history.pushState(null,"",{json.dumps(next_path)});document.body.innerHTML={json.dumps(content)};' if content
               else f'location.href={json.dumps(next_path)};') + '};'
            + (f'document.querySelector("input[type=file]").onchange = event => {{const data = new FormData();'
               f'data.append("cv", event.target.files[0]); fetch({json.dumps(endpoint + "attachments")},'
               '{method:"POST", body:data});};' if index == 4 else '') + '</script>')
    return path


@pytest.mark.parametrize('scenario', ['success', 'candidate_home', 'otp', 'changed', 'missing'])
async def test_workday_browser_flow(scenario, adapter_pool, server, profile, tmp_path, fast, monkeypatch):
    install_adapter(monkeypatch, server, True)
    monkeypatch.setattr(service.accounts, 'create_password', lambda host, email: 'fixture-password')
    monkeypatch.setattr(service.accounts, 'account_known', lambda host, email: True)
    path = workday_flow(server, scenario)
    _, app = approved_fixture(profile, tmp_path, server.base + path)
    if scenario == 'success':
        save_answers(profile.id, {'race_ethnicity': 'Black'})
    result = await service.run(app.id, mode='submit', trigger='user', pool=adapter_pool)
    assert result.status == {'success': 'submitted', 'candidate_home': 'submitted', 'otp': 'needs_email',
                             'changed': 'needs_human', 'missing': 'failed'}[scenario], (
        result.result.dispatch_diagnostics, [(step.index, step.page_kind, step.note) for step in result.result.steps],
        server.requests, [post['path'] for post in server.posts])
    assert bool(result.submit_clicked_at) == (scenario in {'success', 'candidate_home', 'missing'})
    assert store.held_binding(result.id) is not None
    if result.status == 'submitted':
        assert result.result.dispatch_diagnostics.receipt_kind == (
            'candidate_home_listing' if scenario == 'candidate_home' else 'first_party_confirmation')
        uploads = [post for post in server.posts if post['path'].endswith('/attachments')]
        assert len(uploads) == 1
        assert service.cvs.safe_pdf(app).read_bytes() in uploads[0]['body']
        expected = b'black' if scenario == 'success' else b'optout'
        assert any(b'name="ethnicity"\r\n\r\n' + expected + b'\r\n' in post['body'] for post in server.posts)


async def test_smartrecruiters_browser_batch(adapter_pool, server, profile, tmp_path, fast, monkeypatch):
    install_adapter(monkeypatch, server, False)
    captcha_path = PUBLICATION_PATH[:-1] + '0'
    server.html[captcha_path] = '<div class="captcha-box" style="width:200px;height:50px">?</div>'
    server.html[PUBLICATION_PATH] = (f'<h1>Apply</h1><form method="post" action="{PUBLICATION_PATH}/application/submit">'
                                     '<label>First name<input name="first_name" required></label>'
                                     '<button type="submit">Submit application</button></form>')
    _, blocked = approved_fixture(profile, tmp_path, server.base + captcha_path, title='Synthetic CAPTCHA')
    _, app = approved_fixture(profile, tmp_path, server.base + PUBLICATION_PATH)
    runs = await service.send_selected([blocked.id, app.id], pool=adapter_pool)
    assert [run.status for run in runs] == ['needs_human', 'submitted'], [run.result for run in runs]
    assert len(server.posts) == 1
    _, duplicate = approved_fixture(profile, tmp_path, server.base + PUBLICATION_PATH + '/profile', title='Duplicate synthetic role')
    result = await service.run(duplicate.id, mode='submit', trigger='user', pool=adapter_pool)
    assert result.status == 'blocked' and len(server.posts) == 1


async def test_kill_switch_disables_enrolled_host_without_browser(server, profile, tmp_path, monkeypatch):
    install_adapter(monkeypatch, server, False)
    monkeypatch.setattr(service.get_settings(), 'apply_autonomous', False)
    _, app = approved_fixture(profile, tmp_path, server.base + PUBLICATION_PATH)
    result = await service.run(app.id, mode='submit', trigger='user')
    assert result.status == 'needs_human' and not server.requests and not server.posts
    assert service.queue(profile.id)[0].supervised


async def test_enrolled_host_pauses_after_two_uncertain_sends(adapter_pool, server, profile, tmp_path, fast, monkeypatch):
    install_adapter(monkeypatch, server, False)
    for index in range(3):
        path = PUBLICATION_PATH[:-1] + str(index)
        target = path + '/application/submit'
        server.html[path] = (f'<form method="post" action="{target}">'
                             '<label>First name<input name="first_name" required></label>'
                             '<button>Submit application</button></form>')
        server.post_pages[target] = 'smartrecruiters_guest.html'
        _, app = approved_fixture(profile, tmp_path, server.base + path,
                                  title=('Embedded Systems', 'Database Reliability', 'Optics Research')[index],
                                  company=('Fixture Aerospace', 'Synthetic Data Labs', 'Optics Example')[index])
        result = await service.run(app.id, mode='submit', trigger='user', pool=adapter_pool)
        assert result.status == ('failed' if index < 2 else 'needs_human'), (index, result.result)
        assert bool(result.submit_clicked_at) == (index < 2)
    assert store.host_paused('127.0.0.1') and len(server.posts) == 2
