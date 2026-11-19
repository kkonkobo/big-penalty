"""Read-only discovery from a verified posting to its application entry point."""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
from urllib.parse import urlsplit

from jobhunt.apply.autonomous import _domain
from jobhunt.apply.destination import clean_url, origin
from jobhunt.apply.identity import ats_target
from jobhunt.apply.supervised import _url_posting_ids


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
        && /^(?:apply(?: now| for this job)?|(?:je )?postul(?:er|e)(?: maintenant| à cette offre)?|candidater|(?:jetzt )?bewerben)$/iu.test(label(node)));
    const within = controls.filter(node => scope.contains(node));
    if (within.length) controls = within;
    const cookie = [...document.querySelectorAll('button, [role="button"], a[href]')].filter(node =>
      visible(node) && /^(?:tout refuser|refuser (?:tout|les cookies)|continuer sans accepter|reject all(?: cookies)?|decline all|necessary only|alle ablehnen|nur notwendige)$/iu.test(label(node))
      && !!node.closest('[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [role="dialog"]'));
    const ready = [...document.querySelectorAll('input[type="file"], input[type="email"], input[type="password"], input[autocomplete="given-name"]')]
      .some(node => visible(node) && !node.closest(excluded));
    return {ids, controls, cookie, ready, posting: jobs.length === 1 || scope.matches('[itemtype$="/JobPosting"]'),
            url: location.href, nonce: document.__bp_autonomous_document};
}"""

_ID = re.compile(r"(?:[a-z][a-z_-]{0,15})?\d[\w-]{1,79}\Z", re.I | re.ASCII)
_ENTRY = re.compile(r"(?:^|[/_-])(?:apply|application|candidate|candidature|postuler|bewerben|talentcommunity)(?:$|[/_-])", re.I)


def _identifiers(values):
    return {str(value).casefold() for value in values if _ID.fullmatch(str(value))}


class Probe:
    def __init__(self, source, requisition, ids):
        self.source = source
        self.source_domain = _domain(urlsplit(source).hostname)
        ats, identity = ats_target(source)
        self.source_ats = ats
        self.source_identity = identity
        url_ids = _url_posting_ids(source)
        self.ids = {requisition} if requisition else ({identity[1]} if identity else (url_ids or _identifiers(ids)))
        self.next_url = ""
        self.visited = set()
        self.hops = 0

    def same_site(self, target):
        return (origin(target) == origin(self.source) or bool(self.source_domain and
                _domain(urlsplit(target).hostname) == self.source_domain))

    def navigation(self, target):
        if not clean_url(target, navigation=True) or not origin(target):
            return False
        ats, identity = ats_target(target)
        if identity and self.ids and identity[1] not in self.ids:
            return False
        if identity and self.source_identity and ats == self.source_ats and identity != self.source_identity:
            return False
        target_ids = _url_posting_ids(target)
        if target_ids and not target_ids <= self.ids and not (identity and identity[1] in self.ids):
            return False
        return self.same_site(target) or bool(len(self.ids) == 1 and ats)

    def allows(self, request):
        if request.is_navigation_request():
            if self.hops >= 6 or not self.navigation(request.url):
                return False
            try:
                frame = request.frame
                if frame.parent_frame is not None:
                    return False
            except Exception:
                if request.resource_type != "document":
                    return False
            self.visited.add(request.url)
            self.hops += 1
            return True
        return self.same_site(request.url) or any(origin(url) == origin(request.url) for url in self.visited)

    def redirect(self, target):
        if self.hops >= 6 or self.next_url or not self.navigation(target):
            return False
        self.next_url = target
        return True

    def landing(self, target, ids, *, ready):
        if not self.navigation(target):
            return False
        _, identity = ats_target(target)
        declared = _identifiers(ids)
        if declared and self.ids and not declared <= self.ids:
            return False
        if not (ready or _ENTRY.search(urlsplit(target).path)):
            return False
        if self.same_site(target):
            return True
        return bool(len(self.ids) == 1 and (
            (identity and identity[1] in self.ids) or declared == self.ids or _url_posting_ids(target) == self.ids))


async def discover(page, guard, audience, *, requisition=None, require_posting=False):
    """Return the admitted page, or a fixed diagnostic. No writes are forwarded while probing."""
    baseline = set(page.context.pages)
    opened = []

    def on_page(tab):
        opened.append(tab)

    context = page.context
    context.on("page", on_page)
    probe = Probe(audience.live_url, requisition, [])
    guard.probe = probe
    accepted = False
    try:
        if guard.read_only:
            await page.context.set_offline(False)
        state = await page.evaluate_handle(_POSTING, isolated_context=True)
        data = await state.get_properties()
        if (await data["url"].json_value() != audience.live_url or not audience.document_nonce
                or await data["nonce"].json_value() != audience.document_nonce or audience.active):
            return None, "apply_target_unverified"
        if require_posting and (not await data["posting"].json_value() or await data["ready"].json_value()):
            return None, "job_page_no_apply_control"
        cookies = await data["cookie"].get_properties()
        if len(cookies) == 1:
            audience.recheck()
            await cookies["0"].as_element().click(timeout=2500, no_wait_after=True)
            state = await page.evaluate_handle(_POSTING, isolated_context=True)
            data = await state.get_properties()
            if (await data["url"].json_value() != audience.live_url
                    or await data["nonce"].json_value() != audience.document_nonce or probe.visited):
                return None, "apply_target_unverified"
        controls = await data["controls"].get_properties()
        if not controls:
            return None, "job_page_no_apply_control"
        if len(controls) != 1:
            return None, "apply_control_ambiguous"
        probe = Probe(audience.live_url, requisition, await data["ids"].json_value())
        guard.probe = probe
        was_ready = await data["ready"].json_value()
        button = controls["0"].as_element()
        same = await page.evaluate("""node => [node.ownerDocument === document && node.isConnected,
                       location.href, document.__bp_autonomous_document]""", button, isolated_context=True)
        if same != [True, audience.live_url, audience.document_nonce]:
            return None, "apply_target_unverified"
        audience.recheck()
        await button.click(timeout=2500, no_wait_after=True)
        deadline = asyncio.get_running_loop().time() + 5
        while not guard.refused and asyncio.get_running_loop().time() < deadline:
            new = set(context.pages) - baseline
            if len(opened) > 1 or len(new) > 1:
                break
            tab = next(iter(new), page)
            if probe.next_url:
                url, probe.next_url = probe.next_url, ""
                await tab.goto(url, wait_until="domcontentloaded", timeout=5000)
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
                    target = await landing["url"].json_value()
                    ids = await landing["ids"].json_value()
                    if ((tab != page or target != audience.live_url or (ready and not was_ready))
                            and probe.landing(target, ids, ready=ready)):
                        if tab != page:
                            await page.close()
                        if guard.refused or len(opened) > 1:
                            return None, "apply_target_unverified"
                        audience.recheck()
                        audience.entry_nonce = nonce
                        accepted = True
                        return tab, clean_url(target, navigation=True)
            await asyncio.sleep(0.1)
    except Exception:
        pass
    finally:
        context.remove_listener("page", on_page)
        if not accepted:
            for tab in set(page.context.pages) - baseline:
                with contextlib.suppress(Exception):
                    await tab.close()
        if guard.read_only:
            with contextlib.suppress(Exception):
                await page.context.set_offline(True)
    return None, "apply_target_unverified"
