"""France Travail (ex-Pôle emploi): the biggest French board, incl. partner-board offers.

Two modes, chosen automatically:

* API mode (FRANCE_TRAVAIL_CLIENT_ID / FRANCE_TRAVAIL_CLIENT_SECRET set): official "Offres d'emploi v2"
  API. OAuth2 client_credentials on https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire
  (scope `api_offresdemploiv2 o2dsoffre`), then GET https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search
  (range=0-149 pages, 1150 results max, 10 req/s). Searches by ROME code when the query has some.
  NOTE: new API sign-ups were reported suspended, and this mode could not be exercised live (no credentials):
  it is written from the public documentation and covered by offline tests on sample payloads only.
* Website mode (default): the public candidate site https://candidat.francetravail.fr/offres/recherche
  (server-rendered list, 20 per page, `offresPartenaires=true` to include partner boards) plus two key-less
  JSON/XHR endpoints the site itself uses for the detail page:
    - GET /api-descriptifoffre/offre/{id}                                  -> full description, ROME, salary...
    - GET /offres/recherche/detail.detailstandalone.postuler.contact:chargercontact?t:ac={id}
                                                                           -> "Postuler" target (recruiter site,
                                                                              or the partner board's original URL)
  Plain HTTP works (no bot wall seen); falls back to the browser pool via fetch.get_html if blocked.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from jobhunt.fetch import get_html
from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.sources._fr_public import (
    clean, contract_from_title, department_of, detect_remote, find_region, geocode, html_to_text,
    parse_fr_date, parse_salary,
)
from jobhunt.sources._rome import lookup_rome
from jobhunt.sources.base import Source, SourceContext, SourceError

log = logging.getLogger("jobhunt.sources.france_travail")

SITE = "https://candidat.francetravail.fr"
SEARCH_URL = f"{SITE}/offres/recherche"
DETAIL_URL = f"{SITE}/offres/recherche/detail/{{id}}"
DETAIL_API = f"{SITE}/api-descriptifoffre/offre/{{id}}"
CONTACT_URL = f"{SITE}/offres/recherche/detail.detailstandalone.postuler.contact:chargercontact"

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire"
API_SEARCH = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
API_SCOPE = "api_offresdemploiv2 o2dsoffre"
API_PAGE = 150
API_MAX = 1150
SITE_PAGE = 20

# FT "nature de contrat" facet codes (site `natureOffre=` / API `natureContrat=`)
NATURE_APPRENTISSAGE = "E2"
NATURE_PROFESSIONNALISATION = "FS"
_RADII = (0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
_EMISSION = (1, 3, 7, 14, 31)  # "publiée depuis" choices on the site/API


def _pause(lo: float = 0.4, hi: float = 0.9):
    return asyncio.sleep(random.uniform(lo, hi))


def _emission(days: int | None) -> int | None:
    if not days:
        return None
    for e in _EMISSION:
        if days <= e:
            return e
    return None  # > 31 days: no filter


def _site_contract(label: str | None) -> ContractType:
    t = (label or "").lower()
    if "apprentissage" in t or "professionnalisation" in t or "alternance" in t:
        return ContractType.ALTERNANCE
    if "intérim" in t or "interim" in t or t.startswith("mis"):
        return ContractType.INTERIM  # also "CDI Intérimaire" (DIN)
    if "cdi" in t:
        return ContractType.CDI
    if "cdd" in t or "saisonnier" in t:
        return ContractType.CDD
    if "stage" in t:
        return ContractType.STAGE
    if "portage" in t or "libéral" in t or "liberal" in t or "franchise" in t:
        return ContractType.FREELANCE
    return ContractType.UNKNOWN


def _api_contract(type_code: str | None, nature: str | None, alternance: bool | None, title: str | None) -> ContractType:
    if alternance or (nature or "").upper() in (NATURE_APPRENTISSAGE, NATURE_PROFESSIONNALISATION, "E1"):
        return ContractType.ALTERNANCE
    base = {
        "CDI": ContractType.CDI, "CDD": ContractType.CDD, "MIS": ContractType.INTERIM,
        "DIN": ContractType.INTERIM, "SAI": ContractType.CDD, "TTI": ContractType.INTERIM,
        "CCE": ContractType.FREELANCE, "LIB": ContractType.FREELANCE, "FRA": ContractType.FREELANCE,
    }.get((type_code or "").upper(), ContractType.UNKNOWN)
    return contract_from_title(title, base)


def _location(synth: str | None) -> tuple[str | None, str | None]:
    """'31 - Toulouse' -> ('Toulouse (31)', '31'); '75 - Paris 1er Arrondissement'."""
    if not synth:
        return None, None
    m = re.match(r"\s*(\d{2,3}|2[AB])\s*-\s*(.+)", synth)
    if m:
        dep, city = m.group(1), m.group(2).strip()
        return f"{city.title() if city.isupper() else city} ({dep})", dep
    return synth.strip(), None


# =========================================================================== website parsing

def parse_search_html(html: str) -> list[dict[str, Any]]:
    """List page -> raw dicts. Pure function (offline-testable)."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.result[data-id-offre]"):
        oid = li["data-id-offre"]
        title_el = li.select_one(".media-heading-title") or li.select_one("h2")
        if not title_el:
            continue
        sub = li.select_one("p.subtext")
        company = loc = None
        if sub:
            span = sub.find("span")
            loc = clean(span.get_text()) if span else None
            if span:
                span.extract()
            comp = clean(re.sub(r"[\s\-–]+$", "", sub.get_text()))
            company = comp or None
            if not span:  # no company: whole text is the location
                loc, company = (company, None) if company and re.match(r"\d{2,3}\s*-", company) else (None, company)
        img = li.select_one("img[alt^='Origine']")
        origin = None
        if img:
            origin = re.sub(r"^Origine de l.offre\s*:\s*", "", img.get("alt", "")).strip() or None
        contrat_el = li.select_one(".media-right p.contrat") or li.select_one("p.contrat")
        contrat_lines = [clean(x) for x in (contrat_el.get_text("\n").split("\n") if contrat_el else []) if clean(x)]
        date_el = li.select_one("p.date")
        desc_el = li.select_one("p.description")
        out.append({
            "id": oid,
            "title": clean(title_el.get_text()),
            "company": company,
            "location": loc,
            "snippet": clean(desc_el.get_text()) if desc_el else None,
            "contract": contrat_lines[0] if contrat_lines else None,
            "hours": contrat_lines[1] if len(contrat_lines) > 1 else None,
            "published": clean(date_el.get_text()) if date_el else None,
            "origin": origin,
        })
    return out


