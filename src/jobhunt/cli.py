"""Command line: `jobhunt serve | sources | profile-add | search | daily | schedule | shortlist | new | login`,
plus the sub-apps `cv` (tailored CVs) and `apply` (milestone 2b: list | dry-run | send | resolve | trust | answer | auto)."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
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


@app.command("reresolve")
def reresolve(
    profile_id: Annotated[Optional[int], typer.Argument(help="profil (par défaut : tous)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="compte les transitions sans écrire")] = False,
    explain: Annotated[int, typer.Option("--explain", min=0, help="affiche les motifs de rejet des N premières offres par transition")] = 0,
) -> None:
    """Reclasse les ATS des offres enregistrées sans relancer la recherche."""
    from jobhunt.origin import reresolve_stored

    explanations = {}
    counts = asyncio.run(reresolve_stored(profile_id, dry_run=dry_run, explain=explain, explanations=explanations))
    for (before, after), count in sorted(counts.items()):
        typer.echo(f"{before} → {after}: {count}")
        for attempts in explanations.get((before, after), ()):
            if attempts:
                typer.echo("  " + "; ".join(f"{attempt['host'] or '—'}: {attempt['reason']}" for attempt in attempts))


@app.command("origins")
def origins(
    profile_id: int,
    min_score: Annotated[float, typer.Option("--min-score", help="score minimal des offres à traiter")] = 50.0,
    limit: Annotated[Optional[int], typer.Option("--limit", "-n", help="nombre maximal d'offres (meilleur score d'abord)")] = None,
    force: Annotated[bool, typer.Option("--force", help="reprend aussi les origines sous le seuil de confiance")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="rapport seulement : rien n'est enregistré")] = False,
    refetch: Annotated[bool, typer.Option("--refetch/--no-refetch", help="relit la page des annonces sans lien de candidature (LinkedIn...)")] = True,
    details: Annotated[int, typer.Option("--details", help="nombre de lignes détaillées pour les échecs")] = 40,
    trace: Annotated[int, typer.Option("--trace", help="affiche la trace complète (étapes, requêtes, candidats) des N premiers échecs")] = 0,
) -> None:
    """Retrouve le lien de l'entreprise (site carrières / ATS) des offres déjà enregistrées.

    Reprend les offres au score voulu dont l'origine manque ou est un lien de site d'annonces (effacé s'il ne peut pas être
    remplacé), en ignorant le cache d'échecs. Navigateur sans fenêtre. Le rapport ne contient que entreprises, titres et hôtes."""
    from jobhunt import origin as origin_mod
    from jobhunt.pipeline import reresolve_origins

    if db.get_profile(profile_id) is None:
        raise typer.BadParameter(f"profil {profile_id} introuvable")

    def progress(done: int, total: int) -> None:
        if done == total or done % 10 == 0:
            console.print(f"  {done}/{total}")

    rep = asyncio.run(reresolve_origins(profile_id, min_score=min_score, limit=limit, force=force, dry_run=dry_run,
                                        refetch=refetch, headless=True, progress=progress))
    mode = " [yellow](simulation : rien d'enregistré)[/yellow]" if dry_run else ""
    console.print(f"\nOffres au score ≥ {min_score:g} : {rep.total} · avec lien entreprise avant : {rep.with_origin_before} · "
                  f"après : {rep.with_origin_after}{mode}")
    console.print(f"Reprises : {rep.tried} · liens entreprise trouvés : {rep.found} · liens d'annonces effacés : {rep.cleared} · "
                  f"pages relues : {rep.refetched} (liens de candidature récupérés : {rep.apply_urls_found})")
    if rep.li_calls:
        console.print(f"Détails LinkedIn : {rep.li_calls} appel(s) · liens entreprise récupérés : {rep.li_links} · Easy Apply : {rep.li_easy} · "
                      f"fermées : {rep.li_closed} · échecs : {rep.li_failures} · coût : {rep.li_cost:.4f} $")
    if rep.by_via():
        t = Table("étape", "trouvés", title="Liens trouvés par étape")
        for via, n in rep.by_via().items():
            t.add_row(via, str(n))
        console.print(t)
    if rep.by_reason():
        t = Table("raison", "offres", title="Échecs par raison")
        for code, n in rep.by_reason().items():
            t.add_row(origin_mod.REASON_LABELS.get(code, code), str(n))
        console.print(t)
        misses = [r for r in rep.rows if not r.found]
        t = Table("score", "entreprise", "poste", "raison", "détail", title=f"Échecs (meilleurs scores, {min(details, len(misses))}/{len(misses)})")
        for r in misses[:details]:
            t.add_row(str(int(r.score)), r.company[:28], r.title[:48], origin_mod.REASON_LABELS.get(r.reason, r.reason),
                      r.detail + ("" if r.has_apply_url else " (sans lien de candidature)"))
        console.print(t)
        for r in misses[:trace]:
            console.print(f"\n[bold]Trace — {r.company} | {r.title[:60]} (score {int(r.score)})[/bold]", markup=True)
            for line in r.trace or ["(aucune trace : résultat issu du cache)"]:
                console.print("  " + line, markup=False, highlight=False)
    for e in sorted(set(rep.errors))[:5]:
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


