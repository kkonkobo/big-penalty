"""Command line: `jobhunt serve | sources | profile-add | search | shortlist | login`."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.freshness import age_days, is_repost
from jobhunt.models import ContractType, Profile
from jobhunt.sources import registry

app = typer.Typer(help="jobhunt: recherche d'emploi multi-sources (France).", no_args_is_help=True)
console = Console()


@app.command("llm-check")
def llm_check() -> None:
    """Teste chaque fournisseur configuré (petite requête payante), sans afficher de secrets."""
    from jobhunt.llm import get_llm

    async def check():
        llm = get_llm()
        try:
            return await llm.check()
        finally:
            await llm.aclose()

    table = Table("Fournisseur", "État", "Latence (s)")
    results = asyncio.run(check())
    for result in results:
        table.add_row(result["provider"], result["status"], str(result["latency_s"]))
    console.print(table)
    if any(result["status"] != "ok" for result in results):
        raise typer.Exit(1)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    """Lance l'interface web."""
    import uvicorn

    uvicorn.run("jobhunt.web.app:app", host=host, port=port, reload=reload)


@app.command()
def sources() -> None:
    """Liste les sources, leur configuration et leur activation."""
    s = get_settings()
    t = Table("nom", "libellé", "type", "configurée", "activée", "login", "agrégateur")
    for name, cls in sorted(registry().items()):
        ok = cls().is_configured(s)
        missing = ", ".join(a for a in cls.requires_credentials if not getattr(s, a, None))
        t.add_row(name, cls.label, cls.kind, "oui" if ok else f"non ({missing})", "oui" if cls.enabled_by_default else "non",
                  "oui" if cls.requires_login else "", "oui" if cls.is_aggregator else "")
    console.print(t)
    console.print("Fournisseurs IA : " + " → ".join(s.llm_providers or ["règles"]))
    for name in s.llm_providers:
        available = bool(getattr(s, f"{name}_api_key"))
        console.print(f"  {name}: {'clé configurée' if available else 'clé absente'}")


def _load_profile_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        return json.loads(text)
    out: dict = {}  # tiny "key: value" format; lists are comma separated
    key = None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1)
            out[key] = m.group(2).strip()
        elif key and line.strip():
            out[key] += "\n" + line.strip()
    for k in ("interests", "skills", "locations", "languages", "contract_types"):
        if isinstance(out.get(k), str):
            out[k] = [x.strip() for x in re.split(r"[,\n]", out[k]) if x.strip()]
    return out


@app.command("profile-add")
def profile_add(
    file: Annotated[Optional[Path], typer.Option("--file", "-f", help="JSON ou fichier « clé: valeur »")] = None,
    name: Optional[str] = None,
    headline: Optional[str] = None,
    interest: Annotated[Optional[list[str]], typer.Option(help="répétable")] = None,
    contract: Annotated[Optional[list[str]], typer.Option(help="stage, alternance, cdi... répétable")] = None,
    location: Annotated[Optional[list[str]], typer.Option(help="répétable")] = None,
    skill: Annotated[Optional[list[str]], typer.Option(help="répétable")] = None,
    education: Optional[str] = None,
    experience_years: Optional[float] = None,
    cv_file: Optional[Path] = None,
    notes: Optional[str] = None,
) -> None:
    """Crée un profil (fichier et/ou options)."""
    data: dict = _load_profile_file(file) if file else {}
    for k, v in dict(name=name, headline=headline, education=education, experience_years=experience_years, notes=notes).items():
        if v is not None:
            data[k] = v
    for k, v in dict(interests=interest, contract_types=contract, locations=location, skills=skill).items():
        if v:
            data[k] = v
    if cv_file:
        data["cv_text"] = cv_file.read_text(encoding="utf-8")
    if not data.get("name") or not data.get("headline"):
        raise typer.BadParameter("name et headline sont obligatoires")
    if "experience_years" in data and isinstance(data["experience_years"], str):
        data["experience_years"] = float(data["experience_years"]) if data["experience_years"] else None
    data.pop("id", None)
    p = db.save_profile(Profile.model_validate(data))
    console.print(f"Profil créé : id={p.id} ({p.name})")