def listing_to_posting(r: dict[str, Any], *, contract_hint: ContractType | None = None) -> JobPosting | None:
    if not r.get("id") or not r.get("title"):
        return None
    loc, dep = _location(r.get("location"))
    ctype = _site_contract(r.get("contract"))
    if contract_hint == ContractType.ALTERNANCE:
        ctype = ContractType.ALTERNANCE
    ctype = contract_from_title(r["title"], ctype)
    origin = r.get("origin")
    native = (origin or "").lower() in ("france travail", "pole emploi", "pôle emploi", "")
    return JobPosting(
        source="france_travail",
        source_id=r["id"],
        url=DETAIL_URL.format(id=r["id"]),
        title=r["title"],
        company=r.get("company"),
        location=loc,
        department=dep,
        contract_type=ctype,
        description=r.get("snippet"),  # truncated teaser; fetch_details gets the full text
        posted_at=parse_fr_date(r.get("published")),
        apply_method="unknown",
        raw={"listing": r, "origin_board": None if native else origin, "native": native},
    )


def parse_detail_json(d: dict[str, Any], p: JobPosting) -> JobPosting:
    """Merge /api-descriptifoffre/offre/{id} into the posting."""
    upd: dict[str, Any] = {}
    parts = [html_to_text(d.get("descriptif"))]
    comps = [c.get("libelleCompetence") for c in (d.get("listeCompetence") or []) if c.get("libelleCompetence")]
    if comps:
        parts.append("Compétences : " + " ; ".join(comps))
    forms = [f"{f.get('niveauLibelle') or ''} {f.get('domaineLibelle') or ''}".strip() for f in (d.get("listeFormation") or [])]
    if any(forms):
        parts.append("Formation : " + " ; ".join(f for f in forms if f))
    langs = [l.get("libelleLangue") for l in (d.get("listeLangue") or []) if isinstance(l, dict) and l.get("libelleLangue")]
    if langs:
        parts.append("Langues : " + ", ".join(langs))
    desc = "\n\n".join(x for x in parts if x)
    if desc:
        upd["description"] = desc
    lt = d.get("lieuTravail") or {}
    if lt.get("codePostalLieuTravail"):
        upd["postal_code"] = lt["codePostalLieuTravail"]
    if lt.get("codeDepartementLieuTravail"):
        upd["department"] = lt["codeDepartementLieuTravail"]
    if d.get("libelleCodeLieuTravail"):
        upd["location"] = d["libelleCodeLieuTravail"]
    etab = d.get("etablissement") or {}
    name = clean(etab.get("libelleEnseigne"))
    if name and name.lower() != "enseigne non disponible":
        upd["company"] = name
    url = etab.get("urlEntreprise")
    if url and not etab.get("flagUrlEntrepriseInvalide"):
        upd["company_website"] = url if url.startswith("http") else "https://" + url
    sal = d.get("salaire")
    if isinstance(sal, dict):
        sal = sal.get("libelle")
    if sal and "selon" not in sal.lower() and "non" not in sal.lower()[:5]:
        lo, hi = parse_salary(sal)
        upd.update(salary_text=clean(sal), salary_min_eur=lo, salary_max_eur=hi)
    synth = d.get("syntheseContrat")
    ctype = _site_contract(synth)
    if ctype != ContractType.UNKNOWN:
        upd["contract_type"] = contract_from_title(p.title, ctype)
    exp = (d.get("experienceProfil") or {}).get("experience")
    if exp:
        upd["experience_level"] = exp
    cond = d.get("conditionTravailPoste") or {}
    upd["remote"] = detect_remote(d.get("deplacement"), cond.get("libelleConditionTravail"), cond.get("commentaireConditionTravail"), desc)
    raw = dict(p.raw)
    raw["detail"] = {k: d.get(k) for k in ("codeRome", "appellationRome", "qualification", "syntheseSecteurActivite", "dateDerniereModification", "flagOffrePartenaireAOM", "syntheseHoraire")}
    upd["raw"] = raw
    return p.model_copy(update=upd)