apply_app = typer.Typer(help="Candidature sur le site de l'entreprise : file d'envoi, simulation, envoi, réponses aux questions.",
                        no_args_is_help=True)
app.add_typer(apply_app, name="apply")
HeadlessOpt = Annotated[Optional[bool], typer.Option("--headless/--headful", help="défaut : visible, sauf JOBHUNT_HEADLESS=1")]
BUCKET_FR = {"to_validate": "à valider", "paused": "en pause", "sending": "en cours", "failed": "échec", "blocked": "bloquée",
             "sent": "envoyée", "needs_verification": "à vérifier"}


def _print_run(run) -> None:
    console.print(f"Statut : {run.status} — {run.message}", markup=False, highlight=False)
    if run.result and run.result.untrusted_host:
        console.print(f"Site non approuvé : {run.result.untrusted_host} — `big-penalty apply trust PROFIL {run.result.untrusted_host}` "
                      "pour lui faire confiance, puis relancer.", markup=False, highlight=False)
    for st in (run.result.steps if run.result else []):
        console.print(f"  étape {st.index} · {st.page_kind} · {st.url}", markup=False, highlight=False)
        for f in st.filled:
            note = f" (déduite : {f.basis})" if getattr(f, "inferred", False) else ""
            console.print(f"    {f.label} ← {f.fact or f.op}{note}", markup=False, highlight=False)
        if st.clicked:
            console.print(f"    clic : {st.clicked}", markup=False, highlight=False)
        if st.screenshot:
            console.print(f"    capture : {st.screenshot}", markup=False, highlight=False)
    if run.result and run.result.questions:
        console.print("Questions : " + " ; ".join(q.label for q in run.result.questions), markup=False, highlight=False)


@apply_app.command("list")
def apply_list(profile: Annotated[Optional[int], typer.Option("--profile", "-p")] = None) -> None:
    """File d'envoi : chaque candidature avec son état, son dernier essai et ce qui la bloque."""
    from jobhunt.apply.service import queue

    t = Table("id", "score", "entreprise", "poste", "état", "essai", "problèmes")
    for i in queue(profile):
        shown = i.outstanding or i.last_run
        last = (f"n°{shown.id} · /apply/runs/{shown.id}" if i.outstanding else
                f"{shown.status} — {shown.message}"[:60] if shown else "")
        t.add_row(str(i.app.id), "" if i.score is None else f"{i.score:.0f}", (i.app.company or "")[:25], (i.app.job_title or "")[:40],
                  BUCKET_FR[i.bucket], last,
                  " ; ".join(([f"formulaire hébergé sur {i.untrusted_host} (apply trust)"] if i.untrusted_host else []) + i.problems)[:70])
    console.print(t)