@app.command()
def search(
    profile_id: int,
    source: Annotated[Optional[list[str]], typer.Option("--source", "-s", help="répétable")] = None,
    max_queries: Annotated[Optional[int], typer.Option("--max-queries", "-n")] = None,
) -> None:
    """Planifie, cherche, dédoublonne et évalue les offres."""
    from jobhunt.pipeline import run_search

    if db.get_profile(profile_id) is None:
        raise typer.BadParameter(f"profil {profile_id} introuvable")

    def progress(ev: dict) -> None:
        if ev.get("event") == "stage":
            console.print(f"[bold]→ {ev['stage']}[/bold]")
        elif ev.get("event") == "query_done":
            console.print(f"  {ev['source']}: {ev['done']}/{ev['total']}")
        elif ev.get("event") in ("source_error", "error"):
            console.print(f"  [red]{ev.get('source', '')} {ev.get('error')}[/red]")

    res = asyncio.run(run_search(profile_id, sources=source or None, max_queries=max_queries, progress=progress))
    console.print(f"\nPlan : {len(res.plan)} requêtes · annonces brutes : {res.raw_postings} · "
                  f"offres uniques : {res.unique_jobs} (nouvelles : {res.new_jobs}) · "
                  f"évaluées : {res.scored} · écartées d'avance : {res.prefiltered}")
    for n, s in res.sources.items():
        extra = f" — {s.skipped_reason}" if s.skipped_reason else ""
        console.print(f"  {n}: {s.status}, {s.postings} annonces, {len(s.errors)} erreur(s){extra}")
        for e in s.errors[:2]:
            console.print(f"    [red]{e}[/red]")
    for e in res.errors:
        console.print(f"[yellow]! {e}[/yellow]")


@app.command()
def rescore(profile_id: int) -> None:
    """Réévalue les offres déjà enregistrées (après modification du profil), sans relancer la recherche."""
    from jobhunt.pipeline import rescore as _rescore

    if db.get_profile(profile_id) is None:
        raise typer.BadParameter(f"profil {profile_id} introuvable")

    def progress(ev: dict) -> None:
        if ev.get("event") == "score_batch":
            console.print(f"  lots : {ev['done']}/{ev['total']}")

    res = asyncio.run(_rescore(profile_id, progress=progress))
    console.print(f"Offres : {res.unique_jobs} · réévaluées : {res.scored} · écartées d'avance : {res.prefiltered}")
    for e in res.errors:
        console.print(f"[yellow]! {e}[/yellow]")


@app.command()
def shortlist(profile_id: int, min_score: float = 0, limit: int = 40, all: bool = False) -> None:
    """Affiche les meilleures offres (tri score × fraîcheur, offres de plus de 14 jours masquées ; * = republiée)."""
    jobs = [j for j in db.query_jobs(profile_id, min_score=min_score, show_filtered=all)][:limit]
    t = Table("id", "score", "âge (j)", "poste", "entreprise", "lieu", "contrat", "sources", "statut", "origine")
    for j in jobs:
        age = age_days(j)
        age_s = "?" if age is None else f"{int(age)}{'*' if is_repost(j) else ''}"
        t.add_row(str(j.id), str(int(j.score or 0)), age_s, j.title[:50], (j.company or "")[:25], (j.location or "")[:22],
                  j.contract_type.value, "+".join(j.sources), j.status, "oui" if j.origin_url else "")
    console.print(t)


@app.command()
def login(source: str, profile: Annotated[int, typer.Option("--profile", "-p")]) -> None:
    """Ouvre un navigateur visible pour se connecter à une source ; la session est conservée."""
    cls = registry().get(source)
    if cls is None:
        raise typer.BadParameter(f"source inconnue : {source}")
    url = getattr(cls, "login_url", None) or typer.prompt("URL de connexion")

    async def go() -> None:
        from jobhunt.browser import BrowserPool

        pool = BrowserPool()
        try:
            ctx = await pool.context(profile, source, headless=False)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url)
            await asyncio.to_thread(input, "Connectez-vous dans la fenêtre, puis appuyez sur Entrée ici… ")
        finally:
            await pool.close()

    asyncio.run(go())
    console.print("Session enregistrée.")
