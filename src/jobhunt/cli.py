"""Command line: `jobhunt serve | sources | profile-add | search | daily | schedule | shortlist | new | login`."""

from __future__ import annotations

import asyncio
import json
import re
from enum import Enum
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

app = typer.Typer(help="Big Penalty (jobhunt) : recherche d'emploi multi-sources (France).", no_args_is_help=True)
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


def _jobs_table(jobs) -> Table:
    t = Table("id", "score", "âge (j)", "poste", "entreprise", "lieu", "contrat", "sources", "statut", "origine")
    for j in jobs:
        age = age_days(j)
        age_s = "?" if age is None else f"{int(age)}{'*' if is_repost(j) else ''}"
        t.add_row(str(j.id), str(int(j.score or 0)), age_s, j.title[:50], (j.company or "")[:25], (j.location or "")[:22],
                  j.contract_type.value, "+".join(j.sources), j.status, "oui" if j.origin_url else "")
    return t


@app.command()
def shortlist(profile_id: int, min_score: float = 0, limit: int = 40, all: bool = False) -> None:
    """Affiche les meilleures offres (tri score × fraîcheur, offres de plus de 14 jours masquées ; * = republiée)."""
    console.print(_jobs_table(db.query_jobs(profile_id, min_score=min_score, show_filtered=all)[:limit]))


@app.command("new")
def new_jobs(profile_id: int, limit: int = 40) -> None:
    """Nouveautés du jour (vues pour la première fois depuis minuit ou depuis la dernière recherche), triées par pertinence."""
    if db.get_profile(profile_id) is None:
        raise typer.BadParameter(f"profil {profile_id} introuvable")
    jobs = db.query_jobs(profile_id, new_only=True)
    console.print(_jobs_table(jobs[:limit]))
    console.print(f"{len(jobs)} nouvelle(s) offre(s), dont {sum(1 for j in jobs if (j.score or 0) >= 70)} ≥ 70.")


@app.command()
def daily(profile_ids: Annotated[Optional[list[int]], typer.Argument(help="défaut : tous les profils")] = None) -> None:
    """Recherche quotidienne (sans fenêtre de navigateur) de chaque profil, puis résumé des nouveautés du jour.

    Un verrou (data/daily.lock) empêche deux exécutions simultanées ; journal dans data/logs/daily-AAAA-MM-JJ.log ;
    notification macOS à la fin (désactivable avec JOBHUNT_NO_NOTIFY=1). Code de sortie ≠ 0 seulement en cas d'erreur réelle.
    """
    from jobhunt.daily import run_daily

    try:
        report = run_daily(profile_ids or None, emit=lambda line: console.print(line, markup=False, highlight=False))
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    if report.failed:
        raise typer.Exit(1)