def parse_contact_html(zone_html: str) -> tuple[str | None, str]:
    """'Postuler' dropdown HTML -> (apply_url, apply_method)."""
    soup = BeautifulSoup(zone_html, "html.parser")
    for a in soup.select("a[href]"):
        href = a["href"]
        if href.startswith("mailto:"):
            return href, "email"
    for a in soup.select("a[href^=http]"):
        return a["href"].replace("&amp;", "&"), "external"
    text = soup.get_text(" ", strip=True)
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if m:
        return f"mailto:{m.group(0)}", "email"
    return None, "unknown"


# =========================================================================== API mapping

def api_to_posting(o: dict[str, Any]) -> JobPosting | None:
    oid = o.get("id")
    title = clean(o.get("intitule"))
    if not oid or not title:
        return None
    lt = o.get("lieuTravail") or {}
    libelle = lt.get("libelle") or ""
    m = re.match(r"\s*(\d{2,3}|2[AB])\s*-\s*(.+)", libelle)
    dep = m.group(1) if m else department_of(lt.get("codePostal"))
    city = (m.group(2) if m else lt.get("commune") or libelle).strip()
    if city.isupper():
        city = city.title()
    loc = f"{city} ({dep})" if dep and city else (city or None)
    ent = o.get("entreprise") or {}
    sal = o.get("salaire") or {}
    sal_text = clean(" ".join(x for x in (sal.get("libelle"), sal.get("complement1"), sal.get("complement2")) if x))
    lo, hi = parse_salary(sal.get("libelle")) if sal.get("libelle") else (None, None)
    contact = o.get("contact") or {}
    origin = o.get("origineOffre") or {}
    partners = origin.get("partenaires") or []
    apply_url = contact.get("urlPostulation") or contact.get("urlRecruteur")
    method = "unknown"
    if apply_url:
        method = "external"
    elif contact.get("courriel"):
        m2 = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", contact["courriel"])
        if m2:
            apply_url, method = f"mailto:{m2.group(0)}", "email"
    elif partners and partners[0].get("url"):
        apply_url, method = partners[0]["url"], "external"
    desc = html_to_text(o.get("description"))
    ctype = _api_contract(o.get("typeContrat"), o.get("natureContrat"), o.get("alternance"), title)
    if (o.get("natureContrat") or "") and "apprentissage" in (o.get("natureContrat") or "").lower():
        ctype = ContractType.ALTERNANCE
    origin_label = (partners[0].get("nom") if partners else None) or origin.get("origine")
    native = str(origin.get("origine")) == "1" and not partners
    return JobPosting(
        source="france_travail",
        source_id=str(oid),
        url=DETAIL_URL.format(id=oid),
        title=title,
        company=clean(ent.get("nom")),
        location=loc,
        postal_code=lt.get("codePostal"),
        department=dep,
        contract_type=ctype,
        remote=detect_remote(desc, o.get("complementExercice")),
        salary_text=sal_text,
        salary_min_eur=lo,
        salary_max_eur=hi,
        description=desc,
        posted_at=parse_fr_date(o.get("dateCreation")),
        apply_url=apply_url,
        apply_method=method,
        company_website=ent.get("url"),
        experience_level=clean(o.get("experienceLibelle")),
        raw={"api": o, "origin_board": None if native else origin_label, "native": native},
    )