@apply_app.command("diagnostics")
def apply_diagnostics(
    days: Annotated[int, typer.Option("--days", min=1, max=3650, help="nombre de jours à examiner")] = 30,
    database: Annotated[Optional[Path], typer.Option("--db", help="autre base locale (ex. base de test)")] = None,
) -> None:
    """Bilan local agrégé des échecs et pauses, sans ouvrir de site ni envoyer de données."""
    from jobhunt.apply.diagnostics import failure_breakdown

    try:
        groups = failure_breakdown(days=days, path=database)
    except sqlite3.Error:
        console.print("Diagnostic indisponible : base locale absente, illisible ou sans historique compatible.")
        raise typer.Exit(1) from None
    if not groups:
        console.print(f"Aucun échec ni pause enregistré sur les {days} derniers jours.")
        return
    table = Table(padding=(0, 0))
    for label in ("rang", "mode", "ATS", "catégorie", "motif", "dossiers", "essais"):
        table.add_column(label, overflow="fold")
    rank = 0
    for group in groups:
        if group.rankable:
            rank += 1
        table.add_row(str(rank) if group.rankable else "—", group.mode, group.ats, group.category, group.reason,
                      str(group.applications), str(group.runs))
    console.print(table)
    if not rank:
        console.print("Aucun échec moteur récurrent attribué à un ATS connu.")


@apply_app.command("dry-run")
def apply_dry_run(app_id: int, headless: HeadlessOpt = None) -> None:
    """Inspecte le formulaire sans saisir de données, joindre de document ni cliquer."""
    from jobhunt.apply.errors import ApplyError
    from jobhunt.apply.service import run

    try:
        res = asyncio.run(run(app_id, mode="dry_run", trigger="user", headless=headless))
    except ApplyError as e:
        console.print(f"Erreur : {e}", markup=False)
        raise typer.Exit(1) from None
    _print_run(res)
    if res.status not in ("ready", "needs_input", "needs_email", "needs_human"):
        raise typer.Exit(1)


@apply_app.command("supervised")
def apply_supervised(app_id: int) -> None:
    """Ouvre une candidature approuvée dans un navigateur visible. Vous cliquez sur Envoyer."""
    from jobhunt import db
    from jobhunt.apply.errors import ApplyError
    from jobhunt.apply.service import SUPERVISED_HANDOFF, origin_digest, run
    from jobhunt.apply.destination import audit_url, clean_url
    from jobhunt.apply.store import confirmation_tokens, potential_duplicates
    from jobhunt.cv.store import get_application

    app = get_application(app_id)
    if app is None:
        raise typer.BadParameter("candidature introuvable")
    job = db.get_job(app.job_id)
    destination = audit_url(clean_url(job.origin_url, navigation=True)) if job else ""
    console.print(f"Candidature {app.id} · CV révision {app.current_rev} · {destination or 'destination inconnue'}", markup=False)
    console.print(SUPERVISED_HANDOFF, markup=False)
    console.print(f"Pour la saisie, utilisez « Remplir cette étape » ou lancez big-penalty apply fill-step {app_id} dans un autre terminal.")
    if not typer.confirm("La saisie et le dépôt du CV peuvent transmettre des données avant le clic final. Continuer ?"):
        raise typer.Exit(1)
    confirmations = []
    tokens = confirmation_tokens(app.profile_id, app.id)
    for other, exact in sorted(potential_duplicates(app.profile_id, app.id).items()):
        if exact or other not in tokens or not typer.confirm(f"Confirmez : la candidature n°{app.id} n'est pas un doublon de la candidature n°{other} ?"):
            raise typer.Exit(1)
        confirmations.append(tokens[other])
    try:
        result = asyncio.run(run(app_id, mode="supervised", trigger="user", headless=False, expected_rev=app.current_rev,
                                 expected_origin=origin_digest(job.origin_url if job else None),
                                 duplicate_confirmations=tuple(confirmations)))
    except ApplyError as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from None
    _print_run(result)
    if result.status == "blocked":
        raise typer.Exit(1)


