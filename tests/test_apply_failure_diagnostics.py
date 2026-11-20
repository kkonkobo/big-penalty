"""Failure diagnostics (masked screenshot + value-free page outline) and Workday's static-asset reads. Synthetic data only."""

from __future__ import annotations

import json

import pytest

from apply_fakes import ScriptedPlanner, cv_pdf, fast, page, sample_facts, server  # noqa: F401
from jobhunt.apply import workday
from jobhunt.apply.destination import RecipientGuard
from jobhunt.apply.engine import Uploads, run_application
from jobhunt.apply.page_outline import OUTLINE_FILE, RUN_FILE, SCREENSHOT_FILE, bare_url, save_run_record
from jobhunt.apply.types import StepPlan

SENTINEL = "Sentinel-Typed-Value-4821"
OUTLINE_PAGE = f"""<title>Synthetic posting</title>
<h1>Application form</h1><h2>Contact details</h2>
<form>
  <label for="given">Given name</label><input id="given" name="given_name" value="{SENTINEL}" required>
  <label>Email<input type="email" name="email" aria-required="true" value="{SENTINEL}@example.test"></label>
  <label>Country<select name="country"><option>One</option><option selected>Two</option></select></label>
  <label>Notes<textarea name="notes">{SENTINEL} notes</textarea></label>
  <input type="hidden" name="csrf" value="{SENTINEL}-hidden">
  <label><input type="checkbox" name="terms" checked> Accept the terms</label>
  <label id="src-label" for="source">How did you hear about us?</label>
  <button type="button" id="source" aria-haspopup="listbox" aria-labelledby="src-label source" data-automation-id="sourcePrompt"
          aria-label="How did you hear about us? {SENTINEL} Required" aria-required="true">{SENTINEL} chosen</button>
  <div role="combobox" aria-label="{SENTINEL} combobox value" aria-labelledby="source">{SENTINEL} typed</div>
  <label>About you<div contenteditable="true">{SENTINEL} rich text</div></label>
  <button type="button" data-automation-id="continueButton">Continue</button>
</form>
<a href="/elsewhere?ref={SENTINEL}">Back to search</a>
<script>localStorage.setItem('k', '{SENTINEL}-storage'); document.cookie = 'c={SENTINEL}-cookie';
history.replaceState(null, '', location.pathname + '?ref={SENTINEL}#{SENTINEL}');</script>"""


def _files_text(folder) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in folder.glob("*.json"))


async def test_stopped_run_saves_masked_screenshot_and_value_free_outline(page, server, fast, cv_pdf, tmp_path):
    server.html["/outline"] = OUTLINE_PAGE
    out = tmp_path / "run"
    res = await run_application(page, url=server.url("outline"), facts=sample_facts(),
                                planner=ScriptedPlanner([StepPlan(page_kind="other")]), uploads=Uploads(cv_pdf=cv_pdf),
                                mode="dry_run", out_dir=out, max_steps=3)
    assert res.status in {"needs_input", "blocked", "failed"} and SENTINEL in page.url  # (the live URL did carry a query)
    outline = json.loads((out / OUTLINE_FILE).read_text(encoding="utf-8"))
    assert (out / SCREENSHOT_FILE).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert outline["url"] == server.url("outline") and outline["status"] == res.status and outline["title"] == "Synthetic posting"
    assert [h["text"] for h in outline["headings"]][:2] == ["Application form", "Contact details"]
    fields = {field["label"]: field for field in outline["fields"]}
    assert fields["Given name"] == {"label": "Given name", "type": "text", "required": True, "automation_id": "", "name": "given_name"}
    assert fields["Email"]["required"] and fields["Email"]["type"] == "email"
    assert fields["Country"]["type"] == "select" and fields["Country"]["options"] == 2
    assert fields["How did you hear about us?"] == {"label": "How did you hear about us?", "type": "listbox", "required": True,
                                                   "automation_id": "sourcePrompt", "name": ""}
    assert fields["About you"]["type"] == "contenteditable"
    assert not any(control["automation_id"] == "sourcePrompt" for control in outline["controls"])
    assert fields["Accept the terms"]["type"] == "checkbox" and "checked" not in fields["Accept the terms"]
    assert not any(field["name"] == "csrf" for field in outline["fields"])
    assert {"kind": "button", "text": "Continue", "automation_id": "continueButton", "disabled": False} in outline["controls"]
    assert any(control["kind"] == "link" and control["text"] == "Back to search" for control in outline["controls"])
    assert SENTINEL not in _files_text(out)  # no field value, query string, href, cookie or storage content