schedule_app = typer.Typer(help="Planification quotidienne via launchd (macOS), plusieurs heures par jour possibles.",
                           no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")


def _sched_call(fn, *args, **kw):
    from jobhunt import schedule as sched

    try:
        return fn(*args, **kw)
    except sched.ScheduleError as e:
        console.print(f"Erreur : {e}", markup=False)
        raise typer.Exit(1) from None


def _print_times(times: list[str]) -> None:
    console.print("Heures : " + ", ".join(times) + " (rattrapage au réveil si le Mac dormait).", markup=False)


@schedule_app.command("install")
def schedule_install(
    time: Annotated[Optional[list[str]], typer.Option("--time", help="HH:MM, répétable (défaut 07:30)")] = None,
) -> None:
    """Installe l'agent launchd (remplace l'existant)."""
    from jobhunt import schedule as sched

    times = _sched_call(sched.normalize_times, time or [sched.DEFAULT_TIME])
    path = _sched_call(sched.install, times)
    console.print(f"Installé : {path}", markup=False)
    _print_times(times)


@schedule_app.command("set")
def schedule_set(times: Annotated[list[str], typer.Argument(help="HH:MM [HH:MM ...]")]) -> None:
    """Remplace toutes les heures (installe si besoin)."""
    from jobhunt import schedule as sched

    norm = _sched_call(sched.normalize_times, times)
    _sched_call(sched.set_times, norm)
    _print_times(norm)


@schedule_app.command("add")
def schedule_add(time: str) -> None:
    """Ajoute une heure."""
    from jobhunt import schedule as sched

    _print_times(_sched_call(sched.add_time, time))


@schedule_app.command("remove")
def schedule_remove(time: str) -> None:
    """Retire une heure (la dernière : utiliser uninstall)."""
    from jobhunt import schedule as sched

    _print_times(_sched_call(sched.remove_time, time))


@schedule_app.command("run")
def schedule_run() -> None:
    """Lance une recherche maintenant via launchd (retourne immédiatement)."""
    from jobhunt import schedule as sched

    _sched_call(sched.run_now)
    console.print("Recherche lancée (~25 min par profil). Suivi : `big-penalty schedule status`.", markup=False)


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Désinstalle l'agent et supprime le plist."""
    from jobhunt import schedule as sched

    console.print("Désinstallé." if _sched_call(sched.uninstall) else "Rien à désinstaller.", markup=False)


@schedule_app.command("status")
def schedule_status() -> None:
    """État : chargé, heures, prochain passage, exécution en cours, dernier journal."""
    from jobhunt import schedule as sched

    st = _sched_call(sched.status)
    console.print(f"Fichier plist : {'présent' if st.installed else 'absent'} · agent : {'chargé' if st.loaded else 'non chargé'}",
                  markup=False)
    if st.times:
        nxt = f" · prochain passage : {st.next_run:%Y-%m-%d %H:%M}" if st.loaded and st.next_run else ""
        console.print(f"Heures : {', '.join(st.times)}{nxt}", markup=False)
    console.print("Exécution : " + ("en cours" if st.running else "aucune en cours"), markup=False)
    if st.last_log:
        console.print(f"Dernier journal ({st.last_log.name}) :", markup=False)
        for line in st.log_tail or []:
            console.print("  " + line, markup=False, highlight=False)
    else:
        console.print("Aucun journal pour l'instant.", markup=False)


cv_app = typer.Typer(help="CV sur mesure : profil maître, adaptation à une offre, liste des candidatures.", no_args_is_help=True)
app.add_typer(cv_app, name="cv")


def _print_flags(flags) -> None:
    for f in flags:
        console.print(f"  [{ {'error': 'ERREUR', 'prep': 'prépa entretien'}.get(f.level, 'à vérifier') }] {f.location} : {f.message}"
                      + (f" → {f.hint}" if f.hint else ""), markup=False,
                      highlight=False)


@cv_app.command("import")
def cv_import(
    profile_id: int,
    pdf: Annotated[Optional[Path], typer.Option("--pdf", help="CV au format PDF (texte + photo)")] = None,
    text: Annotated[Optional[Path], typer.Option("--text", help="CV en texte brut")] = None,
    stage_qualifier: Annotated[Optional[str], typer.Option(
        "--stage-qualifier", help='Texte entre parenthèses dans « Recherche de stage (…) en … »')] = None,
) -> None:
    """Structure le CV en brouillon de profil maître (master.pending.yaml, UN appel IA) + rapport ; à confirmer dans l'interface."""
    from jobhunt.cv.importer import import_cv
    from jobhunt.cv.master import save_pending
    from jobhunt.llm import LLMError, get_llm

    profile = db.get_profile(profile_id)
    if profile is None:
        raise typer.BadParameter(f"profil {profile_id} introuvable")
    if (pdf is None) == (text is None):
        raise typer.BadParameter("fournir exactement --pdf ou --text")
    for f in (pdf, text):
        if f is not None and not f.is_file():
            raise typer.BadParameter(f"fichier introuvable : {f}")

    async def go():
        llm = get_llm()
        try:
            return await import_cv(profile_id, profile, llm, pdf=pdf, text_file=text)
        finally:
            await llm.aclose()

    try:
        res = asyncio.run(go())
    except (LLMError, ValueError) as e:
        console.print(f"Erreur : {e}", markup=False)
        raise typer.Exit(1) from None
    master, unverified, path = res.master, res.unverified, res.path
    if stage_qualifier is not None:
        master.identity.stage_qualifier = stage_qualifier
        save_pending(profile_id, master)
    bullets = sum(len(p.bullets) for p in master.projects)
    console.print(f"Brouillon de profil maître écrit : {path}", markup=False)
    console.print(f"À CONFIRMER dans l'interface (bouton « Confirmer ce CV maître ») : /profiles/{profile_id}/cv", markup=False)
    for d in res.disputed:
        console.print(f"  contesté : {d['field']} — {d['reason']}", markup=False)
    console.print(f"{len(master.skill_groups)} groupes de compétences ({len(master.skill_items())} éléments), "
                  f"{len(master.projects)} projets ({bullets} points), {len(master.education)} formations, "
                  f"{len(master.languages)} langues · photo : {'oui' if master.identity.photo else 'non'}", markup=False)
    if unverified:
        console.print(f"{len(unverified)} élément(s) non retrouvé(s) mot pour mot dans le CV source :", markup=False)
        for u in unverified:
            console.print("  - " + u, markup=False, highlight=False)
    else:
        console.print("Vérification : tout le contenu est retrouvé mot pour mot dans le CV source.", markup=False)


@cv_app.command("tailor")
def cv_tailor(job_id: int, profile: Annotated[Optional[int], typer.Option("--profile", "-p")] = None) -> None:
    """Génère le CV adapté à une offre (UN appel IA), vérifie les faits, produit le PDF d'une page."""
    from jobhunt.cv.service import CVError, is_eligible, prepare_application
    from jobhunt.llm import LLMError

    job = db.get_job(job_id)
    if job is None:
        raise typer.BadParameter(f"offre {job_id} introuvable")
    if profile is not None and profile != job.profile_id:
        raise typer.BadParameter(f"l'offre {job_id} appartient au profil {job.profile_id}")
    try:
        application = asyncio.run(prepare_application(job.profile_id, job_id))
    except (CVError, LLMError) as e:
        console.print(f"Erreur : {e}", markup=False)
        raise typer.Exit(1) from None
    console.print(f"Candidature {application.id} · {job.title} · {job.company or '?'} · statut {application.status}", markup=False)
    console.print("Titre du CV : " + application.tailored.headline, markup=False)
    console.print("Mots-clés couverts : " + ", ".join(application.tailored.matched_keywords), markup=False)
    console.print("Mots-clés ajoutés (à préparer) : " + ("; ".join(application.tailored.gaps) or "aucun"), markup=False)
    for a in application.tailored.adjustments:
        console.print("Ajustement : " + a, markup=False)
    console.print(f"{len(application.errors)} erreur(s), {len(application.warnings)} à vérifier, {len(application.preps)} point(s) à préparer", markup=False)
    _print_flags(application.flags)
    if application.notes:
        console.print(application.notes, markup=False)
    console.print(f"PDF : {application.cv_path or '(non généré)'}", markup=False)
    v = application.verifier
    console.print(f"Version {application.current_rev} · vérification IA : "
                  + (f"{'réussie' if v.passed else 'échec'} ({v.provider})" if v and v.ran else "non effectuée"), markup=False)
    ok, reasons = is_eligible(application)
    console.print("Éligible à l'envoi automatique : " + ("oui" if ok else "non — " + " ; ".join(reasons)), markup=False)
    if application.errors:
        raise typer.Exit(2)


@cv_app.command("list")
def cv_list(profile: Annotated[Optional[int], typer.Option("--profile", "-p")] = None) -> None:
    """Liste les candidatures (CV adaptés) et leur statut."""
    from jobhunt.cv.store import list_applications

    t = Table("id", "offre", "poste", "entreprise", "statut", "erreurs", "à vérifier", "PDF")
    for a in list_applications(profile):
        t.add_row(str(a.id), str(a.job_id), (a.job_title or "")[:45], (a.company or "")[:25], a.status,
                  str(len(a.errors)), str(len(a.warnings)), a.cv_path or "")
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