@apply_app.command("fill-step")
def apply_fill_step(app_id: Annotated[int, typer.Argument(help="numéro de candidature", min=1)]) -> None:
    """Remplit la page affichée dans la fenêtre supervisée, sans cliquer."""
    from jobhunt.apply.errors import ApplyError
    from jobhunt.apply.supervised import call_local

    try:
        preview = asyncio.run(call_local(app_id, "preview"))
        console.print(f"Offre réservée : {preview['company']} · {preview['title']} · {preview['reserved_url']}", markup=False)
        console.print(f"Page ouverte : {preview['page_url']}", markup=False)
        if preview["warning"]:
            console.print("ATTENTION : le titre de la page ne ressemble ni au poste ni à l’entreprise réservés.", style="bold yellow")
        if not typer.confirm("Confirmez-vous que la page ouverte concerne cette offre et autorisez-vous la saisie de cette étape ?"):
            raise typer.Exit(1)
        result = asyncio.run(call_local(app_id, "fill", preview["token"]))
    except ApplyError as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from None
    if result["status"] in ("blocked", "failed", "needs_human"):
        console.print("Saisie interrompue. Consultez l’essai ; si la fenêtre est ouverte, vérifiez le poste avant de reconfirmer.")
        raise typer.Exit(1)
    console.print("Étape vérifiée. Poursuivez dans la fenêtre supervisée ; aucun bouton n’a été cliqué.")


@apply_app.command("send")
def apply_send(app_ids: Annotated[list[int], typer.Argument(help="numéros de candidature")], headless: HeadlessOpt = None,
               yes: Annotated[bool, typer.Option("--yes", "-y", help="ne pas demander confirmation")] = False) -> None:
    """Envoie les candidatures choisies via un adaptateur. Sans adaptateur, utilisez supervised."""
    from jobhunt.apply.service import send_selected
    from jobhunt.cv.store import get_application

    apps = [get_application(i) for i in app_ids]
    missing = [i for i, a in zip(app_ids, apps) if a is None]
    if missing:
        raise typer.BadParameter("candidature(s) introuvable(s) : " + ", ".join(map(str, missing)))
    if not yes:
        console.print("Envoyer réellement ces candidatures (CV approuvé par vous) :", markup=False)
        for a in apps:
            console.print(f"  {a.id} · {a.company or '?'} · {a.job_title or '?'}", markup=False, highlight=False)  # type: ignore[union-attr]
        if not typer.confirm("Confirmer l'envoi ?"):
            raise typer.Exit(1)
    from jobhunt.apply.errors import ApplyError

    def progress(app_id: int, run) -> None:
        console.print(f"Candidature {app_id}", markup=False)
        _print_run(run)

    try:
        runs = asyncio.run(send_selected(app_ids, headless=headless, on_progress=progress,
                                         expected_revs={a.id: a.current_rev for a in apps}))  # type: ignore[union-attr]
    except ApplyError as e:
        console.print(f"Erreur : {e}", markup=False)
        raise typer.Exit(1) from None
    if any(r.status != "submitted" for r in runs) or len(runs) < len(set(app_ids)):
        raise typer.Exit(1)


@apply_app.command("resolve")
def apply_resolve(app_id: int, run_id: Annotated[int, typer.Option("--run-id", min=1)],
                  sent: Annotated[bool, typer.Option("--sent/--not-sent", help="résultat vérifié : envoyée / non envoyée")]) -> None:
    """Confirme le résultat de l'essai indiqué, uniquement après avoir vérifié sa réception ou sa non-réception."""
    from jobhunt.apply.errors import ApplyError
    from jobhunt.apply.service import resolve_unconfirmed
    from jobhunt.cv.errors import CVError

    try:
        out = resolve_unconfirmed(app_id, sent=sent, run_id=run_id)
    except (ApplyError, CVError) as e:
        console.print(f"Erreur : {e}", markup=False)
        raise typer.Exit(1) from None
    console.print("Candidature marquée comme envoyée." if sent else
                  "Essai marqué non envoyé : un autre essai reste à vérifier." if out.status == "failed" else
                  "Candidature remise en brouillon : tout nouvel envoi doit être lancé manuellement.", markup=False)