# =========================================================================== source

class FranceTravailSource(Source):
    domains = ("francetravail.fr", "francetravail.io", "pole-emploi.fr")
    name = "france_travail"
    label = "France Travail"
    kind = "http"
    is_aggregator = True  # includes partner boards (Hellowork, Meteojob, APEC...): resolve to the origin before applying
    requires_credentials = ()  # optional: API mode if FRANCE_TRAVAIL_CLIENT_ID/SECRET are set
    contract_types = None

    def __init__(self) -> None:
        self._token: tuple[str, float] | None = None
        self._place_cache: dict[tuple, str | None] = {}

    # ------------------------------------------------------------------ public
    def api_enabled(self, ctx: SourceContext) -> bool:
        s = ctx.settings
        return bool(s.france_travail_client_id and s.france_travail_client_secret)

    async def search(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        if self.api_enabled(ctx):
            try:
                return await self._search_api(query, ctx)
            except SourceError as e:
                ctx.log.warning("france_travail API mode failed (%s); using the public website", e)
        return await self._search_site(query, ctx)

    async def fetch_details(self, posting: JobPosting, ctx: SourceContext) -> JobPosting:
        if "api" in posting.raw and posting.description and posting.apply_url:
            return posting  # API results are already complete
        pid = posting.source_id
        p = posting
        try:
            async with ctx.http() as c:
                r = await c.get(DETAIL_API.format(id=pid), headers={"Accept": "application/json"})
                if r.status_code == 200:
                    p = parse_detail_json(r.json(), p)
                else:
                    ctx.log.debug("france_travail detail %s -> %s", pid, r.status_code)
                await _pause(0.2, 0.5)
                r = await c.get(
                    CONTACT_URL, params={"t:ac": pid},
                    headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*"},
                )
                if r.status_code == 200:
                    zone = "".join(z[1] for z in r.json().get("_tapestry", {}).get("content", []) if len(z) > 1)
                    url, method = parse_contact_html(zone)
                    if url:
                        p = p.model_copy(update={"apply_url": url, "apply_method": method})
                    elif p.apply_method == "unknown":
                        # candidature en ligne on francetravail.fr itself
                        p = p.model_copy(update={"apply_url": p.url, "apply_method": "platform"})
        except (httpx.HTTPError, ValueError) as e:
            ctx.log.debug("france_travail details failed for %s: %s", pid, e)
        return p

    # ------------------------------------------------------------------ location helpers
    async def _site_lieux(self, query: SearchQuery, ctx: SourceContext) -> tuple[str | None, int | None]:
        """-> (`lieux` param, `rayon`). Commune INSEE code, '{dept}D' or '{region}R'."""
        key = (query.location, query.postal_code, query.department)
        if key in self._place_cache:
            return self._place_cache[key], self._radius(query, self._place_cache[key])
        lieux: str | None = None
        loc = (query.location or "").strip()
        async with ctx.http() as c:
            if query.postal_code or (loc and not re.fullmatch(r"(?i)france|toute la france|remote|télétravail", loc)):
                region = None
                if loc and not query.postal_code:
                    region = await find_region(c, loc.replace("Ile", "Île") if loc.lower().startswith("ile-") else loc)
                    if region is None and "ile" in loc.lower() and "france" in loc.lower():
                        region = {"code": "11"}
                if region:
                    lieux = f"{region['code']}R"
                else:
                    place = await geocode(c, loc or None, query.postal_code)
                    if place and place.get("insee"):
                        lieux = place["insee"]
                    elif place is None:
                        ctx.log.warning("france_travail: could not geocode %r; searching all France", loc)
            elif query.department:
                lieux = f"{query.department}D"
        if lieux is None and query.department:
            lieux = f"{query.department}D"
        self._place_cache[key] = lieux
        return lieux, self._radius(query, lieux)

    @staticmethod
    def _radius(query: SearchQuery, lieux: str | None) -> int | None:
        if not lieux or lieux.endswith(("R", "D")) or query.radius_km is None:
            return None
        return min(_RADII, key=lambda r: abs(r - query.radius_km))

    # ------------------------------------------------------------------ website mode
    def _site_groups(self, query: SearchQuery) -> list[tuple[str, dict[str, str], ContractType | None]]:
        """(name, extra params, hint) — one list request series per contract family."""
        cts = set(query.contract_types)
        if not cts:
            return [("any", {}, None)]
        groups: list[tuple[str, dict[str, str], ContractType | None]] = []
        types = [c for c, code in ((ContractType.CDI, "CDI"), (ContractType.CDD, "CDD"), (ContractType.INTERIM, "MIS")) if c in cts]
        # `typeContrat` takes a single value on the site; one request series per value
        for c, code in ((ContractType.CDI, "CDI"), (ContractType.CDD, "CDD"), (ContractType.INTERIM, "MIS")):
            if c in types:
                groups.append((code, {"typeContrat": code}, c))
        if ContractType.ALTERNANCE in cts:
            groups.append(("alternance", {"natureOffre": f"{NATURE_APPRENTISSAGE},{NATURE_PROFESSIONNALISATION}"}, ContractType.ALTERNANCE))
        if ContractType.STAGE in cts:
            groups.append(("stage", {"_stage": "1"}, ContractType.STAGE))
        if ContractType.FREELANCE in cts:
            groups.append(("portage", {"typeContrat": "PS"}, ContractType.FREELANCE))
        if cts & {ContractType.VIE, ContractType.OTHER, ContractType.UNKNOWN} and not groups:
            groups.append(("any", {}, None))
        return groups or [("any", {}, None)]

    async def _search_site(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        lieux, rayon = await self._site_lieux(query, ctx)
        groups = self._site_groups(query)
        want = query.max_results
        per_group = max(5, -(-want // len(groups)))  # ceil
        seen: dict[str, JobPosting] = {}
        for gi, (gname, extra, hint) in enumerate(groups):
            extra = dict(extra)
            kw = query.keywords.strip()
            is_stage = extra.pop("_stage", None) is not None
            if is_stage and not re.search(r"\bstages?\b", kw, re.I):
                kw = f"stage {kw}"
            params: dict[str, str] = {"motsCles": kw, "offresPartenaires": "true", "tri": "1"}
            if lieux:
                params["lieux"] = lieux
                if rayon:
                    params["rayon"] = str(rayon)
            e = _emission(query.posted_within_days)
            if e:
                params["emission"] = str(e)
            params.update(extra)
            got = 0
            for start in range(0, per_group + SITE_PAGE, SITE_PAGE):
                if got >= per_group or len(seen) >= want:
                    break
                params["range"] = f"{start}-{start + SITE_PAGE - 1}"
                url = f"{SEARCH_URL}?{urlencode(params)}"
                html = await get_html(url, ctx, site="france_travail")
                rows = parse_search_html(html)
                if not rows:
                    break
                for r in rows:
                    p = listing_to_posting(r, contract_hint=hint)
                    if not p:
                        continue
                    if is_stage and p.contract_type != ContractType.STAGE:
                        continue  # keyword "stage" matched a non-internship posting
                    if p.source_id in seen:
                        continue
                    seen[p.source_id] = p
                    got += 1
                    if got >= per_group or len(seen) >= want:
                        break
                if len(rows) < SITE_PAGE:
                    break
                await _pause()
            if gi < len(groups) - 1:
                await _pause()
        out = list(seen.values())
        if query.remote_only:
            ctx.log.debug("france_travail: remote_only can only be checked after fetch_details")
        return out[:want]

    # ------------------------------------------------------------------ API mode
    async def _get_token(self, ctx: SourceContext, c: httpx.AsyncClient) -> str:
        if self._token and self._token[1] > time.monotonic() + 30:
            return self._token[0]
        s = ctx.settings
        r = await c.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": s.france_travail_client_id,
                "client_secret": s.france_travail_client_secret,
                "scope": API_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            raise SourceError(f"France Travail OAuth failed: HTTP {r.status_code} {r.text[:200]}")
        j = r.json()
        self._token = (j["access_token"], time.monotonic() + float(j.get("expires_in", 1499)))
        return self._token[0]

    @staticmethod
    def _api_keywords(kw: str) -> str:
        # the API rejects some punctuation and long strings
        return re.sub(r"[^\w\s+#.-]", " ", kw)[:200].strip()

    async def _api_params(self, query: SearchQuery, ctx: SourceContext, c: httpx.AsyncClient) -> dict[str, str]:
        params: dict[str, str] = {"sort": "1"}
        loc = (query.location or "").strip()
        if query.postal_code or (loc and not re.fullmatch(r"(?i)france|toute la france|remote", loc)):
            region = await find_region(c, loc) if (loc and not query.postal_code) else None
            if region:
                params["region"] = region["code"]
            else:
                place = await geocode(c, loc or None, query.postal_code)
                if place and place.get("insee"):
                    params["commune"] = place["insee"]
                    if query.radius_km:
                        params["distance"] = str(min(query.radius_km, 200))
        if "commune" not in params and "region" not in params and query.department:
            params["departement"] = query.department
        e = _emission(query.posted_within_days)
        if e:
            params["publieeDepuis"] = str(e)
        return params

    async def _search_api(self, query: SearchQuery, ctx: SourceContext) -> list[JobPosting]:
        cts = set(query.contract_types)
        romes = (query.rome_codes or lookup_rome(query.keywords))[:3]
        kw = self._api_keywords(query.keywords)
        async with ctx.http() as c:
            token = await self._get_token(ctx, c)
            base = await self._api_params(query, ctx, c)
            variants: list[dict[str, str]] = []
            contract_variants: list[tuple[dict[str, str], str | None]] = []
            if not cts:
                contract_variants.append(({}, None))
            else:
                for ct, code in ((ContractType.CDI, "CDI"), (ContractType.CDD, "CDD"), (ContractType.INTERIM, "MIS")):
                    if ct in cts:
                        contract_variants.append(({"typeContrat": code}, None))
                if ContractType.ALTERNANCE in cts:
                    contract_variants.append(({"natureContrat": f"{NATURE_APPRENTISSAGE},{NATURE_PROFESSIONNALISATION}"}, None))
                if ContractType.STAGE in cts:
                    contract_variants.append(({}, "stage"))
                if not contract_variants:
                    contract_variants.append(({}, None))
            for extra, flavour in contract_variants:
                k = f"stage {kw}" if flavour == "stage" and "stage" not in kw.lower() else kw
                # ROME + keywords, then keywords only, then ROME only: the first non-empty one wins
                combos = []
                if romes:
                    combos.append({"codeROME": ",".join(romes), "motsCles": k})
                combos.append({"motsCles": k})
                if romes and flavour != "stage":
                    combos.append({"codeROME": ",".join(romes)})
                for cb in combos:
                    variants.append({**base, **extra, **cb, "_flavour": flavour or "", "_group": str(extra)})
            seen: dict[str, JobPosting] = {}
            done_groups: set[str] = set()
            per = max(5, -(-query.max_results // max(1, len(contract_variants))))
            group_count: dict[str, int] = {}
            for v in variants:
                g = v.pop("_group")
                flavour = v.pop("_flavour")
                if g in done_groups:
                    continue
                for start in range(0, min(per, API_MAX), API_PAGE):
                    end = min(start + API_PAGE, API_MAX) - 1
                    rows = await self._api_page(c, token, {**v, "range": f"{start}-{end}"}, ctx)
                    if not rows:
                        break
                    for o in rows:
                        p = api_to_posting(o)
                        if not p or p.source_id in seen:
                            continue
                        if flavour == "stage" and p.contract_type != ContractType.STAGE:
                            continue
                        seen[p.source_id] = p
                        group_count[g] = group_count.get(g, 0) + 1
                    if group_count.get(g, 0) >= per or len(rows) < API_PAGE:
                        break
                    await asyncio.sleep(0.15)  # 10 req/s cap
                if group_count.get(g):
                    done_groups.add(g)
                await asyncio.sleep(0.15)
                if len(seen) >= query.max_results:
                    break
        out = list(seen.values())
        return out[: query.max_results]

    async def _api_page(self, c: httpx.AsyncClient, token: str, params: dict[str, str], ctx: SourceContext) -> list[dict]:
        for attempt in range(3):
            r = await c.get(API_SEARCH, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            if r.status_code == 429:
                await asyncio.sleep(float(r.headers.get("Retry-After", 1)) + 0.2)
                continue
            break
        if r.status_code == 204:
            return []
        if r.status_code in (200, 206):
            try:
                return r.json().get("resultats") or []
            except ValueError as e:
                raise SourceError(f"France Travail API: bad JSON: {e}") from e
        if r.status_code in (400, 404):
            ctx.log.debug("france_travail API %s for %s", r.status_code, params)
            return []
        raise SourceError(f"France Travail API HTTP {r.status_code}: {r.text[:200]}")
