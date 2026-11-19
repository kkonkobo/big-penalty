"""LinkedIn job discovery (France).

Primary path: the public *guest* endpoints (no login, plain HTTP):
  search : /jobs-guest/jobs/api/seeMoreJobPostings/search   (10 cards per page, `start` offset)
  detail : /jobs-guest/jobs/api/jobPosting/{id}             (description, criteria, apply type)

What the guest detail exposes about applying: *whether* the offer is off-site
("public_jobs_apply-link-offsite" -> apply_method="external") or LinkedIn's own flow
("public_jobs_apply-link-onsite" -> apply_method="easy_apply"), and, when LinkedIn serves it, the destination
of the off-site button: a `<code id="applyUrl"><!--"https://www.linkedin.com/jobs/view/externalApply/<id>?url=<target>"--></code>`
block and/or an anchor (`a.apply-button`, `data-tracking-control-name="public_jobs_apply-link-offsite..."`) whose
href carries `url=`. `extract_external_apply_url` tries every known variant and decodes the target. Many guest
pages do NOT carry it (it was absent in live checks of Nov 2026); then it is only reachable in a logged-in session,
which is what the browser fallback / `fetch_details` browser path does.

Fallback: when the guest API answers 429/999/authwall, and a BrowserPool is available, we drive the
user's logged-in LinkedIn session (`jobhunt login linkedin`, persistent profile). That path is
deliberately conservative: one page of results, human-like pacing from BrowserPool, no clicking
except to read the external apply link of one job at a time in `fetch_details`.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import random
import re
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._common import html_to_text, infer_contract, infer_remote, km_to_miles
from jobhunt.sources.base import (
    FR_HEADERS,
    LoginRequired,
    Source,
    SourceContext,
    SourceError,
)

BASE = "https://www.linkedin.com"
SEARCH_URL = f"{BASE}/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = f"{BASE}/jobs-guest/jobs/api/jobPosting/{{id}}"
PAGE_SIZE = 10
MAX_PAGES = 5

_CONTRACT_TO_JT = {
    ContractType.CDI: "F",
    ContractType.CDD: "C,T",
    ContractType.INTERIM: "T",
    ContractType.FREELANCE: "C",
    ContractType.STAGE: "I",
    ContractType.ALTERNANCE: "I",
}


class GuestBlocked(Exception):
    """Guest endpoint refused us (429 / 999 / authwall)."""


# ------------------------------------------------------------------ parsing (pure functions)


def _txt(el: Any) -> str | None:
    if el is None:
        return None
    t = re.sub(r"\s+", " ", el.get_text(" ")).strip()
    return t or None


def parse_search_cards(html: str) -> list[dict[str, Any]]:
    """Cards of a guest search page -> dicts(id, url, title, company, company_url, location, date, salary)."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for card in soup.select("div.base-search-card, div.job-search-card"):
        urn = str(card.get("data-entity-urn", ""))
        job_id = urn.rsplit(":", 1)[-1]
        if not job_id.isdigit():
            continue
        title = _txt(card.select_one(".base-search-card__title"))
        if not title:
            continue
        link = card.select_one("a.base-card__full-link")
        url = f"{BASE}/jobs/view/{job_id}"
        company_el = card.select_one(".base-search-card__subtitle")
        company_a = company_el.find("a", href=True) if company_el else None
        time_el = card.select_one("time")
        out.append(
            {
                "id": job_id,
                "url": url,
                "slug_url": (str(link["href"]).split("?")[0] if link and link.get("href") else None),
                "title": title,
                "company": _txt(company_el),
                "company_linkedin_url": str(company_a["href"]).split("?")[0] if company_a else None,
                "location": _txt(card.select_one(".job-search-card__location")),
                "date": str(time_el["datetime"]) if time_el and time_el.get("datetime") else None,
                "salary": _txt(card.select_one(".job-search-card__salary-info")),
            }
        )
    return out


_EMPLOYMENT_TYPES = {
    "temporaire": "temporary",
    "temporary": "temporary",
    "contrat": "contract",
    "contract": "contract",
}