@apply_app.command("resolve-many")
def apply_resolve_many(
    attempts: Annotated[list[str], typer.Argument(help="paires CANDIDATURE:ESSAI déjà vérifiées")],
    sent: Annotated[bool, typer.Option("--sent/--not-sent", help="elles ont bien / n'ont pas été envoyées")],
) -> None:
    """Règle d'un coup plusieurs envois non confirmés (mêmes règles que `resolve`, une par une) puis résume."""
    from jobhunt.apply.errors import ApplyError
    from jobhunt.apply.service import resolve_unconfirmed
    from jobhunt.cv.errors import CVError

    pairs = []
    for attempt in attempts:
        parts = attempt.split(":")
        if len(parts) != 2 or not all(p.isascii() and p.isdecimal() and len(p) <= 12 and int(p) > 0 for p in parts):
            raise typer.BadParameter("Indiquer chaque essai sous la forme CANDIDATURE:ESSAI")
        pairs.append(tuple(map(int, parts)))
    ok, failed = 0, []
    for app_id, run_id in dict.fromkeys(pairs):
        try:
            resolve_unconfirmed(app_id, sent=sent, run_id=run_id)
        except (ApplyError, CVError) as e:
            failed.append(f"{app_id}:{run_id}")
            console.print(f"  n°{app_id}:{run_id} : {e}", markup=False, highlight=False)
        else:
            ok += 1
    what = "confirmé(s) envoyé(s)" if sent else "confirmé(s) non envoyé(s)"
    console.print(f"{ok} essai(s) {what}" + (f", {len(failed)} refusé(s) : {', '.join(failed)}." if failed else "."),
                  markup=False, highlight=False)
    if failed:
        raise typer.Exit(1)


@apply_app.command("trust")
def apply_trust(profile_id: int, host: Annotated[str, typer.Argument(help="nom d'hôte, par exemple jobs.exemple.fr")]) -> None:
    """Faire confiance à un site d'hébergement de formulaires (sinon l'outil refuse d'y saisir ou d'y envoyer quoi que ce soit)."""
    from jobhunt.apply.errors import ApplyError
    from jobhunt.apply.facts import add_trusted_host

    if db.get_profile(profile_id) is None:
        raise typer.BadParameter(f"profil {profile_id} introuvable")
    try:
        h = add_trusted_host(profile_id, host)
    except ApplyError as e:
        console.print(f"Erreur : {e}", markup=False)
        raise typer.Exit(1) from None
    console.print(f"{h} ajouté aux sites de confiance du profil {profile_id} (answers.yaml). Relancer l'essai ou l'envoi.", markup=False)


@apply_app.command("answer")
def apply_answer(app_id: int) -> None:
    """Répond aux questions du dernier essai en pause ; les réponses servent pour tous les formulaires suivants."""
    from jobhunt.apply.missing_answers import editable_custom
    from jobhunt.apply.service import answer_questions
    from jobhunt.apply.store import last_run

    run = last_run(app_id)
    qs = [question for question in run.result.questions if editable_custom(question)] if run and run.result else []
    if not qs:
        console.print("Aucune question en attente pour cette candidature.", markup=False)
        raise typer.Exit(1)
    answers = []
    for q in qs:
        hint = f" [{' / '.join(q.options)}]" if q.options else ""
        answers.append((q.label, typer.prompt(q.label + hint, default="", show_default=False)))
    answer_questions(app_id, answers)
    console.print(f"{sum(1 for _, a in answers if a.strip())} réponse(s) enregistrée(s) : relancer avec `apply send {app_id}`.",
                  markup=False)