async def test_no_screenshot_once_a_secret_was_typed(page, server, fast, tmp_path):
    from jobhunt.apply.page_outline import save_failure_diagnostics

    server.html["/outline"] = OUTLINE_PAGE
    await page.goto(server.url("outline"))
    written = await save_failure_diagnostics(page, tmp_path, status="failed", screenshot=False,
                                             redact=lambda text: text.replace("Contact", "[masked]"))
    assert written == [str(tmp_path / OUTLINE_FILE)] and not (tmp_path / SCREENSHOT_FILE).exists()
    assert "[masked] details" in (tmp_path / OUTLINE_FILE).read_text(encoding="utf-8")


def test_run_record_keeps_no_query_string(tmp_path):
    save_run_record(tmp_path, status="needs_human", final_url=f"https://jobs.example.test/a?x={SENTINEL}",
                    refused_request="POST https://jobs.example.test/api/write", step=3, control="navigation")
    record = json.loads((tmp_path / RUN_FILE).read_text(encoding="utf-8"))
    assert record == {"status": "needs_human", "final_url": "https://jobs.example.test/a",
                      "step": 3, "control": "navigation", "refused_request": "POST https://jobs.example.test/api/write"}


@pytest.mark.parametrize("url, expected", [
    ("https://user:pw@jobs.example.test:8443/apply/x?token=1#frag", "https://jobs.example.test:8443/apply/x"),
    ("http://127.0.0.1:5000/form?id=4", "http://127.0.0.1:5000/form"),
    ("about:blank", "about:"), ("", ""),
])
def test_bare_url(url, expected):
    assert bare_url(url) == expected


# ------------------------------------------------------------------ Workday static assets
@pytest.mark.parametrize("url, allowed", [
    ("https://wd3.myworkdaycdn.com/wday/asset/candidate-experience-jobs/cx-jobs.min.js", True),
    ("https://wd5.myworkday.com/wday/asset/uic-shared-vendors/shared-vendors.min.js", True),
    ("https://wd103.myworkdaycdn.com/wday/asset/fonts/font.woff2", True),
    ("https://wd3.myworkdaycdn.com/wday/asset/x.js?v=1", False),
    ("https://wd3.myworkdaycdn.com/wday/cxs/tenant/job/x", False),
    ("https://wd3.myworkday.com/wday/microscope/prelogin/logs", False),
    ("http://wd3.myworkdaycdn.com/wday/asset/x.js", False),
    ("https://wd3.myworkdaycdn.com:8443/wday/asset/x.js", False),
    ("https://evil.wd3.myworkdaycdn.com/wday/asset/x.js", False),
    ("https://wd3.myworkdaycdn.com.evil.test/wday/asset/x.js", False),
    ("https://tenant.wd3.myworkdayjobs.com/wday/asset/x.js", False),
    ("https://wd3.myworkdaycdn.com/wday/asset/../cxs/x", False),
])
def test_workday_static_asset(url, allowed):
    assert workday.static_asset(url) is allowed


@pytest.mark.parametrize("automation_id, label, expected", [
    ("adventureButton", "Apply", "draft_write"), ("adventureButton", "Postuler", "draft_write"),
    ("applyButton", "Apply", "draft_write"), ("applyManually", "Apply Manually", "draft_write"),
    ("adventureButton", "Apply with LinkedIn", None), ("autofillWithResume", "Autofill with Resume", None),
    ("useMyLastApplication", "Use My Last Application", None),
])
def test_workday_posting_entry_controls(automation_id, label, expected):
    posting = "https://demo.wd3.myworkdayjobs.com/External/job/Paris/Engineer_JR123"
    for url in (posting, posting + "/apply"):
        assert workday.WorkdayAdapter().browser_control(url, automation_id, label, "open_form", False) == expected


