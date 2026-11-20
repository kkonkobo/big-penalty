"""Read-only discovery from a verified posting to its application entry point."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit
from uuid import UUID

import httpx

from jobhunt.apply.autonomous import _domain
from jobhunt.apply.destination import clean_url, login_return, origin
from jobhunt.apply.identity import ats_target
from jobhunt.apply.supervised import _url_posting_ids
from jobhunt.browser import is_challenge
from jobhunt.origin import _identity, workday_requisition


_POSTING = r"""() => {
    const visible = node => !!node.getClientRects().length && getComputedStyle(node).visibility === 'visible';
    const scopes = [...document.querySelectorAll('[itemtype$="/JobPosting"], main, [role="main"]')];
    const scope = scopes.find(node => node.querySelector('h1')) || scopes[0] || document.body;
    const ids = [];
    for (const root of [scope, ...scope.querySelectorAll('h1, [itemtype$="/JobPosting"]')]) {
      for (const key of ['data-job-id', 'data-requisition-id', 'data-job-requisition-id'])
        if (root.hasAttribute(key)) ids.push(root.getAttribute(key));
    }
    const jobs = [];
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(node.textContent);
        for (const object of (Array.isArray(parsed) ? parsed : parsed?.['@graph'] || [parsed]))
          if ([object?.['@type']].flat().includes('JobPosting')) jobs.push(object);
      } catch (_) {}
    }
    if (jobs.length === 1) {
      const id = jobs[0].identifier;
      if (id != null) ids.push(String(id?.value ?? id));
    }
    const label = node => (node.getAttribute('aria-label') || node.innerText || node.value || '')
      .normalize('NFKC').trim().replace(/\s+/g, ' ');
    const excluded = 'nav, footer, aside, [role="navigation"], [id*="similar" i], [class*="similar" i], '
        + '[id*="recommend" i], [class*="recommend" i], [id*="related" i], [class*="related" i]';
    let controls = [...document.querySelectorAll('a[href], button, [role="button"], input[type="button"], input[type="submit"]')]
      .filter(node => visible(node) && !node.matches(':disabled,[aria-disabled="true"],[download]')
        && !(node.form || node.closest('form'))?.querySelector('input[type="password"], input[type="file"], input[type="email"], input[autocomplete="given-name"]')
        && !node.closest(excluded) && (scope.contains(node) || (!node.closest('main, [role="main"]') && !!scope.querySelector('h1')))
        && (/^(?:apply(?: now| for this job)?|(?:je )?postul(?:er|e)(?: maintenant| à cette offre)?|candidater|(?:jetzt )?bewerben)$/iu.test(label(node))
            || node.tagName === 'A' && (/\/talentcommunity\/apply\/\d+(?:\/|$)/i.test(node.pathname)
                || /[?&](?:career_job_req_id|jobReqId)=\d+/i.test(node.search)) && /postul|apply|candidater|bewerben/i.test(label(node))));
    const eligible = node => scope.contains(node);
    const within = controls.filter(eligible);
    const cookie = [...document.querySelectorAll('button, [role="button"], a[href]')].filter(node =>
      visible(node) && /^(?:tout refuser|refuser (?:tout|les cookies)|continuer sans accepter|reject all(?: cookies)?|decline all|necessary only|alle ablehnen|nur notwendige)$/iu.test(label(node))
      && !!node.closest('[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [role="dialog"]'));
    const ready = [...document.querySelectorAll('input[type="file"], input[type="email"], input[type="password"], input[autocomplete="given-name"]')]
      .some(node => visible(node) && !node.closest(excluded));
    const workday = !!document.querySelector('[data-automation-id="applyManually"], [data-automation-id="adventureButton"], [data-automation-id="signInContent"], [data-automation-id="createAccountLink"]');
    const tiny = !document.querySelector('h1') && document.body.innerText.trim().length < 500;
    return {ids, controls, within, cookie, ready, workday, tiny, jobs, posting: jobs.length === 1 || scope.matches('[itemtype$="/JobPosting"]'),
            url: location.href, nonce: document.__bp_autonomous_document};
}"""

_CONTROL = r"""node => {
    const form = node.form;
    if (!form || node.type !== 'submit' || (node.getAttribute('formmethod') || form.method).toLowerCase() !== 'post')
        return {href: node.tagName === 'A' ? node.href : '', post: null};
    const action = node.hasAttribute('formaction') ? node.formAction :
        Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'action').get.call(form);
    const fields = [...form.elements].filter(field => !field.matches(':disabled') && field.name);
    const system = field => /^(?:ctl\d+\$)+moteurRapideOffre\$ctl\d+\$(?:OfferCriteria[$_]|EngineCriteriaCollection\$)/i.test(field.name);
    const passive = field => system(field) && (
        field.type === 'text' && field.value === '' ||
        field.type === 'checkbox' && !field.checked ||
        field.type === 'select-one' && field.selectedIndex === 0 && /^[\w-]{0,24}$/.test(field.value));
    if ((node.getAttribute('formenctype') || form.enctype) !== 'application/x-www-form-urlencoded' ||
        fields.some(field => field !== node && !['hidden', 'submit', 'button'].includes(field.type) && !passive(field)) ||
        [...form.elements].some(field => ['text', 'email', 'tel', 'file', 'password'].includes(field.type) && field.value))
        return {href: '', post: null};
    return {href: '', post: [action, fields.filter(field => field === node || field.type === 'hidden' || passive(field) && field.type !== 'checkbox')
        .map(field => [field.name, field.value])]};
}"""

_SYSTEM_FIELD = re.compile(r"(?:__(?:viewstate\d*|viewstatefieldcount|viewstategenerator|eventtarget|eventargument|eventvalidation|requestverificationtoken)|(?:.*[$:])?(?:id|idOffre|offreId|offerId|jobId|jobReqId|reqId|culture|lang(?:uage)?|locale|token|csrf(?:token)?|requestVerificationToken|btnPostuler|btnApply|bt_Postuler(?:_(?:Haut|Bas))?))\Z", re.I)
_SYSTEM_FILTER = re.compile(r"(?:ctl\d+\$)+moteurRapideOffre\$ctl\d+\$(?:OfferCriteria[$_]|EngineCriteriaCollection\$)[\w$]+\Z", re.I | re.ASCII)
_POSTULER = re.compile(r"(?:.*\$)?(?:btnPostuler|btnApply|bt_Postuler(?:_(?:Haut|Bas))?)\Z", re.I)
_SAP_APPLY = re.compile(r"/talentcommunity/apply/\d+(?:/|$)", re.I)
_WORKDAY_WAIT = 35

_ID = re.compile(r"(?:[a-z][a-z_-]{0,15})?\d[\w-]{1,79}\Z", re.I | re.ASCII)
_ENTRY = re.compile(r"(?:^|[/_-])(?:apply|application|candidate|candidature|postuler|bewerben|talentcommunity)(?:$|[/_-])", re.I)


def _same_talentsoft_post(first, second):
    return bool(first and second and first[0] == second[0] and
                sorted(pair for pair in first[1] if not _POSTULER.fullmatch(pair[0])) ==
                sorted(pair for pair in second[1] if not _POSTULER.fullmatch(pair[0])))


def _identifiers(values):
    return {str(value).casefold() for value in values if _ID.fullmatch(str(value))}


class Probe:
    def __init__(self, source, requisition, ids, *, platform=None):
        self.source = source
        self.source_domain = _domain(urlsplit(source).hostname)
        ats, identity = ats_target(source)
        self.source_ats = ats or ("successfactors" if platform == "successfactors" else None)
        self.source_identity = identity
        url_ids = _url_posting_ids(source)
        self.ids = {requisition.casefold()} if requisition else ({identity[1]} if identity else (url_ids or _identifiers(ids)))
        # Numbers in a career-site URL are the site's own references, not an ATS requisition: not evidence on an ATS.
        self.site_ids = not requisition and not identity and bool(url_ids)
        self.next_url = ""
        self.visited = set()
        self.hops = 0
        self.post = None
        self.alias = None
        self.control_origin = ""
        self.clicked = False
        self.chain = ""
        self.tab = None
        # Autonomous submit (not dry inspection): a recognized ATS reached from the verified posting is accepted unless a
        # requisition contradicts it, and a landing whose URL moved within an admitted origin is still continuous.
        self.lenient = False

    def expected(self, target, ats):
        """Identifiers a target must match: none for an ATS on another site when only career-site URL numbers are known."""
        if self.lenient and self.site_ids and ats and not self.same_site(target):
            return set()
        return {workday_requisition(item) for item in self.ids} if ats == "workday" else set(self.ids)

    def landing_ids(self, target, ids):
        """The posting identifiers the bound landing must keep: the ATS requisition itself when the posting only had
        career-site URL numbers to offer (they are not that ATS's requisition)."""
        ats, identity = ats_target(target)
        if self.ids and not self.expected(target, ats):
            return {identity[1]} if identity else _url_posting_ids(target) | _identifiers(ids)
        return set(self.ids)

    def continuity_site(self, target):
        return self.clicked and (origin(target) == self.control_origin or bool(ats_target(target)[0]))

    def arm_post(self, post):
        if not post or self.source_ats != "talentsoft" or not self.source_identity:
            return False
        url, fields = post
        if (origin(url) != origin(self.source) or not self.navigation(url) or not fields
                or not (urlsplit(url).path == urlsplit(self.source).path or self.ids & _url_posting_ids(url))
                or any(not isinstance(value, str) or not (_SYSTEM_FIELD.fullmatch(key) or
                       _SYSTEM_FILTER.fullmatch(key) and re.fullmatch(r"[\w-]{0,24}", value, re.ASCII)) for key, value in fields)):
            return False
        for key, value in fields:
            if re.fullmatch(r"(?:id|idOffre|offreId|offerId|jobId|jobReqId|reqId)", re.split(r"[$:]", key)[-1], re.I):
                if value.casefold() not in self.ids:
                    return False
        self.post = url, sorted(map(tuple, fields))
        return True

    def allow_post(self, request):
        if (not self.post or request.method != "POST" or request.url != self.post[0]
                or not request.is_navigation_request() or request.resource_type != "document"
                or request.frame.parent_frame is not None or request.frame.url != self.source
                or request.headers.get("content-type", "").partition(";")[0] != "application/x-www-form-urlencoded"):
            return False
        body = request.post_data or ""
        if len(body) > 2_000_000 or sorted(parse_qsl(body, keep_blank_values=True)) != self.post[1]:
            return False
        self.post = None
        return self.admit_navigation(request)

    def same_site(self, target):
        return (origin(target) == origin(self.source) or bool(self.source_domain and
                _domain(urlsplit(target).hostname) == self.source_domain))

    def navigation(self, target):
        if not clean_url(target, navigation=True) or not origin(target):
            return False
        ats, identity = ats_target(target)
        if self.alias and identity == self.alias and ats == "smartrecruiters":
            return True
        expected = self.expected(target, ats)
        if identity and expected and identity[1] not in expected:
            return False
        if identity and self.source_identity and ats == self.source_ats and identity != self.source_identity:
            return False
        target_ids = _url_posting_ids(target) | _url_posting_ids(login_return(target))
        if ats == "workday":
            target_ids = {workday_requisition(item) for item in target_ids}
        if target_ids and not target_ids <= expected and (expected or not self.lenient or not ats):
            return False
        # A recognized ATS host is the employer's application system: reachable when nothing contradicts the posting.
        return self.same_site(target) or self.continuity_site(target) or bool(ats and (len(self.ids) == 1 or self.lenient))

    def admit_navigation(self, request):
        if self.hops >= 6 or not self.navigation(request.url) or (self.hops and request.url != self.chain):
            return False
        try:
            frame = request.frame
            if frame.parent_frame is not None or (self.tab is not None and frame.page != self.tab):
                return False
            self.tab = frame.page
        except Exception:
            if self.hops or request.resource_type != "document":
                return False
        self.chain = ""
        self.visited.add(request.url)
        self.hops += 1
        return True

    def allows(self, request):
        if request.is_navigation_request():
            return self.admit_navigation(request)
        return self.same_site(request.url) or any(origin(url) == origin(request.url) for url in self.visited)

    def redirect(self, target):
        if self.hops >= 6 or self.next_url or not self.navigation(target):
            return False
        self.next_url = target
        self.chain = target
        return True

    def landing(self, target, ids, *, ready):
        if not self.navigation(target):
            return False
        ats, identity = ats_target(target)
        if self.alias and ats == "smartrecruiters" and identity == self.alias:
            return "requisition" if ready or _ENTRY.search(urlsplit(target).path) else ""
        declared = _identifiers(ids)
        expected = self.expected(target, ats)
        if ats == "workday":
            declared = {workday_requisition(item) for item in declared}
        if declared and not declared <= expected and (expected or not self.lenient):
            return False
        if (ready or _ENTRY.search(urlsplit(target).path)) and len(expected) == 1 and (
                (identity and identity[1] in expected) or declared == expected or _url_posting_ids(target) == expected):
            return "requisition"
        if self.clicked and (self.same_site(target) or self.continuity_site(target)) and (
                target in self.visited or (self.lenient and any(origin(url) == origin(target) for url in self.visited))):
            return "continuity"
        return ""


def _workday_entry(url):
    ats, identity = ats_target(url)
    parts = urlsplit(url)
    if ats == "workday" and identity and not parts.query and "/job/" in parts.path:
        if not re.search(r"/apply(?:/|$)", parts.path, re.I):
            return urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/apply/applyManually", fragment=""))
    return ""


def _smart_entry(probe, record):
    if not isinstance(record, dict) or not probe.source_identity:
        return ""
    company, requisition = probe.source_identity
    identifier = record.get("identifier", record.get("id"))
    if isinstance(identifier, dict):
        identifier = identifier.get("value")
    if str(identifier).casefold() != requisition:
        return ""
    url = record.get("applyUrl")
    if isinstance(url, str) and clean_url(url, navigation=True):
        ats, identity = ats_target(url)
        value = identity[1] if ats == "smartrecruiters" and identity and identity[0] == company else None
    else:
        value = record.get("publicationId", record.get("publication"))
        if isinstance(value, dict):
            value = value.get("id")
    try:
        publication = str(UUID(str(value)))
    except ValueError:
        return ""
    probe.alias = company, publication
    return f"https://jobs.smartrecruiters.com/oneclick-ui/company/{quote(company, safe='')}/publication/{publication}"


async def _smart_public_entry(probe):
    if not probe.source_identity or urlsplit(probe.source).hostname != "jobs.smartrecruiters.com":
        return ""
    company, requisition = probe.source_identity
    if not re.fullmatch(r"[a-z0-9_-]{1,100}", company) or not requisition.isascii() or not requisition.isdecimal():
        return ""
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{requisition}"
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                if response.status_code in {403, 429}:
                    raise PermissionError
                if response.status_code != 200:
                    return ""
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        return ""
        return _smart_entry(probe, json.loads(body))
    except (httpx.HTTPError, ValueError):
        return ""


async def _phenom_entry(page, probe):
    if not probe.ids or len(probe.ids) != 1 or probe.source_ats not in {None, "phenom"}:
        return ""
    record = await page.evaluate("""() => {
        const job = window.phApp?.ddo?.jobDetail?.data?.job;
        return job && [job.applyUrl, job.jobId, job.jobSeqNo, job.jobRequisitionId, job.jobReqId, job.requisitionId, job.reqId];
    }""")
    if not isinstance(record, list) or not isinstance(record[0], str):
        return ""
    target = clean_url(record[0], navigation=True)
    if ats_target(target)[0] != "workday" or not probe.navigation(target):
        return ""
    if not ({workday_requisition(item) for item in _identifiers(record[1:])} &
            {workday_requisition(item) for item in probe.ids}):
        return ""
    return _workday_entry(target) or target


async def discover(page, guard, audience, *, requisition=None, require_posting=False, platform=None):
    """Return the admitted page, or a fixed diagnostic. Only a bound Talentsoft entry POST may be forwarded."""
    baseline = set(page.context.pages)
    opened = []

    def on_page(tab):
        opened.append(tab)

    context = page.context
    context.on("page", on_page)
    lenient = bool(getattr(guard, "lenient", False))
    probe = Probe(audience.live_url, requisition, [], platform=platform)
    probe.lenient = lenient
    guard.probe = probe
    accepted = False
    workday = probe.source_ats == "workday"
    try:
        state = await page.evaluate_handle(_POSTING, isolated_context=True)
        data = await state.get_properties()
        if (await data["url"].json_value() != audience.live_url or not audience.document_nonce
                or await data["nonce"].json_value() != audience.document_nonce or audience.active):
            return None, "apply_target_unverified"
        if await is_challenge(page):
            return None, "challenge_or_rate_limited"
        direct = _workday_entry(audience.live_url)
        cookies = await data["cookie"].get_properties()
        if len(cookies) == 1:
            audience.recheck()
            await cookies["0"].as_element().click(timeout=2500, no_wait_after=True)
            state = await page.evaluate_handle(_POSTING, isolated_context=True)
            data = await state.get_properties()
            if (await data["url"].json_value() != audience.live_url
                    or await data["nonce"].json_value() != audience.document_nonce or probe.visited):
                return None, "apply_target_unverified"
        probe = Probe(audience.live_url, requisition, await data["ids"].json_value(), platform=platform)
        probe.lenient = lenient
        guard.probe = probe
        was_ready = await data["ready"].json_value()
        controls = await data["controls"].get_properties()
        within = await data["within"].get_properties()
        if not direct and (probe.source_ats == "phenom" or (not controls and not probe.source_ats)):
            direct = await _phenom_entry(page, probe)
        if require_posting and not direct and probe.source_ats not in {"smartrecruiters"} and (
                not await data["posting"].json_value() or await data["ready"].json_value()):
            if guard.inspection_only and await data["tiny"].json_value():
                return None, "public_page_shell"
            return None, "job_page_no_apply_control"
        if probe.source_ats not in {"successfactors", "talentsoft"} and within:
            controls = within
        if not direct and probe.source_ats == "smartrecruiters" and not controls:
            jobs = await data["jobs"].json_value()
            if len(jobs) == 1:
                direct = _smart_entry(probe, jobs[0])
            if not direct:
                direct = await _smart_public_entry(probe)
        if not direct and not controls:
            return None, "public_page_shell" if guard.inspection_only and await data["tiny"].json_value() else "job_page_no_apply_control"
        if direct:
            if not probe.navigation(direct):
                return None, "apply_target_unverified"
            same = await page.evaluate("() => [location.href, document.__bp_autonomous_document]", isolated_context=True)
            if same != [audience.live_url, audience.document_nonce]:
                return None, "apply_target_unverified"
            audience.recheck()
            await page.goto(direct, wait_until="domcontentloaded", timeout=35000)
        else:
            elements = [item.as_element() for item in controls.values()]
            candidates = [(element, await page.evaluate(_CONTROL, element, isolated_context=True)) for element in elements]
            if not probe.source_ats and all(details["post"] and any(_POSTULER.fullmatch(key) for key, _ in details["post"][1])
                                            for _, details in candidates):
                tenant, req, _ = _identity("talentsoft", probe.source)
                if tenant and req and req.casefold() in probe.ids:
                    probe.source_ats, probe.source_identity = "talentsoft", (tenant, req.casefold())
            if probe.source_ats == "successfactors":
                linked = [(element, details) for element, details in candidates if details["href"]]
                if any(_url_posting_ids(details["href"]) - probe.ids for _, details in linked):
                    return None, "apply_control_ambiguous"
                bound = [(element, details) for element, details in linked if probe.navigation(details["href"])
                         and (_url_posting_ids(details["href"]) & probe.ids or _SAP_APPLY.search(urlsplit(details["href"]).path))]
                if bound:
                    candidates = bound
                elif within:
                    scoped = [item.as_element() for item in within.values()]
                    candidates = [(element, await page.evaluate(_CONTROL, element, isolated_context=True)) for element in scoped]
            button, details = candidates[0]
            if len(candidates) > 1 and probe.source_ats != "successfactors" and (
                    probe.source_ats != "talentsoft" or not details["post"] or
                    any(not _same_talentsoft_post(details["post"], other["post"]) for _, other in candidates)):
                return None, "apply_control_ambiguous"
            same = await page.evaluate("""node => [node.ownerDocument === document && node.isConnected,
                           location.href, document.__bp_autonomous_document]""", button, isolated_context=True)
            if same != [True, audience.live_url, audience.document_nonce]:
                return None, "apply_target_unverified"
            if details["post"] and probe.source_ats == "talentsoft" and not probe.arm_post(details["post"]):
                return None, "apply_target_unverified"
            audience.recheck()
            probe.clicked = True
            probe.control_origin = origin(details["href"] or (details["post"][0] if details["post"] else ""))
            await button.click(timeout=2500, no_wait_after=True)
        workday = probe.source_ats == "workday" or bool(direct and ats_target(direct)[0] == "workday")
        deadline = asyncio.get_running_loop().time() + (_WORKDAY_WAIT if workday else 5)
        while not guard.refused and asyncio.get_running_loop().time() < deadline:
            new = set(context.pages) - baseline
            if len(opened) > 1 or len(new) > 1:
                break
            tab = next(iter(new), page)
            if probe.next_url:
                url, probe.next_url = probe.next_url, ""
                await tab.goto(url, wait_until="domcontentloaded", timeout=35000 if workday else 5000)
                continue
            if not tab.is_closed() and tab.url not in {"", "about:blank"}:
                with contextlib.suppress(Exception):
                    nonce = secrets.token_urlsafe(24)
                    landing_state = await tab.evaluate_handle(
                        f"nonce => {{ document.__bp_apply_landing = nonce; return ({_POSTING})(); }}",
                        nonce, isolated_context=True,
                    )
                    landing = await landing_state.get_properties()
                    ready = await landing["ready"].json_value()
                    marker = await landing["workday"].json_value()
                    target = await landing["url"].json_value()
                    ids = await landing["ids"].json_value()
                    if workday and await is_challenge(tab):
                        return None, "challenge_or_rate_limited"
                    if ((probe.tab is None or tab == probe.tab)
                            and (tab != page or target != audience.live_url or (ready and not was_ready))
                            and (not workday or marker or ready)
                            and (binding := probe.landing(target, ids, ready=ready))):
                        if tab != page:
                            await page.close()
                        if guard.refused or len(opened) > 1:
                            return None, "apply_target_unverified"
                        audience.recheck()
                        audience.entry_nonce = nonce
                        audience.entry_binding = binding
                        audience.entry_ids = frozenset(probe.landing_ids(target, ids))
                        accepted = True
                        return tab, clean_url(target, navigation=True)
            await asyncio.sleep(0.1)
    except PermissionError:
        return None, "challenge_or_rate_limited"
    except Exception:
        pass
    finally:
        context.remove_listener("page", on_page)
        if not accepted:
            for tab in set(page.context.pages) - baseline:
                with contextlib.suppress(Exception):
                    await tab.close()
        if guard.inspection_only:
            with contextlib.suppress(Exception):
                await page.context.set_offline(True)
    return None, guard.refusal_code if guard.refused else "public_page_shell" if workday else "apply_target_unverified"