def _board_types(emp: str | None, level: str | None) -> list[str]:
    """LinkedIn labels -> jobspy-style types understood by infer_contract ("Stage / Alternance" -> internship)."""
    if re.search(r"stagiaire|alternant|intern", level or "", re.I):
        return ["internship"]
    e = (emp or "").lower()
    if re.search(r"stage|alternance|internship", e):
        return ["internship"]
    return [_EMPLOYMENT_TYPES[e]] if e in _EMPLOYMENT_TYPES else []


_APPLY_CODE = re.compile(r"<code[^>]*\bid=[\"']applyUrl[\"'][^>]*>(.*?)</code>", re.I | re.S)
_APPLY_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_OFFSITE_ANCHOR = "a.apply-button, a[data-tracking-control-name*='apply-link-offsite'], a[data-tracking-control-name*='apply-link'], " \
                  "a[href*='externalApply'], a[href*='/safety/go']"


def _decode_apply_target(raw: str | None) -> str | None:
    """A raw string from the page (maybe quoted, HTML- or JSON-escaped, maybe a LinkedIn redirect) -> the employer URL."""
    if not raw:
        return None
    v = htmllib.unescape(raw.strip())
    if v.startswith('"'):
        try:
            v = json.loads(v if v.endswith('"') else v + '"')
        except ValueError:
            v = v.strip('"')
    v = v.strip().strip('"').replace("\\u0026", "&").replace("\\/", "/")
    if v.startswith("//"):
        v = "https:" + v
    if not v.startswith(("http://", "https://")):
        return None
    target = external_url_from_linkedin_redirect(v)
    for _ in range(2):  # a redirect wrapped in a redirect
        if target and urlparse(target).netloc.lower().endswith("linkedin.com"):
            target = external_url_from_linkedin_redirect(target)
    if not target or not target.startswith(("http://", "https://")) or urlparse(target).netloc.lower().endswith("linkedin.com"):
        return None
    return target


def extract_external_apply_url(html: str) -> str | None:
    """Employer URL behind the off-site "Postuler" button of a guest job page, or None (Easy Apply / not served)."""
    for m in _APPLY_CODE.finditer(html):
        body = m.group(1)
        c = _APPLY_COMMENT.search(body)
        if url := _decode_apply_target((c.group(1) if c else body).strip()):
            return url
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select(_OFFSITE_ANCHOR):
        if a.get("href") and (url := _decode_apply_target(str(a["href"]))):
            if "offsite" in str(a.get("data-tracking-control-name", "")) or "externalApply" in str(a["href"]) or \
                    "/safety/go" in str(a["href"]) or "apply-button" in (a.get("class") or []):
                return url
    return None


def parse_detail(html: str) -> dict[str, Any]:
    """Guest job page -> dict(description, criteria{label: value}, apply_kind, board_types, level, function)."""
    soup = BeautifulSoup(html, "html.parser")
    md = soup.select_one("div.show-more-less-html__markup")
    description = html_to_text(md.decode_contents()) if md else None
    criteria: dict[str, str] = {}
    for li in soup.select("li.description__job-criteria-item"):
        k = _txt(li.select_one(".description__job-criteria-subheader"))
        v = _txt(li.select_one(".description__job-criteria-text"))
        if k and v:
            criteria[k] = v
    apply_kind = None
    if "public_jobs_apply-link-offsite" in html:
        apply_kind = "external"
    elif "public_jobs_apply-link-onsite" in html:
        apply_kind = "easy_apply"
    external_url = extract_external_apply_url(html) if apply_kind != "easy_apply" else None
    if external_url:
        apply_kind = "external"
    emp = next((v for k, v in criteria.items() if re.search(r"type d.emploi|employment type", k, re.I)), None)
    level = next((v for k, v in criteria.items() if re.search(r"niveau hi|seniority", k, re.I)), None)
    if level and re.search(r"non pertinent|not applicable", level, re.I):
        level = None
    func = next((v for k, v in criteria.items() if re.search(r"fonction|job function", k, re.I)), None)
    title = _txt(soup.select_one("h2.top-card-layout__title"))
    company_a = soup.select_one("a.topcard__org-name-link")
    sal = _txt(soup.select_one(".compensation__salary"))
    return {
        "title": title,
        "company": _txt(company_a),
        "description": description,
        "criteria": criteria,
        "apply_kind": apply_kind,
        "external_url": external_url,
        "board_types": _board_types(emp, level),
        "level": level,
        "function": func,
        "salary": sal,
    }