@apply_app.command("auto")
def apply_auto(
    profile_id: int,
    headful: Annotated[bool, typer.Option("--headful/--headless", help="fenêtres visibles (beaucoup de sites bloquent le mode invisible)")] = False,
    limit: Annotated[Optional[int], typer.Option("--limit", min=1, help="arrêter après N candidatures envoyées")] = None,
    min_score: Annotated[Optional[float], typer.Option("--min-score", help="ne traiter que les offres de fit >= S (relève les seuils, jamais ne les abaisse)")] = None,
    attended: Annotated[bool, typer.Option("--attended", help="quelqu'un surveille la fenêtre : elle attend la résolution d'un CAPTCHA")] = False,
) -> None:
    """Passe automatique : prépare les CV, approuve (score suffisant) et envoie si l'envoi automatique est activé (comme le run quotidien)."""
    from jobhunt.apply.service import daily_apply
    from jobhunt.daily import apply_line

    if db.get_profile(profile_id) is None:
        raise typer.BadParameter(f"profil {profile_id} introuvable")
    if attended and not headful:
        raise typer.BadParameter("--attended demande des fenêtres visibles (--headful)")
    summary = asyncio.run(daily_apply(profile_id, emit=lambda line: console.print(line, markup=False, highlight=False),
                                      headless=not headful, limit=limit, min_score=min_score, attended=attended))
    console.print(apply_line(summary) + f" ({summary.prepared} CV préparé(s))", markup=False)
    for e in summary.errors:
        console.print("  erreur : " + e, markup=False, highlight=False)


def _mask_email(email: str) -> str:
    local, at, domain = email.partition("@")
    return (local[:1] + "\u2026" + at + domain) if at else (email[:1] + "\u2026")


def _accounts_call(fn, *args, **kw):
    from jobhunt.apply.errors import ApplyError

    try:
        return fn(*args, **kw)
    except ApplyError as e:
        console.print(f"Erreur : {e}", markup=False, highlight=False)
        raise typer.Exit(1) from None


@apply_app.command("accounts")
def apply_accounts() -> None:
    """Liste les comptes créés sur les sites d'entreprises (jamais les mots de passe)."""
    from jobhunt.apply import accounts

    rows = _accounts_call(accounts.list_accounts)
    t = Table("site", "e-mail", "créé le", "état", "enregistré")
    for r in rows:
        t.add_row(r.get("host", ""), _mask_email(r["account"]), str(r.get("created_at", ""))[:10],
                  "en attente (inscription non confirmée)" if r.get("state") == "pending" else "actif", "oui" if r["stored"] else "non")
    console.print(t)
    console.print(f"Stockage : {_accounts_call(accounts.backend_name)}", markup=False, highlight=False)


@apply_app.command("accounts-export")
def apply_accounts_export(file: Annotated[Path, typer.Argument(help="fichier JSON à écrire")],
                          overwrite: Annotated[bool, typer.Option("--overwrite", help="écraser le fichier s'il existe")] = False) -> None:
    """Exporte les comptes (avec mots de passe) vers un fichier, pour les importer sur une autre machine."""
    from jobhunt.apply import accounts

    n = _accounts_call(accounts.export_accounts, file, overwrite=overwrite)
    console.print(f"{n} compte(s) exporté(s) dans {file}.", markup=False, highlight=False)
    console.print("ATTENTION : ce fichier contient les mots de passe en clair. Le supprimer dès l'import terminé.",
                  markup=False, highlight=False)


@apply_app.command("accounts-import")
def apply_accounts_import(file: Annotated[Path, typer.Argument(help="fichier JSON produit par accounts-export")],
                          overwrite: Annotated[bool, typer.Option("--overwrite", help="remplacer les mots de passe différents")] = False) -> None:
    """Importe les comptes d'un fichier d'export (les comptes existants avec un autre mot de passe sont ignorés)."""
    from jobhunt.apply import accounts

    imported, skipped = _accounts_call(accounts.import_accounts, file, overwrite=overwrite)
    console.print(f"{imported} compte(s) importé(s), {skipped} ignoré(s) (déjà présents avec un autre mot de passe)."
                  if skipped else f"{imported} compte(s) importé(s).", markup=False, highlight=False)


@apply_app.command("accounts-delete")
def apply_accounts_delete(host: Annotated[str, typer.Argument(help="site, par exemple jobs.exemple.fr")], email: str,
                          yes: Annotated[bool, typer.Option("--yes", "-y", help="sans confirmation")] = False) -> None:
    """Supprime un compte (mot de passe et entrée de la liste)."""
    from jobhunt.apply import accounts

    if not yes and not typer.confirm(f"Supprimer le compte {_mask_email(email)} sur {host} ?"):
        raise typer.Abort()
    _accounts_call(accounts.delete_account, host, email)
    console.print("Compte supprimé.", markup=False)


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