class _Response:
    def __init__(self, status, location=""):
        self.status, self.headers, self.disposed = status, {"location": location} if location else {}, False

    async def dispose(self):
        self.disposed = True


class _Request:
    def __init__(self, url, method="GET", navigation=False):
        self.url, self.method, self.navigation, self.resource_type, self.frame = url, method, navigation, "script", None
        self.headers, self.post_data_buffer = {}, None

    def is_navigation_request(self):
        return self.navigation


class _Route:
    def __init__(self, request, responses):
        self.request, self.responses, self.outcome = request, responses, None

    async def fetch(self, **_):
        return self.responses.pop(0)

    async def abort(self):
        self.outcome = "aborted"

    async def fulfill(self, response=None, **_):
        self.outcome = ("fulfilled", response.status)


class _Context:
    def __init__(self, hops):
        self.handler, self.hops, self.request, self.fetched, self.pages = None, hops, self, [], []

    async def add_init_script(self, script):
        pass

    async def route(self, pattern, handler):
        self.handler = handler

    async def fetch(self, url, **_):
        self.fetched.append(url)
        return self.hops.pop(0)


class _Page:
    def __init__(self, hops=()):
        self.context = _Context(list(hops))
        self.main_frame = None


class _NoReads:
    """An audience that refuses every request (the strictest guard)."""
    active = False
    entry_binding = ""

    def request_allowed(self, request, page, *, initial):
        return False

    def write_recipient(self, url):
        return False

    def pre_data_write(self, url):
        return False


CDN = "https://wd3.myworkdaycdn.com/wday/asset/candidate-experience-jobs/cx-jobs.min.js"
VENDORS = "https://wd3.myworkday.com/wday/asset/uic-shared-vendors/shared-vendors.min.js"


VERSIONED = "https://wd3.myworkday.com/wday/asset/uic-shared-vendors/2.0/shared-vendors.min.js"


@pytest.mark.parametrize("request_, responses, hops, outcome, refused, fetched", [
    (_Request(CDN), [_Response(200)], [], ("fulfilled", 200), False, []),
    (_Request(VENDORS), [_Response(307, "/wday/asset/uic-shared-vendors/2.0/shared-vendors.min.js")], [_Response(200)],
     ("fulfilled", 200), False, [VERSIONED]),
    (_Request(VENDORS), [_Response(307, "https://elsewhere.example.test/x.js")], [], "aborted", False, []),
    (_Request(VENDORS), [_Response(307, "/wday/asset/a.js")], [_Response(302, "https://elsewhere.example.test/b.js")],
     "aborted", False, ["https://wd3.myworkday.com/wday/asset/a.js"]),
    (_Request(CDN + "?q=1"), [], [], "aborted", False, []),
    (_Request("https://cdn.example.test/wday/asset/x.js"), [], [], "aborted", False, []),
    # A write or a sub-frame navigation is not a static read: under the lenient guard it is dropped
    # (and counted) rather than closing the run.
    (_Request(CDN, method="POST"), [], [], "aborted", False, []),
    (_Request(CDN, navigation=True), [], [], "aborted", False, []),
])
async def test_guard_admits_only_workday_static_reads(request_, responses, hops, outcome, refused, fetched):
    page = _Page(hops)
    guard = RecipientGuard(frozenset({"https://demo.wd3.myworkdayjobs.com"}), audience=_NoReads())
    await guard.install(page)
    route = _Route(request_, responses)
    await page.context.handler(route, request_)
    assert (route.outcome, guard.refused, page.context.fetched) == (outcome, refused, fetched)
    assert guard.refused_request == (f"{request_.method} {bare_url(request_.url)}" if refused else "")
    if request_.method == "POST" or request_.navigation:
        assert guard.aborted_hosts == {"wd3.myworkdaycdn.com": 1}