def external_url_from_linkedin_redirect(href: str) -> str | None:
    """Extract the target of linkedin externalApply / safety/go style redirect links."""
    try:
        u = urlparse(href)
    except ValueError:
        return None
    if u.netloc and "linkedin.com" not in u.netloc:
        return href
    qs = parse_qs(u.query)
    for key in ("url", "u", "dest", "destination", "redirect"):
        if qs.get(key):
            value = qs[key][0]
            return value if value.startswith(("http://", "https://")) else unquote(value)
    return None


def build_search_params(query: SearchQuery, start: int = 0) -> dict[str, Any]:
    loc = query.location or query.postal_code or "France"
    if "france" not in loc.lower():
        loc = f"{loc}, France"
    params: dict[str, Any] = {"keywords": query.keywords, "location": loc, "start": start, "sortBy": "DD"}
    if query.posted_within_days:
        params["f_TPR"] = f"r{query.posted_within_days * 86400}"
    jts = [_CONTRACT_TO_JT.get(c) for c in query.contract_types]
    if jts and None not in jts:
        params["f_JT"] = ",".join(dict.fromkeys(",".join(jts).split(",")))
    if query.remote_only:
        params["f_WT"] = "2"
    if query.radius_km and query.location:
        params["distance"] = km_to_miles(query.radius_km)
    return params


