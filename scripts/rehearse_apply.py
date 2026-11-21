"""Rehearse the autonomous apply engine on a REAL public posting with a FICTIONAL candidate. Nothing is ever sent.

    JOBHUNT_DATA_DIR=<scratch dir> python scripts/rehearse_apply.py URL --company "ACME" --title "Stage ingénieur"

The real engine (planner-trusting mode, the real planner LLM) reads the live page, fills the first form page with the
fictional data below, and stops before any click that could send it (final, "next" after data, login / signup). It prints
what each step filled, what it could not, and what it would have clicked; screenshots and the plans go to --out.
Headless only. Use a scratch JOBHUNT_DATA_DIR: never the real one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from patchright.async_api import async_playwright

from jobhunt.apply import engine
from jobhunt.apply.autonomous import Audience
from jobhunt.apply.planner import LLMPlanner
from jobhunt.apply.types import Fact, FactSheet
from jobhunt.config import get_settings
from jobhunt.llm import FailoverLLM

CV_HTML = """<html><body style="font-family:Arial;font-size:11pt;margin:36px">
<h1>Camille Durand</h1><p>camille.durand.test@example.org · 06 39 98 12 34 · Toulouse, France</p>
<h2>Formation</h2><p>2023 – 2026 · Cycle ingénieur mécatronique (2e année, Bac+4) · École d'ingénieurs de Toulouse</p>
<p>2021 – 2023 · Classe préparatoire PTSI/PT · Lycée de Toulouse</p>
<h2>Projets</h2><p>Robot suiveur de ligne (STM32, C embarqué, asservissement PID) — projet en équipe de 4</p>
<p>Banc de test moteur brushless (Python, LabVIEW, CAN)</p>
<h2>Compétences</h2><p>C, C++, Python, MATLAB/Simulink, STM32, SolidWorks, CAN, Git</p>
<h2>Langues</h2><p>Français (langue maternelle), Anglais (B2), Espagnol (A2)</p></body></html>"""

PROFILE = ("Intitulé : Étudiant ingénieur en mécatronique\nFormation : Cycle ingénieur mécatronique (2e année, Bac+4), École "
           "d'ingénieurs de Toulouse, 2023 – 2026\nCompétences : C, C++, Python, MATLAB/Simulink, STM32, SolidWorks, CAN, Git\n"
           "Langues : Français langue maternelle, Anglais B2, Espagnol A2\nProjet : Robot suiveur de ligne (STM32, équipe de 4)")


def fake_facts(company: str, title: str) -> FactSheet:
    email, phone = "camille.durand.test@example.org", "+33 6 39 98 12 34"
    rows = [
        ("full_name", "Nom complet", "Camille Durand", True), ("first_name", "Prénom", "Camille", True),
        ("last_name", "Nom de famille", "Durand", True), ("email", "Adresse e-mail", email, True),
        ("phone", "Téléphone", phone, True), ("phone_device_type", "Type de téléphone (Mobile ou Fixe)", "Mobile", True),
        ("phone_calling_code", "Indicatif téléphonique", "+33", True),
        ("former_employer", "Emploi antérieur auprès de cet employeur", "Non", True),
        ("relatives_at_employer", "Proche ou membre de la famille employé chez cet employeur", "Non", True),
        ("linkedin", "Profil LinkedIn", "https://www.linkedin.com/in/camille-durand-test", True),
        ("civility", "Civilité", "Madame", False), ("gender", "Genre", "Femme", False), ("birth_date", "Date de naissance", "14/03/2003", False),
        ("nationality", "Nationalité", "Française", False), ("address", "Adresse postale", "12 rue des Lilas", False),
        ("address_line_2", "Complément d’adresse", None, False), ("postal_code", "Code postal", "31000", False),
        ("city", "Ville", "Toulouse", False), ("country", "Pays", "France", False), ("driving_licence", "Permis de conduire", "Permis B", False),
        ("disability", "Reconnaissance de travailleur handicapé (RQTH)", "Non", False), ("birth_place", "Lieu de naissance", "Toulouse", False),
        ("department", "Département de résidence", "Haute-Garonne", False),
        ("needs_visa_sponsorship", "Besoin d'un visa ou d'un permis de travail (sponsoring)", "Non", False),
        ("vehicle", "Véhicule personnel", "Non", False), ("criminal_record", "Casier judiciaire (déclaration)", None, False),
        ("notice_period", "Préavis", "Aucun", False), ("current_salary", "Rémunération actuelle", None, False),
        ("graduation_date", "Date d'obtention du diplôme en cours (prévue) / graduation date", "septembre 2026", False),
        ("politically_exposed", "Personne politiquement exposée", "Non", False),
        ("recruiter_ai", "Assistance du recruteur par une IA", "Oui", False),
        ("conflict_of_interest", "Conflit d’intérêts avec un employé ou client", "Non", False),
        ("internship_agreement", "L'école fournit une convention de stage (internship agreement)", "Oui", False),
        ("work_authorization", "Autorisation de travail / situation administrative en France", "Citoyenne française, aucune autorisation nécessaire", False),
        ("mobility", "Mobilité géographique", "France entière", False), ("languages_summary", "Langues parlées (résumé)", "Français natif, anglais B2", False),
        ("language_1", "Langue : Français", "Français : langue maternelle", False), ("language_2", "Langue : Anglais", "Anglais : B2", False),
        ("school", "École / établissement actuel", "École d'ingénieurs de Toulouse", False),
        ("degree", "Diplôme / formation en cours", "Cycle ingénieur mécatronique", False),
        ("education_level", "Niveau d'études (formation la plus récente)", "Bac+4 (2e année de cycle ingénieur)", False),
        ("current_title", "Intitulé actuel", "Étudiante ingénieure en mécatronique", False),
        ("candidate_profile", "Profil du candidat (CV maître) — sert uniquement à déduire une réponse plausible", PROFILE, False),
        ("today", "Date du jour", "2026-11-20", False), ("contract_sought", "Type de contrat recherché", "Stage", False),
        ("available_from", "Date de début / disponibilité", "1 février 2027", False), ("duration", "Durée", "6 mois", False),
        ("salary_expectation", "Prétentions salariales", "Gratification légale de stage", False),
        ("job_source", "Où avez-vous vu l'offre ? (source réelle de l'annonce)", "Site carrière de l'entreprise", False),
        ("job_title", "Intitulé du poste", title, False), ("company", "Entreprise", company, False),
        ("consent_optional", "Consentement facultatif / ne souhaite pas répondre", "non", False),
        ("account_email", "E-mail du compte candidat", email, True),
    ]
    facts = [Fact(key=key, label=label, value=value, exact=exact, variants=["0639981234", "06 39 98 12 34", "+33639981234"] if key == "phone" else [])
             for key, label, value, exact in rows]
    facts.append(Fact(key="account_password", label="Mot de passe du compte candidat", value="disponible", exact=True, secret=True))
    return FactSheet(facts=facts)


class Recorder:
    """The real planner, with every plan written next to the screenshots."""

    def __init__(self, inner, out: Path):
        self.inner, self.out, self.calls = inner, out, 0

    async def plan(self, snapshot, facts, **kw):
        plan = await self.inner.plan(snapshot, facts, **kw)
        self.calls += 1
        (self.out / f"plan-{self.calls}.json").write_text(json.dumps(
            {"fields": [f.model_dump(exclude_defaults=True) for f in snapshot.fields], "text": snapshot.text[:1500],
             "plan": plan.model_dump(exclude_defaults=True)}, ensure_ascii=False, indent=1), encoding="utf-8")
        return plan


async def main() -> int:
    args = argparse.ArgumentParser()
    args.add_argument("url")
    args.add_argument("--company", default="")
    args.add_argument("--title", default="")
    args.add_argument("--out", default="")
    args.add_argument("--steps", type=int, default=14)
    opts = args.parse_args()
    data_dir = Path(os.environ.get("JOBHUNT_DATA_DIR", ""))
    if not os.environ.get("JOBHUNT_DATA_DIR") or (data_dir / "jobhunt.sqlite3").exists():
        print("Set JOBHUNT_DATA_DIR to a scratch directory (never the real data).", file=sys.stderr)
        return 2
    out = Path(opts.out or tempfile.mkdtemp(prefix="rehearse-", dir=data_dir))
    out.mkdir(parents=True, exist_ok=True)
    os.environ["JOBHUNT_APPLY_LOCAL_SCREENSHOTS"] = "1"
    engine.human_pause = lambda *a, **k: asyncio.sleep(0.05)
    planner = Recorder(LLMPlanner(FailoverLLM(get_settings()), autonomy=True), out)

    async def ok() -> list[str]:
        return []

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(tempfile.mkdtemp(prefix="rehearse-profile-"), channel="chrome", headless=True,
                                                          viewport={"width": 1366, "height": 900}, locale="fr-FR")
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.set_content(CV_HTML)
            pdf = await page.pdf(format="A4")
            cv = out / "CV.pdf"
            cv.write_bytes(pdf)
            audience = Audience(opts.url, [], lambda: None, pdf_bytes=pdf)
            audience.core = audience.trusting = True
            flow = engine._Run(page, url=opts.url, facts=fake_facts(opts.company, opts.title), planner=planner,
                               uploads=engine.Uploads(cv_pdf=cv, cv_bytes=pdf), mode="submit", out_dir=out, job_title=opts.title,
                               company=opts.company, password=lambda url: "Rehearsal-Pw-2026!", before_submit=ok, on_submit_click=None,
                               on_nav_click=None, verifier=None, host_allowed=audience.trusted_host, max_steps=opts.steps, goto=None,
                               account_known=lambda url: False)
            flow.autonomous, flow.rehearse = audience, True
            result = await flow.run()
            with __import__("contextlib").suppress(Exception):
                await flow.page.screenshot(path=str(out / "final.png"), full_page=True)
        finally:
            await ctx.close()
    for step in result.steps:
        print(f"step {step.index} [{step.page_kind}] {step.url[:100]} clicked={step.clicked!r}")
        for filled in step.filled:
            print(f"    + {filled.op:<10} {filled.label[:50]!r} = {filled.value[:40]!r}" + (" (déduit)" if filled.inferred else ""))
        if step.note:
            print(f"    note: {step.note[:700]}")
    print(f"RESULT {result.status}: {result.message}")
    for question in result.questions:
        print(f"    ? [{question.kind}] {question.label[:90]!r} reason={question.reason} options={question.options[:6]}")
    print(f"out: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