def _posted(date_s: str | None) -> datetime | None:
    if not date_s:
        return None
    try:
        return datetime.fromisoformat(date_s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def card_to_posting(card: dict[str, Any]) -> JobPosting:
    loc = card.get("location")
    # "Toulouse, Occitanie, France" -> "Toulouse"; keep the whole text if short/odd.
    display = loc
    if loc:
        parts = [p.strip() for p in loc.split(",") if p.strip() and p.strip().lower() != "france"]
        display = ", ".join(parts[:1]) if len(parts) > 1 else (parts[0] if parts else loc)
    title = card["title"]
    return JobPosting(
        source="linkedin",
        source_id=card["id"],
        url=card["url"],
        title=title,
        company=card.get("company"),
        location=display,
        contract_type=infer_contract(title, None),
        remote=infer_remote(f"{title} {loc or ''}"),
        salary_text=card.get("salary"),
        posted_at=_posted(card.get("date")),
        apply_method="unknown",
        raw={k: v for k, v in card.items() if k != "id"},
    )


def apply_detail(p: JobPosting, d: dict[str, Any]) -> JobPosting:
    """Merge parse_detail() output into a posting (returns a new posting)."""
    desc = d.get("description")
    if desc:
        p.description = desc
    p.contract_type = infer_contract(p.title, desc, d.get("board_types"))
    p.remote = infer_remote(f"{p.title}\n{desc or ''}")
    if d.get("level"):
        p.experience_level = d["level"]
    if d.get("apply_kind"):
        p.apply_method = d["apply_kind"]
    if d.get("external_url") and not p.apply_url:
        p.apply_url = d["external_url"]
    if d.get("salary") and not p.salary_text:
        p.salary_text = d["salary"]
    p.raw = {**p.raw, "criteria": d.get("criteria", {})}
    return p


# ------------------------------------------------------------------ source


class LinkedInSource(Source):
    domains = ("linkedin.com",)
    name: ClassVar[str] = "linkedin"
    label: ClassVar[str] = "LinkedIn"
    kind: ClassVar[Literal["api", "http", "browser"]] = "http"
    is_aggregator: ClassVar[bool] = True
    login_url: ClassVar[str] = "https://www.linkedin.com/login"
    # Pull the detail page (description, apply type) for every card found by search(): +1 request/job.
    enrich_in_search: ClassVar[bool] = False

    # -------------------------------------------------- guest HTTP
    async def _guest_get(self, client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> str:
        r = await client.get(url, params=params)
        final = str(r.url)
        if r.status_code in (429, 999, 403, 401) or "authwall" in final or "/login" in urlparse(final).path:
            raise GuestBlocked(f"HTTP {r.status_code} {urlparse(final).path}")
        if r.status_code >= 400:
            raise SourceError(f"LinkedIn guest HTTP {r.status_code} for {urlparse(url).path}")
        return r.text

    async def _guest_search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        out: list[JobPosting] = []
        seen: set[str] = set()
        async with ctx.http(headers={"Accept": "text/html,*/*"}) as client:
            for page in range(MAX_PAGES):
                if page:
                    await asyncio.sleep(random.uniform(2.5, 5.0))
                try:
                    html = await self._guest_get(client, SEARCH_URL, build_search_params(query, page * PAGE_SIZE))
                except GuestBlocked:
                    if out:
                        ctx.log.warning("linkedin: guest search blocked after %d results", len(out))
                        break
                    raise
                cards = parse_search_cards(html)
                new = [c for c in cards if c["id"] not in seen]
                if not new:
                    break
                for c in new:
                    seen.add(c["id"])
                    out.append(card_to_posting(c))
                if len(out) >= query.max_results or len(cards) < PAGE_SIZE:
                    break
        return out[: query.max_results]

    async def _guest_detail(self, p: JobPosting, ctx: SourceContext, client: httpx.AsyncClient | None = None) -> JobPosting:
        async def go(c: httpx.AsyncClient) -> JobPosting:
            html = await self._guest_get(c, DETAIL_URL.format(id=p.source_id))
            return apply_detail(p, parse_detail(html))

        if client is not None:
            return await go(client)
        async with ctx.http(headers={"Accept": "text/html,*/*"}) as c:
            return await go(c)

    # -------------------------------------------------- public API
    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        try:
            postings = await self._guest_search(query, ctx)
        except GuestBlocked as e:
            ctx.log.info("linkedin: guest API blocked (%s)", e)
            if ctx.browser is None:
                raise SourceError(
                    f"LinkedIn guest API rate-limited ({e}); retry later or log in with `jobhunt login linkedin`"
                ) from e
            return await self._browser_search(query, ctx)
        except httpx.HTTPError as e:
            raise SourceError(f"LinkedIn guest API network error: {e}") from e

        if self.enrich_in_search and postings:
            await self._enrich(postings, ctx)
        return postings

    async def _enrich(self, postings: list[JobPosting], ctx: SourceContext) -> None:
        async with ctx.http(headers={"Accept": "text/html,*/*"}) as client:
            for i, p in enumerate(postings):
                if i:
                    await asyncio.sleep(random.uniform(1.2, 2.8))
                try:
                    await self._guest_detail(p, ctx, client)
                except GuestBlocked:
                    ctx.log.warning("linkedin: guest detail blocked; %d/%d postings enriched", i, len(postings))
                    break
                except (httpx.HTTPError, SourceError) as e:
                    ctx.log.debug("linkedin: detail failed for %s: %s", p.source_id, e)

    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        needs_external = posting.apply_method == "external" and not posting.apply_url
        if posting.description and posting.apply_method != "unknown" and not needs_external:
            return posting
        try:
            posting = await self._guest_detail(posting, ctx)
            if posting.apply_method != "external" or posting.apply_url:
                return posting
        except GuestBlocked:
            pass
        except (httpx.HTTPError, SourceError) as e:
            ctx.log.debug("linkedin: guest detail failed: %s", e)
            return posting
        if ctx.browser is None or not ctx.browser.has_session(ctx.profile_id, "linkedin"):
            return posting
        try:
            return await self._browser_detail(posting, ctx)
        except SourceError as e:
            ctx.log.info("linkedin: browser detail failed: %s", e)
            return posting

    # -------------------------------------------------- logged-in browser fallback
    async def _open(self, page, ctx: SourceContext, url: str) -> None:
        assert ctx.browser is not None
        await ctx.browser.goto(page, url)
        cur = urlparse(page.url).path
        if cur.startswith(("/login", "/uas/login", "/authwall", "/checkpoint")):
            raise LoginRequired("LinkedIn session missing: run `jobhunt login linkedin --profile <id>` once")

    async def _browser_search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        from jobhunt.browser import human_scroll

        assert ctx.browser is not None
        params = build_search_params(query, 0)
        params.pop("start", None)
        url = f"{BASE}/jobs/search/?{urlencode(params, quote_via=quote)}"
        async with ctx.browser.page(ctx.profile_id, "linkedin") as page:
            await self._open(page, ctx, url)
            try:
                await page.wait_for_selector("[data-occludable-job-id], a[href*='/jobs/view/']", timeout=20_000)
            except Exception as e:
                raise SourceError("LinkedIn logged-in search page showed no job list (layout change or no results)") from e
            await human_scroll(page, steps=4)  # lazy-loads the left-hand list
            html = await page.content()
        return parse_browser_cards(html)[: query.max_results]

    async def _browser_detail(self, p: JobPosting, ctx: SourceContext) -> JobPosting:
        assert ctx.browser is not None
        async with ctx.browser.page(ctx.profile_id, "linkedin") as page:
            await self._open(page, ctx, f"{BASE}/jobs/view/{p.source_id}/")
            try:
                await page.wait_for_selector("#job-details, .jobs-description__content, .jobs-box__html-content", timeout=15_000)
            except Exception:
                pass
            html = await page.content()
            d = parse_browser_detail(html)
            if d.get("description"):
                p.description = d["description"]
                p.contract_type = infer_contract(p.title, p.description)
                p.remote = infer_remote(f"{p.title}\n{p.description}")
            if d.get("apply_kind") == "easy_apply":
                p.apply_method = "easy_apply"
            elif d.get("external_url"):
                p.apply_url, p.apply_method = d["external_url"], "external"
            elif d.get("apply_kind") == "external":
                url = await _read_external_apply_url(page)
                p.apply_method = "external"
                if url:
                    p.apply_url = url
        return p


# ------------------------------------------------------------------ logged-in DOM helpers


def parse_browser_cards(html: str) -> list[JobPosting]:
    """Job list of a logged-in /jobs/search page. Selectors are best-effort (LinkedIn changes them)."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, JobPosting] = {}
    for li in soup.select("[data-occludable-job-id]"):
        jid = str(li.get("data-occludable-job-id"))
        if not jid.isdigit() or jid in out:
            continue
        a = li.select_one("a.job-card-list__title, a.job-card-container__link, a[href*='/jobs/view/']")
        title = _txt(a)
        if a is not None:
            strong = a.find("strong")
            title = _txt(strong) or title
        if not title:
            continue
        text = li.get_text(" ", strip=True)
        easy = bool(re.search(r"candidature simplifiée|easy apply", text, re.I))
        out[jid] = JobPosting(
            source="linkedin",
            source_id=jid,
            url=f"{BASE}/jobs/view/{jid}",
            title=title,
            company=_txt(li.select_one(".artdeco-entity-lockup__subtitle, .job-card-container__primary-description")),
            location=_txt(li.select_one(".artdeco-entity-lockup__caption, .job-card-container__metadata-wrapper")),
            contract_type=infer_contract(title, None),
            apply_method="easy_apply" if easy else "unknown",
        )
    return list(out.values())


def parse_browser_detail(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    desc_el = soup.select_one("#job-details, .jobs-description__content, .jobs-box__html-content")
    text = soup.get_text(" ", strip=True)[:60_000]
    kind = None
    external_url = None
    if soup.find(attrs={"aria-label": re.compile(r"candidature simplifiée|easy apply", re.I)}) or re.search(
        r"Candidature simplifiée|Easy Apply", text
    ):
        kind = "easy_apply"
    else:
        for a in soup.select("a[href]"):
            href = str(a["href"])
            if "externalApply" in href or "/safety/go" in href or "/redir/redirect" in href:
                ext = external_url_from_linkedin_redirect(href)
                if ext:
                    external_url = ext
                    break
        if external_url or soup.find(["button", "a"], string=re.compile(r"^\s*(Postuler|Apply)\s*$", re.I)):
            kind = "external"
    return {
        "description": html_to_text(desc_el.decode_contents()) if desc_el else None,
        "apply_kind": kind,
        "external_url": external_url,
    }


async def _read_external_apply_url(page) -> str | None:
    """Click the (off-site) Postuler button once and read where the new tab goes. Never types or submits."""
    try:
        btn = page.get_by_role("button", name=re.compile(r"^(Postuler|Apply)", re.I)).first
        if not await btn.count():
            btn = page.get_by_role("link", name=re.compile(r"^(Postuler|Apply)", re.I)).first
        async with page.expect_popup(timeout=10_000) as pop:
            await btn.click()
        popup = await pop.value
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        url = popup.url
        await popup.close()
        return external_url_from_linkedin_redirect(url) or (url if "linkedin.com" not in url else None)
    except Exception:
        return None
