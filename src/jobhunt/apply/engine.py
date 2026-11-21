"""Read-only browser inspection for generic ATS pages.

Load the page without candidate data, take the browser offline, then inspect and
validate a proposed plan without dispatching browser actions. Offline routing is
defence in depth, not a reason to put candidate data in an executable document.
Generic live dispatch remains disabled until an ATS-specific adapter can bind both
the candidate-data operation and its receipt to this attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import os
import random
import re
import secrets
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, quote_plus, urldefrag, urlparse

from patchright.async_api import ElementHandle, Locator, Page
from pydantic import BaseModel

from jobhunt.apply import mail, trusting
from jobhunt.apply.adapters import Attempt
from jobhunt.apply.autonomous import (
    SUPERVISED_REQUIRED, _live_option, blocked_consent, decision as autonomous_decision, eeo_field, race_ethnicity_field,
)
from jobhunt.apply.destination import clean_text, clean_url, contains_verification_code, origin
from jobhunt.apply.errors import ApplyError, ManualField, StepChanged
from jobhunt.apply.consent import dismiss_consent
from jobhunt.apply.answer_check import (AnswerCheckItem, AnswerVerifier, as_verdict, derived_from, fold, is_hard_fact)
from jobhunt.apply.missing_answers import IDENTITY, ambiguous_name, capture, exact_option, fact_key, standing_key
from jobhunt.apply.planner import Planner, facts_payload, inspection_inputs
from jobhunt.apply.page_outline import DIAGNOSED_STATUSES, bare_url, safe_reason, save_failure_diagnostics
from jobhunt.apply.prompt_rules import DIAL_COUNTRY_NAMES, decision_key, device_type, french_phone, international_calling_code, recruiter_ai_polarity, technical_key
from jobhunt.apply.scoped_choices import field_choices
from jobhunt.apply.snapshot import Resolver, apply_demographic_choice, live_button, take_snapshot
from jobhunt.apply.widgets import DENIED, WIDGET, date_segment, date_segment_option, denied_widget
from jobhunt.apply.store import binding_active
from jobhunt.apply.types import (Action, DispatchDiagnostics, Fact, FactSheet, FieldOption, FilledField, FormField, Mode, PageSnapshot, Question, RunResult,
                                 RunStatus, StepLog, StepPlan, ACCOUNT_REASONS)
from jobhunt.apply.unattended import ClickUncertain as _Dispatched, click_once, handoff_result, trial_click
from jobhunt.browser import human_pause
from jobhunt.llm import LLMError

SUBMIT_RE = re.compile(r"postuler|envoyer|soumettre|valider ma candidature|je postule|submit|send|apply|finish|terminer|confirmer", re.I)
NAV_RE = re.compile(r"suivant|next|continuer|continue|[eé]tape|step|save and continue|enregistrer et continuer", re.I)
# A cookie-banner button: short, whole-label match only (so it can never be a form's final button).
COOKIE_RE = re.compile(r"(tout )?(accepter|refuser|rejeter|accept|reject|decline|deny)( tout| all| les cookies| cookies| et fermer)?|non|non merci|"
                       r"continuer sans accepter|fermer|close|necessary only|n[eé]cessaires uniquement|j'accepte|je refuse|ok pour moi", re.I)
ACCOUNT_RE = re.compile(r"inscri|cr[eé]er|connect|login|log in|sign|compte|account|identif", re.I)
MARKETING_RE = re.compile(r"newsletter|marketing|promotion|offres? (commerciales?|partenaires)|partenaires|talent|vivier|"
                          r"communications? commerciales?|actualit[eé]s|recevoir (des|nos)", re.I)
# Positive evidence of a received application: explicit receipt language only (a bare "merci" / "thank you" / "confirmation" is not
# one), run on accent-stripped lowercase text (`fold`, apostrophes normalised). Any failure / negation wording in the visible text
# vetoes it.
CONFIRM_RE = re.compile(
    r"nous avons bien recu|we (?:have )?(?:successfully )?received your application|thank(?:s| you) for applying|"
    r"merci (?:pour|d'avoir) (?:votre candidature|postule)|"
    r"candidature (?:a (?:bien |ete )*|est (?:bien )?|bien )?(?:recue|envoyee|enregistree|transmise|soumise|prise en compte)|"
    # ("Votre candidature pour l'offre 2026-1 STAGE - ... a été envoyée": what it is for stands between the two)
    r"candidature [^.!?\n]{1,200}? (?:a (?:bien )?ete|est (?:bien )?) ?(?:recue|envoyee|enregistree|transmise|soumise|prise en compte)|"
    r"application (?:has been |was |is |had been )?(?:successfully )?(?:received|submitted|sent|recorded)|"
    r"application [^.!?\n]{1,200}? (?:has been|was) (?:successfully )?(?:received|submitted|sent|recorded)|"
    r"successfully (?:submitted|applied|sent)")
NEGATION_RE = re.compile(
    r"n'a pas (?:ete|pu)|pas (?:ete )?(?:envoyee|transmise|enregistree|recue)|not been|has not|was not|were not|could not|couldn't|"
    r"unable|erreur|error|echec|failed|failure|invalide|invalid|veuillez corriger|please correct|not (?:submitted|sent|received)|"
    r"impossible de|n'a pu|n'ont pas")
CONDITIONAL_RE = re.compile(
    r"\b(?:once|when|whenever|after|as soon as|if|until|before|upon|une fois|lorsque|lorsqu'|quand|des que|apres|si|avant|jusqu'a)\b|"
    r"\bwill be\b|\byou will\b|\bsera\b|\bseront\b|\bva etre\b|\bvont etre\b|\bgoing to\b")
REQUEST_RE = re.compile(
    r"\bplease\b|\bkindly\b|\bveuillez\b|\bmerci de\b|\bpriere de\b|\bconfirm(?:ez|er)?\b.*\b(?:your|votre)\b|\bverifie[zr]\b|"
    r"\bcheck (?:that|whether|if)\b|\bmake sure\b|\bassurez|\bensure\b|\bclick\b|\bcliquez\b|\bto (?:submit|send|complete)\b|"
    r"\bpour (?:envoyer|soumettre|finaliser|valider)\b")
DIAL_PROMPT_RE = re.compile(r"\b(?:code (?:du |de )?(?:pays|region)|country code|calling code|dial(?:ing)? code|indicatif)\b")
PHONE_LABEL_RE = re.compile(r"\b(?:telephone|phone|mobile|portable|tel)\b")
DIAL_OPTION_RE = re.compile(r"^\s*\(?\+\d{1,4}")
TEXT_KINDS = {"text", "email", "tel", "url", "number", "date", "password", "textarea", "combobox"}
PROFILE_KINDS = {"text", "email", "tel", "url", "number", "date", "textarea"}  # a pre-filled value there = a form with a profile
GUARDED = {"next", "submit", "signup", "login"}
FILLABLE = {"text", "email", "tel", "textarea", "select", "radio", "checkbox"}
# Unattended sends never guess these: they need a stored answer (« À compléter une fois »), else they become a question.
_UNATTENDED_NO_GUESS = re.compile(
    r"dipl[oô]m|degree|qualification|niveau d.?[ée]tudes|education level|abschluss|"
    r"date de d[ée]but|d[ée]but souhait|disponibilit|start date|starting date|available (from|to start)|availability|"
    r"verf[üu]gbar|eintrittsdatum|"
    r"salaire|r[ée]mun[ée]ration|pr[ée]tentions?|salary|compensation|pay expectation|gehalt", re.I)  # "the form is still there" = a file or >= 3 of these
CV_RE = re.compile(r"(?<![a-z])(cv(?![a-z])|curriculum|resume|lebenslauf)")           # run on accent-stripped lowercase text
LETTER_RE = re.compile(r"lettre|cover|motivation|anschreiben")
# Consents that code accepts by itself (recruitment-data / privacy / site terms), run on accent-stripped lowercase text; any other
# tick ("conditions de l'offre salariale"...) goes to the verifier.
PRIVACY_RE = re.compile(r"confidentialit|donnees (a caractere )?personnel|personal data|privacy|rgpd|gdpr|traitement de (mes|vos|ses) donn|"
                        r"protection des donn|politique de (confidentialit|protection)|conditions generales d.utilisation|"
                        r"terms (of use|of service|and conditions)|\bcgu\b")
# A file input whose label names ANOTHER document is never taken for the CV just because it is the only one of the page.
OTHER_DOC_RE = re.compile(r"diplome|releve|transcript|bulletin|lettre|motivation|cover|portfolio|book|piece|identit|passport|passeport|"
                          r"photo|certificat|attestation|justificatif|recommandation|reference")
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
ACCOUNT_KINDS = ("account_login", "account_signup")
ACCOUNT_FACTS = frozenset({"account_email", "account_password", "email", "first_name", "last_name", "full_name", "phone", "civility"})
ACTION_TIMEOUT_MS = 8000
MAX_SUBMIT_CLICKS = 1  # at most ONE final dispatch per run: a field error is never proof that nothing was sent
IDLE_TIMEOUT_MS = 3000   # network-idle wait of a step (never fatal)
TYPE_DELAY_MS = (15, 45)  # per-keystroke delay range
CLICK_WAIT_S = 0.5       # lets a popup / navigation start after a click
RELEASE_WAIT_S = 0.2     # dry run: pause between "pages sent to about:blank" and removing the request guard
RELEASE_PAGES = True     # dry run: send the pages to about:blank at the end (tests that inspect the final DOM switch it off)
RENDER_WAITS = 4        # a page with no field and almost no text is still rendering (script-built site): re-read it this many times
RENDER_WAIT_S = 2.0
RENDER_TEXT_MIN = 40
CLICK_ATTRIBUTION_S = 4.0  # a navigation aborted within this delay after a click of ours is attributed to that click
AUTO_CONSENT = True      # clear consent dialogs before each page read (tests of the planner's own cookie rules switch it off)
SPLIT_UPLOADS = True     # uploads get a step of their own, then the page is read again (tests with scripted plans switch it off)
UPLOAD_SETTLE_S = 2.5    # lets a CV parser re-render / pre-fill the form after an upload
OBSERVE_S = 12.0         # after a post-data click: how long the engine only observes the page before giving up ("not confirmed")
OBSERVE_POLL_S = 2.0     # re-snapshot interval of that observation
TRUSTING_OBSERVE_S = 30.0  # planner-trusting runs wait longer for the receipt page (slow confirmations, redirects)
FILE_OPENER_RE = re.compile(r"(?:choisir|choose|select|selectionner|parcourir|browse|joindre|telecharger|televerser|upload|importer|deposer|ajouter|attach|add)\b"
                            r".{0,20}\b(?:fichier|file|document|cv|resume|curriculum|piece)|^\s*(?:parcourir|browse)")  # folded label of a button that opens a file dialog
UPLOAD_CONFIRM_RE = re.compile(r"^\s*(?:ajouter|add|attach|joindre|telecharger|televerser|upload|importer|charger|envoyer le fichier|aggiungi|carica|allega|"
                               r"hinzufugen|hochladen|anhangen|anadir|adjuntar|subir|cargar|adicionar|anexar|toevoegen|uploaden)\b")
NEXT_OR_SEND_RE = re.compile(r"postuler|envoyer|soumettre|valider|confirmer|terminer|finish|enregistrer|save|suivant|continuer|next|continue|submit|apply|send|"
                             r"weiter|senden|avanti|siguiente|seguinte|volgende|inviare|enviar|\bok\b")
SIGNUP_LINK_RE = re.compile(r"cre(?:er|ez|e) (?:un |une |mon |ma |votre )?(?:compte|espace|profil)|creation (?:de |d')?(?:un |mon )?(?:compte|espace)|"
                            r"s'?inscrire|inscription|nouveau candidat|pas encore de compte|create (?:an )?(?:account|profile)|sign ?up|"
                            r"register|new user|konto erstellen|registrieren|nouvel utilisateur|premiere visite")
# "Inscription avec Google" and the like create nothing on the employer's site: never taken for the account-creation control.
# (a page that still offers the ways to sign in - "Se connecter avec une adresse e-mail" - is not past the account pages)
SIGN_IN_WITH_RE = re.compile(r"(?:se connecter|connexion|s.identifier|sign[ -]?in|log[ -]?in|anmelden) (?:avec|with|mit)\b")
SOCIAL_RE = re.compile(r"google|linkedin|facebook|apple|microsoft|github|twitter|france ?connect|\bsso\b|\bavec (?:un |une )?autre")
ACCOUNT_EXISTS_RE = re.compile(r"existe deja|deja (?:utilis|inscrit|existant|enregistr|cree|un compte)|already (?:exists?|in use|registered|taken|been (?:used|registered))|"
                               r"is already|deja associe|bereits (?:registriert|vorhanden|verwendet)")


def account_key(address: str) -> str:
    """One candidate account = one host, or one Workday career site: its sign-in, creation and application pages do not
    share a URL, so the address of a Workday account page (a URL) is reduced to its host and career site."""
    from jobhunt.apply.accounts import is_workday_host, workday_site  # noqa: PLC0415

    host = (urlparse(address).hostname or "") if "://" in address else address
    if host and is_workday_host(host):
        site = workday_site(address.split("?", 1)[0].split("#", 1)[0])
        return f"{host}|{site}" if site else address
    return address


class _Accounts(set):
    """Account addresses of one run, compared by `account_key`."""

    def add(self, address):
        super().add(account_key(address))

    def discard(self, address):
        super().discard(account_key(address))

    def __contains__(self, address):
        return super().__contains__(account_key(address))


_SIGNAL_RE = re.compile(r"erreur|error|incorrect|invalide|invalid|non valide|verifi|verify|deja|already|locked|verrouill|bloque|echec|failed|"
                        r"not recognized|non reconnu|unavailable|indisponible|expire")


def page_signals(text: str) -> list[str]:
    """The sentences of a page excerpt that carry an error / account wording (e-mail addresses masked), for the local
    failure record: what the site itself said at a step."""
    out = []
    for part in re.split(r"(?<=[.!?])\s+|\s{2,}|\n", text):
        part = part.strip()
        if 8 <= len(part) <= 400 and _SIGNAL_RE.search(fold(part)):
            out.append(re.sub(r"[\w.+-]+@[\w.-]+", "<e-mail>", part)[:240])
    return out[:6]


def says_account_exists(text: str) -> bool:
    """The page STATES that the e-mail already has an account ("Un compte existe déjà avec cette adresse"). The prompt every
    creation form carries ("Vous avez déjà un compte ? Connexion", "Already registered? Sign in") is a question, not that."""
    for match in ACCOUNT_EXISTS_RE.finditer(text):
        tail = re.split(r"[.!\n]", text[match.end():match.end() + 60], maxsplit=1)[0]
        if "?" not in tail:
            return True
    return False


LOGIN_BUTTON_RE = re.compile(r"se connecter|connexion|sign[ -]?in|log[ -]?in|anmelden|identifier")
CREATION_NAME_RE = re.compile(r"creation|creer|create|nouveau|nouvelle|\bnew|register|signup|sign up|inscri")
EMAIL_FIELD_RE = re.compile(r"e-?mail|courriel|identifiant|login|username|adresse mail")
SIGNUP_CONSENT_RE = re.compile(r"conditions|\bcgu\b|\bcgv\b|terms|politique|privacy|confidentialite|donnees|rgpd|gdpr|traitement|"
                               r"j'?ai lu|lu et|accepte|agree|consent")
ACCEPT_BOX_RE = re.compile(r"^\s*(?:validation|valider|certifie|je certifie|confirmation)\b")  # a lone required "Validation" box
SIGNUP_PRIVACY_RE = re.compile(r"politique|privacy|confidentialite|donnees|rgpd|gdpr|traitement")
AGGREGATOR_RE = re.compile(r"agregat|aggregat|app ?store|google play|application mobile|mobile app|app[- ]only|lien inexploitable|not the employer|"
                          r"liste d.?offres|liste des offres|resultats? de recherche|search results|job list(?:ing)?|offer list|listing page")
# Trusting mode: what a sign-in page shown right after an account-creation click says when the new account still awaits the
# link / code sent by e-mail (folded text).
VERIFY_PENDING_RE = re.compile(
    r"verify your (?:account|e-?mail)|check your (?:e-?mail|inbox)|(?:verification|activation) (?:e-?mail|link|code)|"
    r"e-?mail (?:de )?(?:verification|validation|activation)|lien (?:de |d')(?:verification|validation|activation)|"
    r"(?:verifie[rz]|active[rz]|valide[rz]|confirme[rz]) votre (?:compte|adresse|e-?mail)|(?:activate|confirm) your (?:account|e-?mail)|"
    r"resend (?:account )?verification|renvoyer (?:l'?e-?mail (?:de )?)?(?:verification|activation)|"
    r"konto (?:bestatigen|verifizieren|aktivieren)|bestatigungs-?e-?mail|bestatigen sie ihr")
# Trusting mode: read again a page the planner called a job ad although it is the application form itself.
FORM_HINT = ("[THIS PAGE IS A STEP OF THE APPLICATION FORM ITSELF (its required fields are empty), not the job ad: page_kind "
             "`form`; fill it per the rules and end with the click that moves on.] ")
ERROR_TITLE_RE = re.compile(r"(?:erreur|error|errore|fehler|fout)\b")  # folded page title of a form sent back with errors
CAPTCHA_POLL_S = 2.0     # re-snapshot interval while waiting for a human to solve a CAPTCHA
ACCOUNT_CLICKS_MAX = 3   # login / signup clicks per run before the account step is reported as not accepted
ENTRY_TRIES_CORE = 3     # core guard mode: entry discoveries per run (one per page)
MISSING_OFFER_RE = re.compile(
    r"(?:offre|annonce|poste|job|position|posting)[^.!\n]{0,60}(?:n.?existe (?:pas|plus)|n'est plus (?:disponible|en ligne|active)|"
    r"(?:est|a) (?:expiree?|cloturee|pourvue)|no longer (?:exists?|available)|does not exist|"
    r"(?:has|is) (?:expired|been (?:closed|removed|filled)))")
PRINT_VIEW_RE = re.compile(r"/imprimer-fiche-emploi-")  # a careers site's print view of an ad: no apply button; the normal view is "/emploi-..."
GONE_PATH_RE = re.compile(r"/(?:error-?page|error|erreur|not-?found|page-?not-?found|404|oops|expired|offer-?expired|job-?closed)(?:/|\.\w+)?$")
CHANGED_RETRY_S = 0.8  # one re-read of a decision scope that changed under the action (async option lists, dependent fields)
PRE_STEP_CAP = 8        # pure pre-data steps (cookie banner, open_form, account pages) that do not use the max_steps budget
SECRET_LOG = "•••"
AUTO_SUBMIT_NOTE = "soumission automatique bloquée"
NO_SHOT_NOTE = "capture désactivée après saisie du mot de passe"
TOKEN_RE = re.compile(r"submit_clicked", re.I)   # bookkeeping token of the service: never produced by the engine / planner / page
FIELD_ERROR_MSG = ("Le site signale une erreur de saisie après le clic : vérifier sur le site de l'entreprise si la candidature "
                   "est partie")
UNCONFIRMED_MSG = "Envoi non confirmé : vérifier sur le site de l'entreprise"
UNSUPPORTED_DISPATCH = "Envoi bloqué : aucun adaptateur d'envoi et de reçu vérifié pour ce site."
DRY_UNTESTED = "Simulation non testée : inspection hors ligne, sans saisie, fichier ni envoi de candidature."
# What the live control is, read at click time: its destination (the form it OWNS - `.form`, so an explicit form="id" wins over
# the closest ancestor, which is used only when `.form` is null - or a link's href), whether it is a submit control and whether
# its form already holds data (a text-like value or a selected file; hidden inputs, boxes and radios do not count).
_CTL_JS = r"""(e) => {
  const get = (n, a) => Element.prototype.getAttribute.call(n, a);
  const res = (u) => { try { return new URL(u, e.ownerDocument.baseURI).href; } catch (_) { return ''; } };
  const tag = e.tagName, type = (get(e, 'type') || '').toLowerCase();
  const f = (e.form !== undefined && e.form) || e.closest('form');
  const submit = !!f && ((tag === 'BUTTON' && type !== 'button') || (tag === 'INPUT' && (type === 'submit' || type === 'image')));
  let data = false;
  if (f) {
    const els = Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'elements').get.call(f);  // (not a field named "elements")
    for (const x of els) {
      const t = (get(x, 'type') || '').toLowerCase();
      if (x.tagName === 'TEXTAREA') { if (x.value) data = true; }
      else if (x.tagName === 'INPUT' && !['hidden', 'checkbox', 'radio', 'submit', 'button', 'reset', 'image'].includes(t)) {
        if (t === 'file' ? (x.files && x.files.length > 0) : !!x.value) data = true;
      }
    }
  }
  const out = {inForm: !!f, submit, data, url: '', ok: true};
  const fa = get(e, 'formaction');
  if (fa) { out.url = res(fa); out.ok = out.url !== ''; return out; }
  if (tag === 'A') { out.url = res(get(e, 'href') || ''); return out; }
  if (f) {
    const a = get(f, 'action');
    out.url = a ? res(a) : e.ownerDocument.URL;
    out.ok = out.url !== '';
  }
  return out;
}"""


# Does this document hold candidate data? Any visible text-like value or selected file, site-search boxes excepted. Not tied to a <form>.
_PAGE_DATA_JS = r"""(needles) => {
  const WORDS = (needles && needles.words) || [], DIGITS = (needles && needles.digits) || [];
  const strict = WORDS.length > 0 || DIGITS.length > 0;   // we know the candidate: only HIS data makes a page "his"
  const norm = (v) => v.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const SEARCH = /recherch|search|mots?[- ]cl|keyword/i;
  const TEXT = ['text', 'email', 'tel', 'url', 'number', 'date', 'datetime-local', 'month', 'week', 'time'];
  const vis = (e) => { if (!e.isConnected) return false;
    if (e.checkVisibility && !e.checkVisibility({checkVisibilityCSS: true, visibilityProperty: true})) return false;
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const all = [];
  const walk = (root) => { for (const e of root.querySelectorAll('*')) { all.push(e); if (e.shadowRoot) walk(e.shadowRoot); } };
  walk(document);
  for (const e of all) {
    const tag = e.tagName;
    if (tag !== 'INPUT' && tag !== 'TEXTAREA') continue;
    const t = tag === 'INPUT' ? (e.getAttribute('type') || 'text').toLowerCase() : 'textarea';
    if (t === 'file') { if (e.files && e.files.length) return true; continue; }
    if (t !== 'textarea' && !TEXT.includes(t)) continue;
    if (!e.value || !vis(e)) continue;
    const name = e.getAttribute('name') || '';
    const label = [e.getAttribute('aria-label'), e.getAttribute('placeholder'), name, ...[...(e.labels || [])].map((l) => l.textContent)].join(' ');
    if ((e.getAttribute('role') || '').toLowerCase() === 'searchbox' || SEARCH.test(label) || /^q$/i.test(name)) continue;
    if (!strict) return true;
    const v = norm(e.value), d = e.value.replace(/\D/g, '');
    if (WORDS.some((w) => v.includes(w)) || DIGITS.some((x) => d.includes(x))) return true;
  }
  return false;
}"""
# A visible form-level error banner (role=alert / error-like block with error wording). Conservative: short text, no control inside.
_BANNER_JS = r"""() => {
  const WORDS = /erreur|error|invalide|invalid|obligatoire manquant|please correct|veuillez corriger|[eé]chec|failed|failure|impossible|n'a pas pu|could not|unable|probl[eè]me|problem|r[eé]essay|try again|retry|incorrect|manquant|missing|requis|required/i;
  const STYLED = /error|danger|invalid|alert(?!-success|-info)|failure|echec/i;     // error-styled: ANY text counts
  const WEAK = /message|notice|feedback|status|toast|flash|hint|help/i;               // plain containers: the wording list decides
  const OK = /candidature.{0,40}(envoy|re[cç]u|enregistr|transmis)|application.{0,40}(received|submitted|sent)|merci|thank|cookie|traceur|consent/i;
  const vis = (e) => { if (!e.isConnected) return false;
    if (e.checkVisibility && !e.checkVisibility({checkVisibilityCSS: true, visibilityProperty: true})) return false;
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const SKIP = new Set(['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA', 'LABEL', 'OPTION', 'FORM', 'BODY', 'HTML', 'MAIN']);
  for (const e of document.querySelectorAll('[role=alert],[aria-live=assertive],[class]')) {
    if (SKIP.has(e.tagName) || (e.getAttribute('role') || '') === 'button' || !vis(e) || e.querySelector('input,select,textarea,button')) continue;
    const t = (e.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 400 || OK.test(t)) continue;
    const cls = e.getAttribute('class') || '';
    const alert = e.getAttribute('role') === 'alert' || e.getAttribute('aria-live') === 'assertive';
    if (alert || STYLED.test(cls) || ((WEAK.test(cls)) && WORDS.test(t))) return true;
  }
  return false;
}"""
# Does the clicked control's own form (else its closest form / dialog / main container) hold a pre-selected choice?
_SCOPE_CHOICE_JS = r"""(e) => {
  const f = e.form || e.closest('form');
  let controls;
  if (f) controls = [...Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'elements').get.call(f)];   // incl. form="id" controls elsewhere
  else {
    const scope = e.closest('[role=form],dialog,[role=dialog],main');
    if (!scope) return false;
    controls = [...scope.querySelectorAll('select,input,textarea')];
  }
  const vis = (x) => !x.checkVisibility || x.checkVisibility({checkVisibilityCSS: true, visibilityProperty: true});
  for (const x of controls) {
    const t = (x.getAttribute('type') || '').toLowerCase();
    if (x.tagName === 'SELECT') {
      const o = x.selectedOptions && x.selectedOptions[0];
      if (o && o.value !== '' && !(o.disabled && o.index === 0) && vis(x)) return true;
    } else if (x.tagName === 'TEXTAREA') {
      if (x.value) return true;
    } else if (x.tagName === 'INPUT') {
      if ((t === 'radio' || t === 'checkbox') && x.checked) return true;
      if (t === 'file' && x.files && x.files.length) return true;
      if (!['hidden', 'checkbox', 'radio', 'submit', 'button', 'reset', 'image', 'file', 'search', 'password'].includes(t) && x.value) return true;
    }
  }
  return false;
}"""
# The form the clicked control submits, as seen at click time: its method and its action (resolved, without query / fragment).
_FORM_INFO_JS = r"""(e) => {
  const get = (n, a) => Element.prototype.getAttribute.call(n, a);
  const f = (e.form !== undefined && e.form) || e.closest('form');
  if (!f) return null;
  const m = String(get(e, 'formmethod') || get(f, 'method') || 'get').toLowerCase();
  const a = get(e, 'formaction') || get(f, 'action') || e.ownerDocument.URL;
  let u = '';
  try { const x = new URL(a, e.ownerDocument.baseURI); x.search = ''; x.hash = ''; u = x.href; } catch (_) {}
  return {get: m === 'get', action: u};
}"""
# The live members of a radio group, in their CURRENT order (value, label as the snapshot reads it, checked).
_RADIOS_JS = r"""(els) => els.map((e) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const label = (e.labels && e.labels.length ? clean(e.labels[0].textContent) : '') || (e.closest('label') ? clean(e.closest('label').textContent) : '')
    || clean(e.nextSibling && e.nextSibling.nodeType === 3 ? e.nextSibling.textContent : '')
    || clean((e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => e.getRootNode().getElementById(id)?.textContent || '').join(' '))
    || clean(e.getAttribute('aria-label'));
  return {value: e.value, label, checked: e.checked};
})"""
# the file input a styled upload button / label stands for: its label's control, a data target, or the only file input of a nearby container
_NEAR_FILE_JS = r"""(e) => {
  if (e.control && e.control.type === 'file') return e.control;
  const lab = e.closest('label');
  if (lab && lab.control && lab.control.type === 'file') return lab.control;
  const t = e.getAttribute('data-target') || e.getAttribute('data-for') || e.getAttribute('aria-controls');
  if (t) { const n = document.getElementById(t.replace(/^#/, '')) || (() => { try { return document.querySelector(t); } catch (_) { return null; } })();
           if (n && n.type === 'file') return n; }
  for (let p = e.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) {
    const fs = p.querySelectorAll('input[type=file]');
    if (fs.length === 1) return fs[0];
    if (fs.length > 1) break;
  }
  return null;
}"""
# The button's small container (within 4 ancestors): does it hold a file input with a selected file, and nothing else to fill?
_ATTACH_JS = r"""(e) => {
  for (let p = e.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) {
    const fs = [...p.querySelectorAll('input[type=file]')];
    if (!fs.length) continue;
    const others = [...p.querySelectorAll('input,select,textarea')].filter((x) => x.tagName !== 'INPUT'
      || !['file', 'hidden', 'button', 'submit', 'reset', 'image', 'checkbox', 'radio'].includes((x.getAttribute('type') || 'text').toLowerCase()));
    return {filled: fs.some((x) => x.files && x.files.length > 0), small: !['FORM', 'BODY', 'MAIN', 'HTML'].includes(p.tagName) && others.length === 0};
  }
  return {filled: false, small: false};
}"""
# fallback: the only file input of the control's form (else of its frame) - or the only one that accepts pdf / is named like a CV field
_ANY_FILE_JS = r"""(e) => {
  const pick = (list) => {
    if (list.length === 1) return list[0];
    const good = list.filter((x) => /pdf/i.test(x.getAttribute('accept') || '') || /cv|resume|curriculum|file|document|upload/i.test((x.name || '') + ' ' + (x.id || '')));
    return good.length === 1 ? good[0] : null;
  };
  const f = e.form || e.closest('form');
  if (f) { const r = pick([...f.querySelectorAll('input[type=file]')]); if (r) return r; }
  return pick([...e.ownerDocument.querySelectorAll('input[type=file]')]);
}"""
_QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u00b4": "'"})


_QUOTED_RE = re.compile(r"\"[^\"]*\"|\u201c[^\u201d]*\u201d|\u00ab[^\u00bb]*\u00bb|`[^`]*`|(?<!\w)'[^']*'(?!\w)")
MODAL_RE = re.compile(r"\b(?:should|must|will|doit|devrait|devriez|va|vont|reads?|indique(?:ra)?|dira|say)\b|s'affichera|you should see|vous devriez voir")


def _receipt_sentence(sent: str) -> bool:
    """One (folded, quote-blanked) sentence asserting, as done, that the application was received. A conditional / future / modal /
    reporting marker vetoes it only when it comes BEFORE the receipt phrase (it introduces it: "once ...", "you should see ...");
    a marker that follows it ("... received your application and we will review it") does not. A request, an instruction or a
    question vetoes the whole sentence."""
    if sent.rstrip().endswith("?") or REQUEST_RE.search(sent):
        return False
    return any(not (CONDITIONAL_RE.search(sent[:m.start()]) or MODAL_RE.search(sent[:m.start()])) for m in CONFIRM_RE.finditer(sent))


def _blank_directional(t: str) -> str:
    """An opening directional single quote is never an apostrophe: blank \u2018 ... \u2019 (the closer not followed by a letter) and an
    unclosed \u2018 up to the end of its paragraph. A \u2019 between letters stays an apostrophe."""
    t = re.sub(r"\u2018.*?\u2019(?!\w)", lambda m: " " * len(m.group()), t, flags=re.S)
    while True:
        i = t.find("\u2018")
        if i < 0:
            return t
        end = t.find("\n\n", i)
        end = len(t) if end < 0 else end
        t = t[:i] + " " * (end - i) + t[end:]


def _blank_quotes(t: str) -> str:
    """Blank out every quotation of the FULL text (all quote styles, punctuation inside included); an unbalanced opening quote blanks
    the rest of its paragraph."""
    t = _QUOTED_RE.sub(lambda m: " " * len(m.group()), t)
    while True:
        m = re.search(r"[\"\u201c\u00ab`]", t)
        if m is None:
            return t
        end = t.find("\n\n", m.start())
        end = len(t) if end < 0 else end
        t = t[:m.start()] + " " * (end - m.start()) + t[end:]


def _announces_reporting(sent: str) -> bool:
    """A sentence / line that ends with ':' or with reporting / modal wording introduces the NEXT one ("Le message indique :")."""
    body = sent.rstrip(" \n.!?;")
    return body.endswith(":") or MODAL_RE.search(" ".join(body.split()[-2:])) is not None


def confirmation_evidence(text: str) -> bool:
    """Does this visible text say that the application was received? (accent / case insensitive)"""
    t = _blank_directional(fold(text)).translate(_QUOTES)
    if NEGATION_RE.search(t):
        return False
    # an affirmative, COMPLETED assertion: not a quotation, not announced by reporting wording, not conditional / future
    carried = False
    for sent in re.findall(r"[^.!?;\n]+[.!?;\n]*", _blank_quotes(t)):
        if not carried and _receipt_sentence(sent):
            return True
        carried = _announces_reporting(sent)
    return False


@dataclass
class Letter:
    text: str
    pdf_path: Path | None = None


@dataclass
class Uploads:
    cv_pdf: Path
    letter: Callable[[], Awaitable[Letter | None]] | None = None
    cv_bytes: bytes | None = None


@dataclass
class _Item:
    act: Action
    field: FormField
    fact: Fact | None = None
    value: str | None = None          # text to type / option label wanted (None for secrets: resolved at execution time)
    option_index: int = -1            # select/radio: index in field.options
    used: str = ""                    # fact actually used ("cv" / "letter" for uploads), shown in the step log
    llm: bool = False                 # needs an independent verdict before it may be executed
    answer: str = ""                  # what is about to be chosen / ticked / uploaded (for the verifier)
    frame_url: str = ""               # URL of the frame holding the field (password host, https check)
    backed: bool = False              # code alone can vouch for it (verbatim fact, strict date, option equal to the fact)
    hard: bool = False                # the label asks for a hard fact: only a `supported` answer citing a fact is accepted
    inferred: bool = False            # final: deduced (verdict `plausible`), logged as such
    basis: str = ""
    dial_code: str = ""               # combobox calling-code picker: the code ("+33") of the candidate's own phone to pick
    national: bool = False            # phone number typed next to a calling-code picker: national format, without the "+CC"


@dataclass
class _Sent:
    """A click that may have sent the application: what to look for (and not to find) on the page that follows."""
    final: bool
    fields: set[tuple[str, str]]      # (normalized label, kind) of what the engine filled / uploaded on the page of the click
    button: str                       # normalized label of the clicked button


@dataclass
class _Validated:
    items: list[_Item] = dc_field(default_factory=list)
    click: tuple[FormField, str] | None = None
    questions: list[Question] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)
    upload_failed: str = ""   # label of the question when an upload of the step was dropped / failed (no click in that step)
    upload_refs: set[str] = dc_field(default_factory=set)


class _Dirty(Exception):
    """An action left the control in a state that may not be the validated one and cannot be undone (a radio): the step stops."""


class _Skip(Exception):
    """An action that cannot be carried out (short reason, never page content)."""


class _DecisionHold(Exception):
    def __init__(self, result: RunResult):
        self.result = result


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold(s)).strip()


def _label(f: FormField) -> str:
    return f.label or f.name or f.placeholder or f.kind


def _question(f: FormField) -> Question:
    return capture(f, _label(f))


def _match_option(f: FormField, want: str | None) -> int:
    """Index of the option whose label (or value) equals `want`, case/accent-insensitive; -1 if none."""
    w = _norm(want or "")
    if not w:
        return -1
    for i, o in enumerate(f.options):
        if _norm(o.label) == w:
            return i
    for i, o in enumerate(f.options):
        if _norm(o.value) == w:
            return i
    return -1


def _is_empty(f: FormField) -> bool:
    if f.kind in {"radio", "select"} and f.choice_options:
        return not f.selected
    return f.value in ("", "false") if f.kind == "checkbox" else not f.value.strip()


_ALIASES = ({"m", "mr", "monsieur"}, {"mme", "madame", "mrs", "ms"}, {"mlle", "mademoiselle", "miss"},   # civility
            {"oui", "yes", "o", "y"}, {"non", "no", "n"})                                              # yes / no


def option_matches_fact(label: str, value: str) -> bool:
    """Is this option label the fact value itself? ONLY normalized equality or an explicit alias set (civility, yes / no): no prefix,
    no substring, no containment ("Yes, with sponsorship" is not "Yes")."""
    nl, nv = _norm(label), _norm(value)
    return bool(nl) and bool(nv) and (nl == nv or any(nl in g and nv in g for g in _ALIASES))


def _unlabeled(f: FormField) -> bool:
    """A box / radio group / select whose label is empty or just its own kind ("checkbox"): nobody can tell what it asks."""
    return not f.label.strip() or _norm(f.label) == _norm(f.kind)


CHOICE_KINDS = {"checkbox", "radio", "select", "combobox"}


def _site_consent(f: FormField) -> bool:
    return ("consent" in {decision_key(f.label), decision_key(f.group)}
            or bool(PRIVACY_RE.search(fold(f"{f.label} {f.group} {f.name}").translate(_QUOTES))))


def _choice_shape(f: FormField) -> tuple:
    typeahead = f.kind == "combobox" and bool(DIAL_PROMPT_RE.search(fold(f"{f.placeholder} {f.name}")))
    # (a calling-code typeahead renders / filters its list only while open: the list is not part of its identity)
    return (f.form, f.kind, f.name, f.label, f.wording, f.group, f.required, f.multiple, f.autocomplete,
            f.destination, f.choice_destination, [] if typeahead else f.choice_options, [] if typeahead else f.options)


def _choice_state(f: FormField) -> tuple:
    return (_choice_shape(f), f.selected, f.value)


def _secure_url(url: str, fallback: str = "") -> bool:
    """https, or http on loopback (tests). A blank / srcdoc frame inherits the page that holds it."""
    if url.startswith("about:") and fallback:
        url = fallback
    u = urlparse(url)
    return u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in LOOPBACK)


def _dial_selector(f: FormField, snap: PageSnapshot) -> bool:
    """Recognize a calling-code prompt, without assuming the current selection is correct."""
    if fact_key(_question(f).model_copy(update={"required": True})) == "phone_calling_code":  # (optional or not, it is the same prompt)
        return True
    if f.kind == "combobox" and DIAL_PROMPT_RE.search(fold(f"{f.placeholder} {f.name}")):  # a picker of its own, next to the number
        return any(x is not f and x.kind in ("tel", "text") and x.form == f.form and PHONE_LABEL_RE.search(fold(x.label))
                   for x in snap.fields)
    if f.kind == "select" and f.options and 2 * sum(bool(DIAL_OPTION_RE.match(o.label)) for o in f.options) >= len(f.options):
        return any(x.kind == "tel" and x.group == f.group for x in snap.fields)
    return False


def pick_variant(fact: Fact, f: FormField, maxlength: int | None = None, pattern: str | None = None, *, national: bool = False) -> str:
    """Exact fact value, or the variant that best fits the field (placeholder shape, maxlength, pattern). Default: the value."""
    cands = [fact.value or "", *fact.variants]
    if national:  # the country code is chosen in its own picker: type the number without it
        cands = [c for c in cands if not c.lstrip().startswith(("+", "00"))] or cands
    if maxlength:
        cands = [c for c in cands if len(c) <= maxlength] or cands
    if pattern:
        def ok(c: str) -> bool:
            try:
                return re.fullmatch(pattern, c) is not None
            except re.error:
                return True
        cands = [c for c in cands if ok(c)] or cands
    ph = f.placeholder.strip()

    def score(c: str) -> int:
        if not ph:
            return 0
        shape = lambda s: re.sub(r"\d", "9", s)  # noqa: E731
        return 2 * (shape(c) == shape(ph)) + (c[:3] == ph[:3]) + (c[:1] == ph[:1])

    best = max(cands, key=score)
    return best if score(best) > score(cands[0]) else cands[0]


class _Run:
    def __init__(self, page: Page, *, url: str, facts: FactSheet, planner: Planner, uploads: Uploads, mode: Mode, out_dir: Path,
                 job_title: str, company: str, password: Callable[[str], str] | None,
                 before_submit: Callable[[], Awaitable[list[str]]] | None,
                 on_submit_click: Callable[[], Awaitable[None]] | None, on_nav_click: Callable[[], Awaitable[None]] | None,
                 verifier: AnswerVerifier | None, host_allowed: Callable[[str], bool] | None, max_steps: int, goto,
                 on_progress: Callable[[RunResult], Awaitable[None]] | None = None, captcha_wait_s: float = 0.0,
                 account_known: Callable[[str], bool] | None = None, choices: dict[str, int] | None = None):
        self.page, self.url, self.facts, self.planner, self.uploads, self.mode = page, url, facts, planner, uploads, mode
        self.out_dir, self.job_title, self.company, self.password = out_dir, job_title, company, password
        self.before_submit, self.on_submit_click, self.on_nav_click = before_submit, on_submit_click, on_nav_click
        self.diagnostics = DispatchDiagnostics()
        self.before_field: Callable[[str], None] | None = None
        self.current_step = False
        self.generic_step = False
        self.autonomous = None
        self.apply_navigation = None
        self.apply_entry = None
        self.discovering_apply_entry = False
        self.entry_tried: set[tuple[str, str]] = set()  # core guard mode: (page, kind the planner gave it) where the entry discovery ran (capped)
        self.entry_tries = 0
        self.apply_link_followed = False
        self.inspection_offline = False
        self.consent_codes = set()
        self.live_mutation_check: Callable[[ElementHandle | Locator | None], Awaitable[None]] | None = None
        self.attended_accounts = False
        self.supervised_widgets = False
        self.verifier, self.host_allowed = verifier, host_allowed
        self.max_steps, self.goto = max_steps, goto
        self.on_progress, self.captcha_wait_s = on_progress, captcha_wait_s
        self.account_known = account_known   # host -> does the credential store hold an account for it (None: not told to the planner)
        self.choices = choices or {}
        self.choice_fields: dict[str, tuple[Question, str, list[int]]] = {}
        self.questions_by_ref: dict[str, Question] = {}
        self.login_box: set[str] = set()     # refs of the login box that shares its <form> with an inline account-creation form
        self.signup_scope: set[str] | None = None   # refs of the account-creation form when it shares its <form> with the login box
        self.signup_link_clicks = 0          # signup LINK clicks of this run (a link that only reloads the login page must not loop)
        self.login_unknown_try = False       # a login was attempted on a host without a known account
        self.known_login_try = False         # a login was attempted with the stored credential of a host believed to hold the account
        self.account_clicks = 0  # login / signup clicks made on candidate-account pages (a site that keeps refusing is not retried forever)
        self.account_try: tuple[str, str] | None = None  # (login | signup, host) of the account click whose outcome the next page tells
        self.account_exists_hosts: set[str] = _Accounts()      # hosts whose signup answered "account already exists": log in with the stored credential
        self.login_failed: set[str] = _Accounts()  # hosts where that login led back to a password page: the account is not confirmed there
        self.uploaded = False                # a file was set by the engine in this run
        self.uploaded_files: list[tuple[str, str, str]] = []  # (host, normalized label, file name stem in lowercase) of every upload done
        self.attach_done = False             # an upload-confirm click was made
        self.uploaded_cv_names: dict[str | None, set[str]] = {}
        self.consumed = 0                    # steps used against max_steps (pure pre-data steps are free within PRE_STEP_CAP)
        self.pre_steps = 0
        self.attach_pending = False          # an "Ajouter"-style upload-confirm button was just clicked
        self.last_click_rec: tuple[str, str] | None = None  # (url, normalized label) of the last unguarded click made
        self.page_data = False      # the live page holds candidate data in the form of the clicked control (saved draft...)
        self.nav_ref: tuple[str, set[tuple[str, str]], str] | None = None  # (url, fillable labels, button) of the recorded nav click
        self.steps: list[StepLog] = []
        self.touched = False        # candidate data was entered / chosen / uploaded in this run (any page, any kind)
        self.app_touched = False    # same, on an application page (not an account page)
        self.submit_clicks = 0      # final clicks performed
        self.final_clicked = False  # a click that MAY have sent the application was (about to be) performed
        self.nav_pending = False    # a recorded post-data navigation click whose outcome no snapshot has shown yet
        self.final_awaiting = False  # a final click was dispatched and its receipt (or rejection) is not known yet
        self.gated_step = 0         # step whose before_submit() already ran
        self.pending: _Sent | None = None   # the click just made, evaluated on the next snapshot
        self.last_sent: _Sent | None = None
        self.opened: list[Page] = []
        self.all_pages: list[Page] = [page]
        self.click_window = 0.0   # monotonic time until which an aborted navigation is attributed to our last click
        self.click_form: dict[str, Any] | None = None  # dry run: method / action of the form our last click belongs to
        self.widget_blocked = 0   # dry run: non-GET widget navigations aborted while a page was loading (harmless)
        self.render_waited = False   # an empty page was given time to render (once per run)
        self.blocked_requests: list[str] = []   # dry run: page-initiated navigations aborted by the request guard
        self.secrets: set[str] = set()
        self.email_verification = None
        self.email_attempted = False
        self.upload_split: set[str] = set()  # page URLs where the uploads were already done in a step of their own
        self.combo_options: dict[str, list[str]] = {}  # trusting mode: options an opened list showed, by field label
        self.code_entries = 0                # trusting mode: one-time codes read from the mailbox and typed
        self.back_to_posting = False         # trusting mode: returned to the posting after a login that led elsewhere
        self.email_login_tried = False       # trusting mode: at most one sign-in fallback from email verification
        self.rejected_submits = 0            # trusting mode: final clicks the page's own validation refused
        self.sent_url = ""                   # page URL of the last final click
        self.sent_empty: set[tuple[str, str]] = set()  # trusting mode: required fields left empty at the last guarded click
        self.dismissed: list[str] = []       # trusting mode: labels of the privacy / cookie notices closed by code
        self.print_view_left = False         # trusting mode: left a print view of the ad for the normal one, once
        self.stuck_fields: list[str] = []    # trusting mode: fields the program could not set on the last step, which had no click
        self.replans: dict[str, int] = {}    # trusting mode: re-reads of a page made instead of a click (per page URL)
        self.presend: dict[str, int] = {}  # trusting mode: re-reads made before a final click, per page
        self.signed_up: set[str] = _Accounts()     # account addresses where an account-creation click was made in this run
        self.form_hints: dict[str, int] = {}  # trusting mode: page URLs read again as the application form (twice each at most)
        self.plan_trace: list[dict] = []     # trusting mode: what the planner saw / answered at the latest steps (no value)
        self.no_shot_noted = False
        self.final_url = ""
        self._letter: Letter | None = None
        self._letter_done = False

    @property
    def trusting(self) -> bool:
        """Autonomous run in guard mode `core`: the planner's mapping is executed as given (see `trusting`)."""
        return bool(getattr(getattr(self, "autonomous", None), "trusting", False))

    # ------------------------------------------------------------ secrets / redaction
    def _r(self, s: str) -> str:
        """Mask the secrets and strip the service's bookkeeping token."""
        for sec in sorted(self.secrets, key=len, reverse=True):
            # as typed, URL-encoded (query string), half-decoded by a script ("+" for a space), HTML-escaped
            for form in {sec, quote(sec, safe=""), quote_plus(sec), quote(sec), sec.replace(" ", "+"), html.escape(sec), html.escape(sec, quote=False)}:
                s = re.sub(re.escape(form), SECRET_LOG, s, flags=re.I)
        return clean_text(TOKEN_RE.sub("", s))

    def _scrub(self, obj: Any) -> None:
        """Mask the secrets (and the token) in every string of a pydantic model / list, in place."""
        if isinstance(obj, list):
            for i, x in enumerate(obj):
                if isinstance(x, str):
                    obj[i] = self._r(x)
                else:
                    self._scrub(x)
        elif isinstance(obj, BaseModel):
            for name in type(obj).model_fields:
                v = getattr(obj, name)
                if isinstance(v, str):
                    setattr(obj, name, self._r(v))
                elif isinstance(v, (list, BaseModel)):
                    self._scrub(v)

    def _public(self, snap: PageSnapshot) -> tuple[PageSnapshot, bool]:
        """The snapshot as the planner may see it (secrets masked) and whether anything had to be masked."""
        pub = snap.model_copy(deep=True)
        self._scrub(pub)
        pub.url = self._r(clean_url(snap.url))
        masked = bool(self.secrets) and pub.model_dump_json() != snap.model_dump_json()
        for field in pub.fields:
            if technical_key(field.label, field.kind, group=field.group, autocomplete=field.autocomplete) == "credentials":
                field.value = SECRET_LOG if field.value else ""
            if race_ethnicity_field(field, fold):
                field.value = ""
            field.destination = field.choice_destination = None
            field.choice_options = []
        return pub, masked

    def _frame_url(self, resolve: Resolver, ref: str) -> str:
        fn = getattr(resolve, "frame_url", None)
        u = fn(ref) if fn else self.page.url
        return self.page.url if u.startswith("about:") else u

    def _secret(self, frame_url: str) -> str | None:
        """Password for the host of the FRAME holding the field; only on https (or loopback) frames."""
        if self.password is None or not _secure_url(frame_url, self.page.url):
            return None
        try:
            return self.password(self._account_address(frame_url)) or None
        except Exception:
            return None

    # ------------------------------------------------------------ helpers
    def _end(self, status: RunStatus, message: str, questions: list[Question] | None = None) -> RunResult:
        bound = []
        for question in questions or []:
            if question.reason is None and question.ref in self.questions_by_ref:
                question = self.questions_by_ref[question.ref]
            entry = self.choice_fields.get(question.ref)
            if entry and entry[0] == question:
                _, binding, indices = entry
                question = question.model_copy(update={"choice_binding": binding, "choice_indices": indices})
            if question.reason is None:
                question = _question(FormField(ref=question.ref, kind=question.kind, label=question.label))
            bound.append(question)
        trace = self.diagnostics.model_copy()
        if status in ("needs_email", "needs_human") and trace.receipt_reason == "before_submit":
            trace = handoff_result("email" if status == "needs_email" else "human", diagnostics=trace).dispatch_diagnostics
        return RunResult(status=status, message=self._r(message), questions=bound, consent_codes=sorted(self.consent_codes),
                         dispatch_diagnostics=trace)

    async def _shot(self, log: StepLog) -> None:
        if log.screenshot or os.getenv("JOBHUNT_APPLY_LOCAL_SCREENSHOTS") != "1":
            return
        if self.secrets:  # a page may display a typed password in ways the snapshot cannot see (CSS content, canvas...)
            if not self.no_shot_noted:
                self.no_shot_noted = True
                log.note = (log.note + "; " if log.note else "") + NO_SHOT_NOTE
            return
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = (self.out_dir / f"step-{log.index}.png").resolve()
            await self.page.screenshot(path=str(path), full_page=True)
            log.screenshot = str(path)
        except Exception:
            log.note = (log.note + "; " if log.note else "") + "capture d'écran impossible"

    def _host(self) -> str:
        return urlparse(self.page.url).hostname or ""

    @staticmethod
    def _account_address(url: str) -> str:
        from jobhunt.apply.accounts import is_workday_host
        host = urlparse(url).hostname or ""
        return url if is_workday_host(host) else host

    def _bad_host(self, url: str) -> str:
        """The host (or scheme) of `url` when `host_allowed` refuses it; "" when fine. Loopback always passes."""
        if self.host_allowed is None:
            return ""
        u = urlparse(url or "")
        if u.scheme == "javascript":
            return ""
        host = (u.hostname or "").lower()
        if u.scheme in ("http", "https"):
            if host in LOOPBACK:
                return ""
            try:
                ok = bool(self.host_allowed(host))
            except Exception:
                ok = False
            return "" if ok else host
        return (u.scheme or "?") + ":"

    async def _settle(self) -> None:
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
        await human_pause(0.3, 0.8)

    async def _snapshot(self) -> tuple[PageSnapshot, Resolver]:
        # A consent dialog that came back on this page would cover what the run needs. Never in supervised mode (a person
        # drives), and never after data entry in a dry run (a rehearsal clicks nothing once the form is touched).
        if AUTO_CONSENT and self.mode != "supervised" and not (self.mode == "dry_run" and (self.touched or self.app_touched)):
            await dismiss_consent(self.page)
            if self.trusting and not self.final_awaiting:
                with contextlib.suppress(Exception):
                    if closed := await trusting.dismiss_blocker(self):
                        self.dismissed.append(closed)
        for attempt in range(3):
            try:
                snapshot, resolve = await take_snapshot(self.page)
                if self.trusting and self.combo_options:
                    for field in snapshot.fields:  # a list that only renders once opened: show what it offered last time
                        if field.kind == "combobox" and not field.options and (seen := self.combo_options.get(trusting.norm(field.label))):
                            field.options = [FieldOption(value=label, label=label) for label in seen]
                choices = {}
                repeated = set()
                self.questions_by_ref = {}
                for field in snapshot.fields:
                    question = _question(field)
                    self.questions_by_ref[field.ref] = question
                    if (question.reason == "technical"
                            or not field.form.startswith("0:") or _dial_selector(field, snapshot) or _site_consent(field)):
                        continue
                    binding = field_choices(self.page.url, field)
                    if binding:
                        if any(entry[1] == binding[0] for entry in choices.values()):
                            repeated.add(binding[0])
                        choices[field.ref] = question, *binding
                self.choice_fields = {ref: entry for ref, entry in choices.items() if entry[1] not in repeated}
                return snapshot, resolve
            except Exception:  # navigation in flight
                if attempt == 2:
                    raise
                await self._settle()
        raise RuntimeError("unreachable")

    async def _letter_obj(self) -> Letter | None:
        if not self._letter_done:
            self._letter_done = True
            self._letter = await self.uploads.letter() if self.uploads.letter else None
        return self._letter

    async def _el(self, resolve: Resolver, ref: str) -> ElementHandle | Locator | None:
        """The exact element tagged `ref` by the snapshot (a Locator only for resolvers that cannot give the element)."""
        fn = getattr(resolve, "element", None)
        return await fn(ref) if fn else resolve(ref)

    @staticmethod
    async def _ctl(el: ElementHandle | Locator) -> dict[str, Any] | None:
        """What the live control is / sends to (see _CTL_JS); None when it cannot be read."""
        try:
            d = await el.evaluate(_CTL_JS)
        except Exception:
            return None
        return d if isinstance(d, dict) else None

    def _planner_facts(self) -> FactSheet:
        """The facts the planner sees: ours plus the derived `account_exists` of the CURRENT page host (when we can tell)."""
        facts = [fact for fact in self.facts.facts if fact.key != "race_ethnicity"]
        if self.trusting:
            facts.append(Fact(key="candidate_authorizations", label=trusting.AUTHORIZATIONS_LABEL, value=trusting.AUTHORIZATIONS))
        if self.account_known is None:
            return FactSheet(facts=facts)
        try:
            known = self._account_exists()
        except Exception:
            return FactSheet(facts=facts)
        extra = Fact(key="account_exists", label="Un compte existe déjà sur ce site pour le candidat", value="oui" if known else "non")
        return FactSheet(facts=[*facts, extra])

    def _account_exists(self) -> bool:
        """Does the candidate have an account on the CURRENT page host: the store says so and no login with it failed in this run."""
        address = self._account_address(self.page.url)
        if address in self.account_exists_hosts and address not in self.login_failed:
            return True
        return bool(self.account_known and self.account_known(address) and address not in self.login_failed)

    async def _says_account_exists(self) -> bool:
        """The page (any frame) says the e-mail / identifier is already registered."""
        for fr in self.page.frames:
            with contextlib.suppress(Exception):
                text = await fr.evaluate("() => (document.body && document.body.innerText || '').slice(0, 20000)")
                if isinstance(text, str) and says_account_exists(fold(text)):
                    return True
        return False

    def _confirm_account(self, address: str) -> None:
        with contextlib.suppress(Exception):
            if confirm := getattr(self.account_known, "confirm", None):
                confirm(address)

    def _autonomous_decision(self, field: FormField):
        fact = self.facts.get("race_ethnicity")
        return autonomous_decision(field, fold, race_ethnicity=fact.value if fact else None)

    def _standing_item(self, field: FormField) -> _Item | None:
        question = _question(field)
        key = question.fact_id
        fact = self.facts.get(key)
        if fact is None or not fact.value or field.multiple or key in IDENTITY | {"work_authorization", "needs_visa_sponsorship"}:
            return None
        if (self.autonomous or self.mode == "supervised") and field.kind in {"checkbox", "radio"} and not (
                field.form.startswith("0:") and field.destination):
            return None
        if field.kind == "checkbox":
            polarity = recruiter_ai_polarity(field.label)
            if key != "recruiter_ai" or polarity is None or fact.value not in {"Oui", "Non"}:
                return None
            op = "check" if (fact.value == "Oui") == polarity else "uncheck"
            return _Item(Action(ref=field.ref, op=op), field, used=key)
        if field.kind in CHOICE_KINDS:
            index = exact_option(key, fact.value, [option.label for option in field.options], field.label)
            if index is None or not field.options[index].value or not _live_option(field, index):
                return None
            return _Item(Action(ref=field.ref, op="select"), field, option_index=index, used=key)
        if not _is_empty(field) or field.kind not in {"text", "textarea", "number", "date", "email", "tel"}:
            return None
        value = fact.value
        if field.kind == "date":
            from jobhunt.apply.facts import from_french_date

            value = from_french_date(value)
            if not value:
                return None
        return _Item(Action(ref=field.ref, op="fill", fact=key), field, fact, value=value, used=key)

    def _identity_item(self, field: FormField, snap: PageSnapshot, planned: Action | None = None) -> _Item | None:
        """A name / e-mail / phone field the candidate's own facts answer: filled by code, whatever the planner proposed (a
        low-effort plan often leaves "Votre nom", "Enter email address" or "Votre prénom" alone). Only the exact wording of the
        label counts (third parties - referee, company, school... - are refused by the fact's `label_re`)."""
        if field.kind not in {"text", "email", "tel"} or not _is_empty(field) or not field.label.strip():
            return None
        key = standing_key(field.label)
        if key not in IDENTITY:
            return None
        if ambiguous_name(field.label):  # "Votre nom": the surname beside a first-name field, the full name when it stands alone
            key = "last_name" if self._has_first_name(snap) else "full_name"
        if (key == "email" and field.kind == "tel") or (key == "phone" and field.kind == "email"):
            return None
        if planned is not None and planned.fact == key:  # the plan already cites this very fact: its (formatted) answer stands
            return None
        fact = self.facts.get(key)
        if fact is None or not fact.value or not self.facts.known(key) or fact.secret:
            return None
        if not (self._label_matches(f=field, fact=fact) or (ambiguous_name(field.label) and key in {"last_name", "full_name"})):
            return None
        return _Item(Action(ref=field.ref, op="fill", fact=key), field, fact, value=fact.value, used=key)

    @staticmethod
    def _has_first_name(snap: PageSnapshot) -> bool:
        return any(f.kind == "text" and standing_key(f.label) == "first_name" for f in snap.fields)

    def _identity_needles(self) -> dict[str, list[str]]:
        """What identifies the candidate's own data in a field value: his names, e-mail addresses and phone digits. A page
        "holds candidate data" when a visible field contains one of them (a saved draft, a logged-in profile) — not when a
        filter box on an ad page happens to say "France" (that made the engine take "Postuler" for a possible send)."""
        words: set[str] = set()
        digits: set[str] = set()
        for key in ("first_name", "last_name", "full_name", "email", "account_email", "phone"):
            fact = self.facts.get(key)
            if fact is None or not fact.value:
                continue
            for v in [fact.value, *fact.variants]:
                if key == "phone":
                    d = re.sub(r"\D", "", v)
                    if len(d) >= 9:
                        digits.add(d[-9:])
                else:
                    w = fold(v).strip()
                    if len(w) >= 3:
                        words.add(w)
        return {"words": sorted(words), "digits": sorted(digits)}

    async def _holds_data(self, el: ElementHandle | Locator | None) -> bool:
        """LIVE read: the document of the clicked control, or the top page, holds candidate data. Unreadable = True (fail closed)."""
        frames = [self.page.main_frame]
        try:
            fr = await el.owner_frame() if el is not None and hasattr(el, "owner_frame") else None
            if fr is not None and fr is not self.page.main_frame:
                frames.append(fr)
            return any([await f.evaluate(_PAGE_DATA_JS, self._identity_needles()) for f in frames])
        except Exception:
            return True

    @staticmethod
    def _combined_form(kind: str, snap: PageSnapshot) -> bool:
        """A one-page form that holds the account fields AND the application questions (SuccessFactors "Please sign in" +
        profile + questionnaire): the planner called it a form, so it is filled as the application, not as an account box."""
        if kind != "form":
            return False
        account = re.compile(r"e-?mail|mot de passe|password|passwort|prenom|first name|^nom\b|last name|surname|identifiant", re.I)
        others = [f for f in snap.fields if f.kind in {"text", "textarea", "select", "combobox", "radiogroup", "tel", "number", "date"}
                  and not account.search(fold(f.label))]
        return len(others) >= 5

    def _error_page_gone(self, kind: str, snap: PageSnapshot) -> bool:
        """The site answered the posting with its own error / not-found page (no form on it): the ad is gone."""
        return bool(self.autonomous and kind in ("error", "other", "job_page") and not self.app_touched
                    and GONE_PATH_RE.search(urlparse(snap.url).path.lower())
                    and not any(f.kind not in {"button", "link"} for f in snap.fields))

    def _gone_result(self) -> RunResult:
        result = self._end("blocked", "Offre clôturée")
        result.diagnostic_code = "job_closed"
        return result

    def _offer_gone(self, snap: PageSnapshot) -> bool:
        """Before any application data: the page itself says the posting no longer exists (and has no file field to fill)."""
        return bool(self.autonomous and not self.app_touched
                    and not any(f.kind == "file" for f in snap.fields) and MISSING_OFFER_RE.search(fold(snap.text)))

    async def _page_says_gone(self) -> bool:
        """The whole page (any frame, disabled controls included) says the posting no longer exists."""
        for fr in self.page.frames:
            with contextlib.suppress(Exception):
                text = await fr.evaluate("() => (document.body && document.body.innerText || '').slice(0, 60000)")
                if isinstance(text, str) and MISSING_OFFER_RE.search(fold(text)):
                    return True
        return False

    async def _banner_error(self) -> bool:
        """A form-level error banner is visible somewhere on the page (any frame)."""
        for fr in self.page.frames:
            with contextlib.suppress(Exception):
                if await fr.evaluate(_BANNER_JS):
                    return True
        return False

    async def _holds_choice(self, el: ElementHandle | Locator) -> bool:
        """dry_run: a pre-selected / checked control in the SAME form (or container) as the clicked control. Unreadable = True."""
        try:
            return bool(await el.evaluate(_SCOPE_CHOICE_JS))
        except Exception:
            return True

    async def _dest_result(self, resolve: Resolver, refs: list[str], click: FormField | None) -> RunResult | None:
        """Destination binding: page host, frame hosts of the fields / button, and the form action of the button."""
        urls = [self.page.url, *[self._frame_url(resolve, r) for r in refs]]
        action = ""
        if click is not None:
            urls.append(self._frame_url(resolve, click.ref))
            el = await self._el(resolve, click.ref)
            if el is not None:  # (a vanished element is caught by the same-target check before the click)
                info = await self._ctl(el)
                if info is None or not info.get("ok", True):  # fail closed: where would this control send the data?
                    return self._end("blocked", "Envoi bloqué : destination du formulaire illisible.")
                action = str(info.get("url") or "")
        if action:
            urls.append(action)
        for u in urls:
            host = self._bad_host(u)
            if host:
                res = self._end("blocked", f"Le formulaire est hébergé sur un site non reconnu : {host}")
                res.untrusted_host = self._r(host)
                return res
        if urlparse(action).scheme in ("http", "https") and not _secure_url(action):
            return self._end("blocked", "Page non sécurisée (HTTPS requis) : le formulaire envoie ses données en clair.")
        return None

    # ------------------------------------------------------------ validation (the planner is untrusted)
    def _drop(self, v: _Validated, f: FormField | None, why: str, ask: bool = True) -> None:
        v.notes.append(f"{_label(f) if f else 'champ'} : {why}")
        if ask and f is not None and f.required and f.kind not in ("button", "link") and not any(q.ref == f.ref for q in v.questions):
            v.questions.append(_question(f))

    def _validate(self, plan: StepPlan, snap: PageSnapshot, resolve: Resolver | None = None) -> _Validated:
        if self.trusting:
            return trusting.validate(self, plan, snap, resolve)
        v = _Validated()
        by_ref = {f.ref: f for f in snap.fields}
        proposed = {action.ref: action for action in plan.actions if action.op != "click"}
        dismissing_cookies = any(action.click_role == "cookies" for action in plan.actions)
        seen: set[str] = set()
        clicks: list[tuple[FormField, str]] = []
        manual_refs = {question.ref for question in plan.unknown} if self.mode == "supervised" else set()
        for field in snap.fields:
            if self.mode == "supervised" and self.supervised_widgets and field.form.startswith("0:") and field.destination:
                selected = self._autonomous_decision(field)
                if selected and selected[0] in {"eeo_opt_out", "eeo_race_ethnicity"}:
                    code, index = selected
                    seen.add(field.ref)
                    v.items.append(_Item(Action(ref=field.ref, op="select"), field, option_index=index, used=code))
                    continue
            if self.mode == "supervised" and plan.page_kind != "form":
                continue
            if self.autonomous:
                selected = self._autonomous_decision(field)
                if selected:
                    code, index = selected
                    seen.add(field.ref)
                    if field.kind != "checkbox" or _is_empty(field):
                        op = "check" if field.kind == "checkbox" else "select"
                        v.items.append(_Item(Action(ref=field.ref, op=op), field, option_index=index, used=code))
                    continue
                if blocked_consent(fold(f"{field.label} {field.group}")) and field.kind == "checkbox":
                    seen.add(field.ref)
                    if not _is_empty(field):
                        v.items.append(_Item(Action(ref=field.ref, op="uncheck"), field, used="optional_opt_out"))
                    continue
                if field.kind in CHOICE_KINDS and eeo_field(fold(f"{field.label} {field.group}")):
                    seen.add(field.ref)
                    self._drop(v, field, "réponse volontaire à choisir sur le site", ask=True)
                    continue
            action = proposed.get(field.ref)
            if (plan.page_kind == "form" and not dismissing_cookies and field.ref not in manual_refs
                    and (standing := self._standing_item(field))
                    and (action is None or action.fact == standing.used or standing.used == "recruiter_ai"
                         or (action.fact == "phone" and standing.used == "phone_calling_code"))
                    and not (action and standing.used in {"address", "address_line_2"} and action.value
                             and action.value != standing.value)):
                seen.add(field.ref)
                v.items.append(standing)
                continue
            if (plan.page_kind == "form" and not dismissing_cookies and field.ref not in manual_refs
                    and (action is None or (action.op == "fill" and action.fact in IDENTITY | {"account_email"}))
                    and (ident := self._identity_item(field, snap, action))):
                seen.add(field.ref)
                v.items.append(ident)
                continue
            entry = self.choice_fields.get(field.ref)
            if field.ref in manual_refs or not entry or entry[0] != _question(field) or field_choices(self.page.url, field) != entry[1:]:
                continue
            index = self.choices.get(entry[1])
            if index is not None and index in entry[2]:
                seen.add(field.ref)
                v.items.append(_Item(Action(ref=field.ref, op="select"), field, option_index=index, used="confirmed_form_choice"))
        for a in plan.actions:
            f = by_ref.get(a.ref)
            if f is None:
                v.notes.append(f"action ignorée : référence inconnue ({a.op})")
                continue
            if a.op == "click":
                if f.kind not in ("button", "link") or a.click_role in (None, "other"):
                    v.notes.append(f"clic ignoré sur « {_label(f)} »")
                else:
                    clicks.append((f, a.click_role))
                continue
            if a.ref in seen:
                v.notes.append(f"{_label(f)} : action en double ignorée")
                continue
            seen.add(a.ref)
            item = self._check(a, f, v, snap, resolve)
            if item:
                v.items.append(item)
        pw = self.facts.get("account_password")
        if plan.page_kind == "account_signup" and self.mode == "submit" and pw is not None and pw.secret and self.facts.known(pw.key):
            # a password / confirmation field ("Password" + "Verify New Password") the planner left alone, or whose action was dropped
            # (wrong fact cited for it), gets the same secret: on a sign-up page a password box is never a question
            filled = {item.field.ref for item in v.items}
            for f in snap.fields:
                if f.kind == "password" and f.ref not in filled and _is_empty(f) and (self.signup_scope is None or f.ref in self.signup_scope):
                    frame_url = self._frame_url(resolve, f.ref) if resolve else self.page.url
                    if self._secret(frame_url) is not None:
                        seen.add(f.ref)
                        v.questions = [q for q in v.questions if q.ref != f.ref]
                        v.items.append(_Item(Action(ref=f.ref, op="fill", fact=pw.key), f, pw, used=pw.key, frame_url=frame_url))
        if clicks:
            v.click = clicks[-1]
            if len(clicks) > 1:
                v.notes.append("plusieurs clics proposés : seul le dernier est conservé")
        submit_controls = ([field for field in snap.fields if field.native_submit] if self.mode == "supervised"
                           else [v.click[0]] if v.click and v.click[0].native_submit else [])
        v.items = [item for item in v.items if item.used != "confirmed_form_choice" or any(
            control.form == item.field.form and control.destination == item.field.choice_destination
            for control in submit_controls)]
        return v

    @staticmethod
    def _label_matches(f: FormField, fact: Fact) -> bool:
        """`label_re` against the VISIBLE label; name + placeholder only when the field has no label at all."""
        if not fact.label_re:
            return True
        hay = f.label if f.label.strip() else f"{f.name} {f.placeholder}"
        try:
            return re.search(fact.label_re, fold(hay), re.I) is not None
        except re.error:
            return False  # unusable restriction: fail closed

    @staticmethod
    def _upload_question(op: str) -> str:
        return ("CV" if op == "upload_cv" else "Lettre de motivation") + " (dépôt du fichier impossible sur ce site)"

    def _file_satisfied(self, f: FormField, snap: PageSnapshot) -> bool:
        """A file field the engine already uploaded to (same host, same label) whose input the page cleared after the attach click,
        or whose file name the page now lists: the field counts as filled for the rest of the run."""
        if f.kind != "file":
            return False
        host, label = self._host(), _norm(_label(f))
        text = fold(snap.text)
        return any(h == host and lb == label and (self.attach_done or stem in text) for h, lb, stem in self.uploaded_files)

    @staticmethod
    def _dial_code(fact: Fact) -> str:
        """Calling code of a confirmed (exact) phone, "" when unknown."""
        if not fact.exact:
            return ""
        return "+33" if french_phone(fact.value or "") else international_calling_code(fact.value or "") or ""

    @staticmethod
    def _dial_option(f: FormField, fact: Fact) -> int:
        """Only a unique exact option for the country calling code of a confirmed international number."""
        code = "+33" if french_phone(fact.value or "") else international_calling_code(fact.value or "")
        names = DIAL_COUNTRY_NAMES.get(code or "", frozenset())

        def is_code(label: str) -> bool:  # "+33" in the label, or (a country-only list) exactly the country the code belongs to
            codes = re.findall(r"\+\s*[0-9]+", label)
            return codes == [code] or (not codes and _norm(label) in names)

        matches = [index for index, option in enumerate(f.options) if is_code(option.label)] if code else []
        if not fact.exact or len(matches) != 1:
            return -1
        option = f.options[matches[0]]
        if f.choice_options and sum(value == option.value or is_code(label)
                                    for value, label, _, _ in f.choice_options) != 1:
            return -1
        return matches[0] if _live_option(f, matches[0]) else -1

    def _confirmed_dial_selection(self, field: FormField, snap: PageSnapshot) -> bool:
        phone = self.facts.get("phone")
        if not phone or field.kind not in {"select", "combobox"} or field.multiple or not _dial_selector(field, snap):
            return False
        if field.kind == "combobox":  # an intl-tel-input flag picker: the selected dial code is shown as its value (its list repeats countries)
            code = "+33" if french_phone(phone.value or "") else international_calling_code(phone.value or "")
            return bool(phone.exact and code and re.findall(r"\+\s*[0-9]+", field.value or "") == [code]
                        and not re.sub(r"[+\s0-9]", "", field.value or ""))
        index = self._dial_option(field, phone)
        return index >= 0 and self._selection_matches(field, field.options[index])

    @staticmethod
    def _selection_matches(field: FormField, option) -> bool:
        if field.kind == "combobox":
            return field.value == option.label
        if len(field.selected) != 1:
            return False
        value, label, disabled, _ = field.choice_options[field.selected[0]]
        return not disabled and (value, label) == (option.value, option.label)

    def _open_decision(self, field: FormField, snap: PageSnapshot) -> bool:
        active = not _is_empty(field)
        if self.autonomous and field.kind == "checkbox" and blocked_consent(fold(f"{field.label} {field.group}")):
            return active
        if field.kind == "checkbox" and recruiter_ai_polarity(field.label) is not None:
            standing = self._standing_item(field)
            return standing is None or (standing.act.op == "check") != active
        if field.kind not in CHOICE_KINDS:
            return (field.kind != "password" and _question(field).category == "credentials"
                    and (field.required or active))
        if _site_consent(field):
            if self.autonomous and (self._autonomous_decision(field) or (
                    not field.required and not active and blocked_consent(fold(f"{field.label} {field.group}")))):
                return False
            return field.required or active
        if not active:
            return field.required and (decision_key(field.label) or decision_key(field.group)) not in (
                None, "ambiguous_country", "work_authorization_review") \
                and fact_key(_question(field)) != "needs_visa_sponsorship"
        return not self._confirmed_dial_selection(field, snap)

    @staticmethod
    def _in_scope(field: FormField, target: FormField) -> bool:
        return not target.form or not field.form or field.form == target.form

    async def _live_decisions(self, snap: PageSnapshot, target: FormField, approved: dict[str, tuple], *,
                              pending: set[str] = frozenset(), applying: _Item | None = None, retry: bool = True) -> None:
        """Read the whole live decision scope without retagging the original dispatch target."""
        if self.trusting:
            return
        fresh, _ = await take_snapshot(self.page, read_only=True)
        current = next((field for field in fresh.fields if field.live_ref == target.ref), None)
        if (fresh.url != snap.url or fresh.has_captcha or current is None or current.form != target.form
                or current.kind != target.kind or current.destination != target.destination or current.native_submit != target.native_submit
                or current.name != target.name or current.label != target.label or current.wording != target.wording
                or current.group != target.group or current.autocomplete != target.autocomplete
                or current.automation_id != target.automation_id):
            raise _DecisionHold(self._end("blocked", "Le formulaire a changé depuis la lecture : action annulée."))
        if applying and applying.used == "confirmed_form_choice":
            entry = self.choice_fields.get(target.ref)
            if (not entry or field_choices(fresh.url, current) != entry[1:]
                    or self.choices.get(entry[1]) != applying.option_index
                    or sum(field_choices(fresh.url, field) == entry[1:] for field in fresh.fields) != 1):
                raise _DecisionHold(self._end("blocked", "Le choix confirmé ne correspond plus au formulaire : action annulée."))
        originals = {field.ref: field for field in snap.fields}
        questions = []
        live_refs = {field.live_ref for field in fresh.fields if self._in_scope(field, current)}
        if any(field.ref not in live_refs and self._in_scope(field, target) and field.kind in CHOICE_KINDS
               and (field.required or field.ref in approved or not _is_empty(field)) for field in snap.fields):
            raise _DecisionHold(self._end("blocked", "Une question n’est plus accessible : action annulée."))
        for field in fresh.fields:
            if not self._in_scope(field, current):
                continue
            original = originals.get(field.live_ref)
            if (self.autonomous and applying is None and target.kind in {"button", "link"}
                    and field.required and field.kind not in {"button", "link"}
                    and (original is None or (_is_empty(field) and not self._file_satisfied(field, fresh)))):
                questions.append(_question(original or field))
            if field.kind in CHOICE_KINDS:
                expected = approved.get(field.live_ref)
                if expected is not None and expected == _choice_state(field):
                    continue
                # Benign re-renders: an unanswered question whose option list loaded / re-rendered, or a dependent question that
                # appeared still empty (the open-decision pass below asks it). A different question or selection is refused.
                rerendered = bool(original) and _is_empty(original) and _is_empty(field) and expected is None
                appeared = not original and _is_empty(field)
                if not rerendered and not appeared and (
                        (original and _choice_state(original) != _choice_state(field)) or expected is not None or not original):
                    if retry:  # the page may still be settling: read it once more before giving up
                        await asyncio.sleep(CHANGED_RETRY_S)
                        return await self._live_decisions(snap, target, approved, pending=pending, applying=applying, retry=False)
                    raise _DecisionHold(self._end("blocked", "Une question ou sa sélection a changé : action annulée."))
            awaiting_action = field.live_ref in pending and (_is_empty(field) or field.live_ref == current.live_ref)
            if not awaiting_action and self._open_decision(field, fresh):
                questions.append(_question(original) if original else _question(field).model_copy(update={"ref": ""}))
        if questions:
            raise _DecisionHold(self._end("needs_input", "Décisions à vérifier avant de continuer.", questions))

    async def _record_choice(self, snap: PageSnapshot, item: _Item, shown: str, approved: dict[str, tuple]) -> None:
        field = item.field
        if field.kind not in CHOICE_KINDS or self.trusting:
            return
        fresh, _ = await take_snapshot(self.page, read_only=True)
        current = next((entry for entry in fresh.fields if entry.live_ref == field.ref), None)
        if (fresh.url != snap.url or current is None or _choice_shape(current) != _choice_shape(field)
                or (field.kind == "checkbox" and current.value != shown)
                or (field.kind in {"select", "radio"} and not self._selection_matches(current, field.options[item.option_index]))
                or (field.kind == "combobox" and current.value != shown)):
            raise _DecisionHold(self._end("blocked", "La sélection appliquée n’a pas pu être vérifiée : action annulée."))
        approved[field.ref] = _choice_state(current)

    @staticmethod
    def _cv_by_code(f: FormField, snap: PageSnapshot) -> bool:
        hay = fold(f"{f.label} {f.group} {f.name}")
        if OTHER_DOC_RE.search(hay):
            return False  # a label / group / name that names another document at all (mixed, conflicting): the verifier decides
        if CV_RE.search(hay):
            return True
        return sum(x.kind == "file" or (x.kind in ("button", "link") and bool(FILE_OPENER_RE.search(fold(x.label)))) for x in snap.fields) == 1

    def _check(self, a: Action, f: FormField, v: _Validated, snap: PageSnapshot, resolve: Resolver | None) -> _Item | None:
        drop = lambda why, ask=True: self._drop(v, f, why, ask)  # noqa: E731
        op, k = a.op, f.kind
        if a.fact == "race_ethnicity":
            return drop("origine ethnique : sélection contrôlée uniquement")
        if _question(f).category == "credentials" and k != "password":
            return drop("mot de passe et champ incompatibles" if a.fact == "account_password"
                        else "identifiant technique à saisir sur le site")
        if op in ("upload_cv", "upload_letter"):
            if self.autonomous and (op != "upload_cv" or k != "file"):
                return drop("document à déposer à la main")
            if k == "file" and self._file_satisfied(f, snap):
                return drop("fichier déjà déposé dans cette candidature", ask=False)
            if k not in ("file", "button", "link") or (k != "file" and not FILE_OPENER_RE.search(fold(f.label))):
                if op == "upload_cv" or f.required:
                    v.upload_failed = v.upload_failed or self._upload_question(op)
                    v.upload_refs.add(f.ref)
                return drop("envoi de fichier sur un champ qui n'est pas un fichier")
            if op == "upload_letter" and not f.required:
                return drop("lettre facultative : ignorée")
            it = _Item(a, f, used="cv" if op == "upload_cv" else "letter")
            if not (op == "upload_cv" and self._cv_by_code(f, snap)):
                it.llm, it.answer = True, "CV du candidat (PDF)" if op == "upload_cv" else "lettre de motivation (PDF)"
            return it
        if op == "letter_text":
            if k not in ("textarea", "text"):
                return drop("texte de lettre sur un champ inadapté")
            if not f.required:
                return drop("lettre facultative : ignorée")
            return _Item(a, f, used="letter", llm=True, answer="texte de la lettre de motivation")
        fits = {"fill": k in TEXT_KINDS, "select": k in ("select", "radio", "combobox"), "check": k == "checkbox",
                "uncheck": k == "checkbox"}
        if not fits.get(op, False):
            return drop(f"opération {op} incompatible avec un champ {k}")
        exact_key = _question(f).fact_id
        dial = k in ("select", "combobox") and _dial_selector(f, snap)
        choice = decision_key(f.label) or decision_key(f.group)
        if k in CHOICE_KINDS and _unlabeled(f):
            return drop("case ou liste sans libellé lisible : jamais cochée ni choisie")
        if a.fact == "consent_required" or (k in CHOICE_KINDS and _site_consent(f)):
            return drop("consentement à donner sur le site")
        if self.autonomous and k in CHOICE_KINDS and blocked_consent(fold(f"{f.label} {f.group}")):
            return drop("option de diffusion exclue")
        if choice and exact_key not in {"work_authorization", "needs_visa_sponsorship"} and not (
                choice == "ambiguous_country" and (dial or exact_key == "country")):
            return drop("décision à confirmer sur le site")
        if a.fact == "work_authorization" or exact_key == "work_authorization":
            if exact_key != "work_authorization" or a.fact != exact_key or op != "fill" or a.inferred:
                return drop("autorisation de travail : question ou format non confirmé")
        explicit_facts = {"address", "address_line_2", "department", "phone_calling_code", "graduation_date",
                          "politically_exposed", "internship_agreement", "recruiter_ai", "conflict_of_interest", "former_employer",
                          "relatives_at_employer"}
        phone_dial = dial and a.fact == "phone" and not a.inferred
        if (exact_key in explicit_facts or a.fact in explicit_facts) and not phone_dial:
            return drop("réponse exacte confirmée nécessaire")
        address_facts = {"city", "postal_code", "country", "phone_device_type"}
        if exact_key in address_facts or a.fact in address_facts:
            if a.fact != exact_key or a.inferred:
                return drop("information exacte du candidat nécessaire")
        if dial and (a.fact != "phone" or a.inferred):
            return drop("indicatif international confirmé nécessaire")
        fact = self.facts.get(a.fact)
        if a.fact == "needs_visa_sponsorship" or exact_key == "needs_visa_sponsorship":
            if (exact_key != "needs_visa_sponsorship" or a.fact != exact_key or a.inferred or op != "select"
                    or not fact or _norm(fact.value or "") not in {"oui", "yes", "non", "no", "ja", "nein"}):
                return drop("parrainage de visa : question ou format non confirmé")
            affirmative = {"oui", "yes", "ja"}
            negative = {"non", "no", "nein"}
            labels = affirmative if _norm(fact.value or "") in affirmative else negative
            matches = [index for index, option in enumerate(f.options) if _norm(option.label) in labels]
            if len(matches) != 1 or (a.value and _norm(a.value) not in labels):
                return drop("aucun choix simple et exact pour le parrainage de visa")
            return _Item(a, f, fact, option_index=matches[0], used=fact.key)
        if fact is None:
            if not a.inferred:
                return drop("fait cité inconnu")
            if k == "password":
                return drop("mot de passe et champ incompatibles")
        elif (k == "password") != fact.secret:
            return drop("mot de passe et champ incompatibles")
        if fact is not None and fact.secret:
            frame_url = self._frame_url(resolve, f.ref) if resolve else self.page.url
            if self.mode == "dry_run":
                return drop("mot de passe jamais saisi en simulation", ask=False)
            if self._secret(frame_url) is None:
                return drop(f"fait « {fact.key} » sans valeur (ou page non sécurisée)")
            if op == "fill" and k != "password" and not self._label_matches(f, fact):  # (a password field is one whatever its language)
                return drop("champ sans rapport avec le fait cité")
            return _Item(a, f, fact, used=fact.key, frame_url=frame_url)
        if fact is not None and not self.facts.known(fact.key):
            if not a.inferred:
                return drop(f"fait « {fact.key} » sans valeur")
            fact = None  # a deduction may cite a fact that has no value: it then rests on the profile
        if fact is None and not f.required:
            return drop("réponse déduite sur un champ facultatif : ignorée")  # (no question: it is optional)
        if fact is not None and op == "fill" and not self._label_matches(f, fact):
            return drop("champ sans rapport avec le fait cité")
        item = _Item(a, f, fact, used=fact.key if fact else "", hard=is_hard_fact(f"{f.label} {f.group}", f.name, f.placeholder))
        want = fact.value if fact is not None and (fact.exact or fact.key in address_facts | {"work_authorization"}) else (
            a.value or (fact.value if fact is not None else None))
        item.value = want
        if self.autonomous and item.hard and (a.inferred or fact is None or not fact.value
                or (not fact.exact and not derived_from(want or "", fact.value))):
            return drop("fait dur absent des faits confirmés")
        if (self.autonomous and (a.inferred or fact is None or not fact.value)
                and _UNATTENDED_NO_GUESS.search(" ".join(filter(None, (f.label, f.group, f.name, f.placeholder))))):
            return drop("diplôme, date de début ou salaire : jamais deviné sans vous (réponse enregistrée requise)")

        def need_verdict(answer: str, backed: bool) -> _Item | None:
            if item.hard and (a.inferred or fact is None):
                return drop("fait dur (état civil, adresse, pièces, permis...) : jamais deviné")
            item.llm, item.answer, item.backed = True, answer, backed
            return item

        if op in ("check", "uncheck"):
            if op == "check" and not f.required and ((fact is not None and fact.key == "consent_optional") or MARKETING_RE.search(f.label)):
                return drop("case facultative non cochée")
            return need_verdict("true" if op == "check" else "false", False)
        if fact is not None and fact.key == "phone" and dial and k == "combobox":
            # a typeahead picker (its list only exists once opened, often truncated): type the code, pick the one option holding it
            item.dial_code = self._dial_code(fact)
            if not item.dial_code:
                return drop("indicatif téléphonique introuvable pour ce numéro")
            item.value = item.dial_code
            return need_verdict(item.dial_code, True)
        if fact is not None and fact.key == "phone" and k in TEXT_KINDS and any(
                x is not f and x.form == f.form and _dial_selector(x, snap) for x in snap.fields):
            item.national = True
        if fact is not None and fact.key == "phone" and dial:
            item.option_index = self._dial_option(f, fact)  # the code of the candidate's own phone decides, not the planner's text
            if item.option_index < 0:
                return drop("indicatif téléphonique introuvable pour ce numéro")
            return need_verdict(f.options[item.option_index].label, True)
        if fact is not None and fact.key == "phone_device_type":
            matches = [index for index, option in enumerate(f.options) if device_type(option.label) == fact.value]
            if len(matches) != 1:
                return drop("type de téléphone déclaré absent des options")
            item.option_index = matches[0]
            return need_verdict(f.options[item.option_index].label, True)
        if (self.supervised_widgets and k in ("select", "combobox") and op == "select" and not a.inferred
                and fact and fact.exact and (segment := date_segment(f)) and segment[0] == fact.key):
            item.option_index = date_segment_option(f, fact.value)
            if item.option_index < 0 or a.value != f.options[item.option_index].label:
                return drop("segment de date non vérifié")
            item.value = f.options[item.option_index].label
            return item
        if k in ("select", "radio") or (k == "combobox" and f.options):
            item.option_index = _match_option(f, want)
            if item.option_index < 0:
                return drop("valeur hors des options proposées")
            label = f.options[item.option_index].label
            return need_verdict(label, fact is not None and option_matches_fact(label, fact.value or ""))
        if k == "combobox":
            return need_verdict(want or "", fact is not None and derived_from(want or "", fact.value))
        if fact is not None and fact.exact:
            return item  # identity fact: typed verbatim by code (the label was checked above), no LLM
        if not (want or "").strip():
            return drop("valeur manquante")
        return need_verdict(want or "", fact is not None and derived_from(want or "", fact.value))

    def _numbers_known(self, text: str) -> bool:
        """Every number of two or more digits of an inferred free-text answer is stated by some known fact (or is this year / the
        next one)."""
        known = {m for f in self.facts.facts if f.value and not f.secret for m in re.findall(r"\d{2,}", f.value)}
        known |= {str(datetime.now().year), str(datetime.now().year + 1)}
        return all(n in known for n in re.findall(r"\d{2,}", text))

    async def _verify(self, v: _Validated) -> None:
        """One independent LLM pass over every answer code cannot accept by itself (see answer_check). Deductions fail closed."""
        pend = [it for it in v.items if it.llm]
        if not pend or self.trusting:
            return
        verdicts: dict[str, str] = {}
        if self.verifier is not None:
            items = []
            for it in pend:
                f, fact = it.field, it.fact
                q = _label(f) + (f" ({f.group})" if f.group and f.group != f.label else "")
                kind = f.kind if it.act.op not in ("upload_cv", "upload_letter", "letter_text") else it.act.op
                if it.act.op == "fill":
                    kind = "fill"
                items.append(AnswerCheckItem(
                    ref=f.ref, question=self._r(q)[:300], kind=kind, options=[self._r(o.label)[:100] for o in f.options[:60]],
                    answer=self._r(it.answer)[:300], fact_label=fact.label if fact else "",
                    fact_value=(fact.value or "") if fact and not fact.secret else "",
                    inferred=it.act.inferred, basis=self._r(it.act.basis)[:200]))
            catalogue = [{"key": d["key"], "label": d["label"], "value": d["value"]} for d in facts_payload(self.facts)]
            try:
                try:
                    raw = await self.verifier.check(items, catalogue)
                except TypeError:  # a verifier written before the facts catalogue existed
                    raw = await self.verifier.check(items)
                verdicts = {r: x for r, x in ((r, as_verdict(x)) for r, x in (raw or {}).items()) if x}
            except Exception:
                verdicts = {}
        available = bool(verdicts)  # no verifier / LLM error / nothing usable: only what code can back goes through
        fact_ops = ("fill", "select", "check", "uncheck")
        kept = []
        for it in v.items:
            if not it.llm:
                kept.append(it)
                continue
            a, f = it.act, it.field
            verdict = verdicts.get(f.ref, "") if available else ("supported" if it.backed else "")
            if verdict == "supported" and a.op in fact_ops and it.fact is None:
                verdict = "plausible"           # no fact states it: at best a deduction
            if verdict == "plausible" and it.backed:
                verdict = "supported"           # code vouches for it (verbatim fact / strict date re-formatting)
            if verdict == "plausible" and (it.hard or a.op not in fact_ops or (it.fact is None and not f.required)):
                verdict = ""                    # no deduction on hard facts / uploads, nor without a fact on an optional field
            if verdict == "supported" and it.hard and it.fact is None:
                verdict = ""
            if verdict in ("supported", "plausible"):
                # `inferred` stays False ONLY when code establishes the answer (verbatim fact, strict date re-formatting, option equal
                # to the fact) or the candidate's own `custom_*` answer is cited and the verifier agrees. Everything else the
                # candidate is told about, with its basis.
                custom = verdict == "supported" and it.fact is not None and it.fact.key.startswith("custom_")
                # an option with more words than the fact states ("Yes, with sponsorship" for "Yes") on a hard-fact field is reported
                qualified = (it.hard and it.fact is not None and a.op == "select"
                             and len(it.answer.split()) > len((it.fact.value or "").split()))
                if qualified or not (it.backed or custom):
                    if a.op == "fill" and not self._numbers_known(it.answer or it.value or ""):
                        self._drop(v, f, "nombre absent des faits connus : jamais inventé")  # invented salary / phone / ID / year count
                        continue
                    it.inferred = True
                    it.basis = (a.basis.strip() or (f"déduit du fait « {it.fact.label} »" if it.fact else "déduit du profil"))[:200]
                kept.append(it)
            else:
                if a.op in ("upload_cv", "upload_letter") and (a.op == "upload_cv" or f.required):
                    v.upload_failed = v.upload_failed or self._upload_question(a.op)
                    v.upload_refs.add(f.ref)
                why = {"contradiction": "contredit un fait connu", "hard_fact": "fait dur inconnu : jamais deviné"}.get(
                    verdicts.get(f.ref, ""), "")
                self._drop(v, f, "réponse non étayée par le fait cité" + (f" ({why})" if why else ""))
        v.items = kept

    def _effective_role(self, f: FormField, role: str, snap: PageSnapshot) -> str:
        """Re-classify a click as `submit` when the planner understates what the button does."""
        if self.trusting:
            return trusting.effective_role(self, f, role, snap)
        has_file = any(x.kind == "file" for x in snap.fields)
        has_pw = any(x.kind == "password" for x in snap.fields)
        # "Holds the application": we entered data in this run, a file is selected, or the page / frame holds the candidate's OWN
        # identity (`page_data`, see _identity_needles). Unrelated inputs that have values (filters, search boxes) never count.
        formlike = self.touched or self.page_data or any(x.kind == "file" and x.value.strip() for x in snap.fields)
        final = bool(SUBMIT_RE.search(f.label)) and not NAV_RE.search(f.label)
        if role == "cookies" and COOKIE_RE.fullmatch(f.label.strip()) and not SUBMIT_RE.search(f.label):
            return role
        if role in ("next", "open_form", "cookies") and final and formlike:
            return "submit"
        if role in ("signup", "login"):
            if not has_pw or has_file or (final and formlike and not ACCOUNT_RE.search(f.label)):
                return "submit"
        return role

    # ------------------------------------------------------------ click policy
    @staticmethod
    def _is_cookie_click(f: FormField, role: str) -> bool:
        return role == "cookies" and bool(COOKIE_RE.fullmatch(f.label.strip())) and not SUBMIT_RE.search(f.label)

    def _dry_may_click(self, f: FormField, role: str, snap: PageSnapshot) -> bool:
        """dry_run: the only clicks that can never send anything (see the module docstring). None after data entry."""
        if self.touched:
            return False
        if role == "cookies":  # before any data: a banner button (short label, or one that names cookies and nothing final)
            return self._is_cookie_click(f, role) or (bool(re.search(r"cookie|traceur", f.label, re.I)) and not SUBMIT_RE.search(f.label))
        if role == "open_form":
            return True
        return role == "next" and not any(x.kind not in ("button", "link") for x in snap.fields)

    def _is_final(self, f: FormField, role: str) -> bool:
        """submit mode: does this click possibly send the application? (everything but pure navigation after data entry)"""
        if role == "submit":
            return True
        if self.trusting:
            # The planner's role stands (`trusting.effective_role` already upgraded a navigation-looking click whose label
            # says it sends). A "Nächste" / "Continuer" / dialog opener is not a send; a receipt page reached through one
            # still records the send (see the `confirmation` branch of the loop).
            return False
        if self.mode != "submit" or not self.app_touched:
            return False
        if role == "cookies":
            return not self._is_cookie_click(f, role)
        if role in ("signup", "login"):
            return False  # account forms: _effective_role already turned any file / final-looking one into `submit`
        if role == "next":
            return not (NAV_RE.search(f.label) and not SUBMIT_RE.search(f.label))
        return True

    def _dry_end(self, plan: StepPlan, f: FormField, role: str, planned: str) -> RunResult:
        if role in ("signup", "login") or planned in ("signup", "login") or plan.page_kind in ACCOUNT_KINDS:
            return self._end("ready", "Le site demande un compte, créé uniquement pour un vrai envoi : rien n'a été envoyé (simulation).")
        if role == "submit":
            return self._end("ready", "Formulaire prêt : rien n'a été envoyé (simulation).")
        if role == "next" or planned == "next":
            return self._end("ready", "Première page du formulaire remplie, rien n'a été envoyé : les étapes suivantes n'ont pas été "
                                      "testées (simulation).")
        if self.touched:
            return self._end("ready", "Première page du formulaire remplie : rien n'a été envoyé (simulation).")
        return self._end("ready", f"Simulation : le clic « {f.label[:60]} » n'a pas été effectué, rien n'a été envoyé.")

    async def _same_target(self, target: ElementHandle | Locator | None, f: FormField) -> bool:
        """The exact element behind the ref still is the control seen in the snapshot (label, visible, enabled). Duplicates
        elsewhere in the page are irrelevant."""
        if target is None:
            return False
        live = await live_button(target)
        return live is not None and live[1] and live[2] and _norm(live[0]) == _norm(f.label)

    # ------------------------------------------------------------ execution
    async def _supervised_checkpoint(self, target=None) -> None:
        if self.live_mutation_check:
            try:
                await self.live_mutation_check(target)
            except StepChanged as exc:
                raise _DecisionHold(self._end("needs_human", str(exc))) from exc
            except ManualField:
                raise
            except Exception:
                raise _DecisionHold(self._end("blocked", "Identité du poste non vérifiée : saisie annulée.")) from None
        reasons = await self.before_submit()
        if reasons:
            raise _DecisionHold(self._end("blocked", " ; ".join(reasons)))

    async def _before_mutation(self, op: str, target) -> None:
        if self.autonomous:
            try:
                await self.autonomous.target(target)
                if op == 'upload_cv' and hasattr(self.autonomous, 'upload_pending'):
                    self.autonomous.upload_pending = True
            except ManualField:
                raise
            except ApplyError:
                raise _DecisionHold(self._end("needs_human", SUPERVISED_REQUIRED)) from None
        elif self.mode == "supervised":
            await self._supervised_checkpoint(target)
            if self.before_field:
                try:
                    self.before_field(op)
                except ApplyError as exc:
                    raise _DecisionHold(self._end("blocked", str(exc))) from exc

    async def _execute(self, it: _Item, loc: Locator | ElementHandle, snap: PageSnapshot, log: StepLog) -> str:
        """Carry out one validated action. Returns the (redacted) value for the log; raises _Skip when it cannot be done."""
        a, f = it.act, it.field
        T = ACTION_TIMEOUT_MS
        if self.supervised_widgets and f.kind == "combobox" and a.op == "select":
            return await self._supervised_combobox(loc, f, it)
        standing_radio = (f.kind == "radio" and (self.autonomous or self.mode == "supervised")
                          and it.used == _question(f).fact_id and self._standing_item(f) is not None)
        if it.used in {"eeo_opt_out", "eeo_race_ethnicity", "application_background_check"} or standing_radio:
            if self.autonomous:
                nonce = self.autonomous.document_nonce
            else:
                nonce = secrets.token_urlsafe(24)
                await self.page.evaluate("nonce => { document.__bp_autonomous_document = nonce; }", nonce, isolated_context=True)
            await self._before_mutation(a.op, loc)
            try:
                applied = await apply_demographic_choice(loc, f, it.option_index, url=snap.url, nonce=nonce)
            except Exception:
                applied = False
            if not applied:
                raise _DecisionHold(self._end("blocked", "Une question ou sa sélection a changé : action annulée."))
            if it.used == "application_background_check":
                self.consent_codes.add(it.used)
            return f.options[it.option_index].label
        if self.autonomous:
            if a.op == "upload_cv" and f.kind == "file":
                await self._before_mutation(a.op, loc)
                await loc.set_input_files({"name": "CV.pdf", "mimeType": "application/pdf", "buffer": self.uploads.cv_bytes}, timeout=T)
                self.uploaded = True
                return "CV.pdf"
            if it.used in {"application_privacy", "application_terms", "optional_opt_out"}:
                await self._before_mutation(a.op, loc)
                await (loc.check if a.op == "check" else loc.uncheck)(timeout=T)
                if a.op == "check":
                    self.consent_codes.add(it.used)
                return "true" if a.op == "check" else "false"
            if f.kind not in {"select", "text", "email", "tel", "url", "number", "date", "textarea", "password"} and not (
                    f.kind == "checkbox" and it.used == "recruiter_ai"):
                raise ManualField()
        if self.mode == "supervised":
            if a.op == "upload_cv" and f.kind == "file" and self.uploads.cv_bytes is not None:
                await self._before_mutation(a.op, loc)
                await loc.set_input_files({"name": "CV.pdf", "mimeType": "application/pdf", "buffer": self.uploads.cv_bytes}, timeout=T)
                self.uploaded = True
                return "CV.pdf"
            if (f.kind not in ("select", "text", "email", "tel", "url", "number", "date", "textarea")
                    or a.op not in ("fill", "select")) and not (f.kind == "checkbox" and it.used == "recruiter_ai"):
                raise _Skip("à compléter à la main")
        if a.op == "upload_cv":
            await self._set_files(loc, f, self.uploads.cv_pdf)
            return self.uploads.cv_pdf.name
        if a.op == "upload_letter":
            letter = await self._letter_obj()
            if letter is None or letter.pdf_path is None:
                raise _Skip("lettre indisponible")
            await self._set_files(loc, f, letter.pdf_path)
            return letter.pdf_path.name
        if a.op == "letter_text":
            letter = await self._letter_obj()
            if letter is None or not letter.text:
                raise _Skip("lettre indisponible")
            await self._type(loc, f, letter.text)
            return letter.text[:60] + "…"
        fact = it.fact
        if a.op in ("check", "uncheck"):
            want = a.op == "check"
            live_label = _norm(str(await loc.evaluate("e => { const c = (s) => (s || '').replace(/\\s+/g, ' ').trim(); "
                                                      "return c(e.labels && e.labels.length ? e.labels[0].textContent : "
                                                      "(e.closest('label') ? e.closest('label').textContent : e.getAttribute('aria-label'))); }") or ""))
            snap_label = _norm(f.label)
            if live_label and snap_label and live_label not in snap_label and snap_label not in live_label:
                raise _Skip("libellé de la case modifié depuis la lecture")
            if it.used == "recruiter_ai":
                await self._before_mutation(a.op, loc)
                await (loc.check if want else loc.uncheck)(timeout=T)
                return "true" if want else "false"
            try:
                await (loc.check if want else loc.uncheck)(timeout=T)
            except Exception:
                await (loc.check if want else loc.uncheck)(force=True, timeout=T)
            return "true" if want else "false"
        if f.kind == "radio":
            # the page may have re-ordered the group since the snapshot: resolve the validated option LIVE, by value AND label
            want = f.options[it.option_index]
            live = await loc.evaluate_all(_RADIOS_JS)
            idx = [i for i, r in enumerate(live) if _norm(r["label"]) == _norm(want.label) and (not want.value or r["value"] == want.value)]
            if len(idx) != 1:
                raise _Skip("option introuvable dans le groupe actuel")
            tgt = loc.nth(idx[0])
            try:
                await tgt.check(timeout=T)
            except Exception:
                if it.used == "confirmed_form_choice":
                    raise
                await tgt.check(force=True, timeout=T)
            after = [r for r in await loc.evaluate_all(_RADIOS_JS) if r["checked"]]
            if len(after) != 1 or _norm(after[0]["label"]) != _norm(want.label) or (want.value and after[0]["value"] != want.value):
                raise _Dirty("une autre réponse est cochée")  # a radio cannot be unchecked: the step stops here
            return after[0]["label"]
        if f.kind == "select":
            o = f.options[it.option_index]
            if it.used == "confirmed_form_choice":
                matching = await loc.evaluate("(e, value) => Array.from(e.options).filter(o => !o.disabled && "
                                              "!o.closest('optgroup[disabled]') && o.value === value)"
                                              ".map(o => (o.label || o.textContent).trim())", o.value)
                if not matching or any(" ".join(label.split()) != " ".join(o.label.split()) for label in matching):
                    raise _Skip("options du formulaire modifiées depuis la lecture")
            await self._before_mutation(a.op, loc)
            await loc.select_option(value=o.value, timeout=T) if o.value else await loc.select_option(label=o.label, timeout=T)
            shown = str(await loc.evaluate("e => { const o = e.selectedOptions[0]; return o ? (o.label || o.textContent).trim() : ''; }") or "")
            if _norm(shown) != _norm(o.label):  # read back what the control really holds: log THAT, and never keep another choice
                if it.used == "confirmed_form_choice":
                    raise _Dirty("la sélection confirmée n’a pas été retenue")
                await self._before_mutation(a.op, loc)
                with contextlib.suppress(Exception):
                    await loc.evaluate("e => { e.selectedIndex = -1; e.dispatchEvent(new Event('change', {bubbles: true})); }")
                raise _Skip("valeur lue différente de la réponse validée")
            return shown
        if f.kind == "combobox":
            return await self._combobox(loc, f, it)
        if fact is not None and fact.secret:
            if self.mode == "dry_run" or f.kind != "password":
                raise _Skip("mot de passe jamais saisi ici")
            pw = self._secret(it.frame_url)
            if pw is None:
                raise _Skip("mot de passe indisponible")
            self.secrets.add(pw)       # from now on masked everywhere, and no screenshot any more
            await self._before_mutation(a.op, loc)
            await self._type(loc, f, pw)
            return SECRET_LOG
        text = it.value or ""
        if fact is not None and fact.exact and fact.variants:
            if isinstance(loc, ElementHandle):
                ml, pat = await loc.evaluate("e => [e.getAttribute('maxlength'), e.getAttribute('pattern')]")
            else:
                ml = await loc.get_attribute("maxlength", timeout=T)
                pat = await loc.get_attribute("pattern", timeout=T)
            text = pick_variant(fact, f, int(ml) if ml and ml.isdigit() else None, pat, national=it.national)
        await self._before_mutation(a.op, loc)
        await self._type(loc, f, text)
        return text

    async def _set_files(self, loc: Locator, f: FormField, path: Path) -> None:
        """Put a file on the field. A file input directly; a styled button / label: the file input associated with it (same label /
        container / data target) when there is one, else Playwright's file-chooser interception: ONE click, never repeated."""
        T = ACTION_TIMEOUT_MS
        if f.kind == "file":
            await loc.set_input_files(str(path), timeout=T)
            self.uploaded = True
            return
        el = await loc.element_handle(timeout=T)
        near = (await el.evaluate_handle(_NEAR_FILE_JS)).as_element()
        if near is not None:
            await near.set_input_files(str(path))
            self.uploaded = True
            return
        try:
            async with self.page.expect_file_chooser(timeout=5000) as fci:
                await self._click(el)
            chooser = await fci.value
            await chooser.set_files(str(path))
        except Exception as e:
            # no chooser opened (the click is NOT repeated): any file input of the same form / frame that looks like the CV one
            any_input = None
            with contextlib.suppress(Exception):
                any_input = (await el.evaluate_handle(_ANY_FILE_JS)).as_element()
            if any_input is None:
                raise _Skip("sélecteur de fichier non ouvert") from e
            await any_input.set_input_files(str(path))
            if await any_input.evaluate("x => x.files.length") != 1:
                raise _Skip("fichier non retenu par la page") from e
        self.uploaded = True

    async def _type(self, loc: Locator, f: FormField, text: str) -> None:
        T = ACTION_TIMEOUT_MS
        if note_value := getattr(self.autonomous, "note_value", None):
            note_value(text)  # a GET carrying it to an untrusted site is a write (core guard mode)
        if f.kind != "textarea":
            text = text.replace("\r", " ").replace("\n", " ")  # never a stray Enter in a single-line field
        if self.autonomous or self.mode == "supervised" or f.kind in ("date", "number") or len(text) > 120:
            await loc.fill(text, timeout=T)
        else:
            await loc.click(timeout=T)
            await loc.fill("", timeout=T)
            await loc.press_sequentially(text, delay=random.randint(*TYPE_DELAY_MS), timeout=T)
        await human_pause(0.1, 0.4)

    @staticmethod
    async def _combo_value(loc: Locator) -> str:
        return str(await loc.evaluate("e => { const i = e.matches('input,textarea') ? e : e.querySelector('input'); "
                                      "return (i ? i.value : (e.innerText || e.textContent || '')).trim(); }") or "")

    async def _supervised_combobox(self, loc: ElementHandle, field: FormField, item: _Item) -> str:
        if (not field.form.startswith("0:") or not field.destination or _site_consent(field) or denied_widget(field)
                or field.multiple or not 0 <= item.option_index < len(field.options)):
            raise _Skip("liste à compléter à la main")
        option = field.options[item.option_index]
        expected = [option.label, option.value]
        start_url = self.page.url
        for phase in ("open", "choose"):
            fresh, _ = await take_snapshot(self.page, read_only=True)
            current = next((entry for entry in fresh.fields if entry.live_ref == field.ref), None)
            if (fresh.url != start_url or not current or _choice_shape(current) != _choice_shape(field)
                    or _site_consent(current) or denied_widget(current)):
                raise _Skip("liste de choix non vérifiée")
            await self._before_mutation("select", loc)
            if not await loc.evaluate(WIDGET, [phase, DENIED, expected, field.choice_options, start_url, field.destination]):
                raise _Skip("liste de choix modifiée")
        return await self._combo_value(loc)

    async def _abandon_combobox(self, loc: Locator) -> None:
        """Leave the control without choosing: clear what was typed and blur it (no key press: Escape is never sent)."""
        with contextlib.suppress(Exception):
            if await loc.evaluate("e => e.matches('input,textarea')"):
                await loc.fill("", timeout=2000)
        with contextlib.suppress(Exception):
            await loc.evaluate("e => e.blur()")

    async def _combobox(self, loc: Locator, f: FormField, it: _Item) -> str:
        """Type the validated answer, then pick the live option STRICTLY equal to it (equality / alias, never a substring or an
        "only match"); read the control back and log what it really holds. Anything else: nothing is chosen (`_Skip`)."""
        T = ACTION_TIMEOUT_MS
        want = it.value or ""
        if f.options and it.option_index >= 0:
            want = f.options[it.option_index].label
        codes = lambda t: re.findall(r"\+\s*[0-9]+", t)  # noqa: E731
        await loc.click(timeout=T)
        if await loc.evaluate("e => e.matches('input,textarea')"):
            await loc.fill("", timeout=T)
            await loc.press_sequentially(want, delay=random.randint(*TYPE_DELAY_MS), timeout=T)
        await asyncio.sleep(0.3)
        found: list[tuple[Locator, str]] = []
        for fr in self.page.frames:
            opts = fr.get_by_role("option")
            for i in range(min(await opts.count(), 200)):
                o = opts.nth(i)
                if await o.is_visible():
                    found.append((o, (await o.inner_text()).strip()))
        if it.dial_code:  # exactly one option names this calling code (+1 is shared: then nothing is chosen)
            hit = [o for o, t in found if codes(t) == [it.dial_code]]
            hit = hit if len(hit) == 1 else []
        else:
            hit = [o for o, t in found if option_matches_fact(t, want)]
        if not hit:
            await self._abandon_combobox(loc)
            raise _Skip("option introuvable dans la liste")
        await hit[0].click(timeout=T)
        await human_pause(0.1, 0.4)
        live = await self._combo_value(loc)
        if not (codes(live) == [it.dial_code] if it.dial_code else option_matches_fact(live, want)):
            await self._abandon_combobox(loc)
            raise _Skip("valeur lue différente de la réponse validée")
        return live

    @staticmethod
    async def _trial(el: ElementHandle, timeout: int) -> bool:
        """Is the element clickable? (Playwright's trial: every actionability check, NO event dispatched.)"""
        return await trial_click(el, timeout)

    async def _click(self, target: ElementHandle | Locator, *, recheck=None) -> None:
        """Activate the exact element EXACTLY ONCE. Actionability is learnt with non-dispatching trials (retried after scrolling
        into view); the dispatch is then one normal click (trial passed) or, when the element stays covered, one `e.click()`.
        A dispatch that raises is never retried: it may have fired -> `_Dispatched`."""
        if isinstance(self, _Run):  # (unit tests call this unbound)
            self.click_window = time.monotonic() + CLICK_ATTRIBUTION_S
        async def gate():
            if recheck is not None:
                await recheck()

        await click_once(target, recheck=gate, timeout_ms=ACTION_TIMEOUT_MS, trial=_Run._trial)

    async def _after_click(self) -> None:
        await asyncio.sleep(CLICK_WAIT_S)
        usable: list[Page] = []
        for p in self.opened:  # a popup / new tab is followed only when it is an http(s) page
            with contextlib.suppress(Exception):
                await p.wait_for_load_state("domcontentloaded", timeout=5000)
            for _ in range(24 if self.trusting else 0):  # a new tab that is still on about:blank (slow redirect chain to the ATS)
                if p.is_closed() or urlparse(p.url).scheme in ("http", "https"):
                    break
                await asyncio.sleep(0.5)
            if not p.is_closed() and urlparse(p.url).scheme in ("http", "https"):
                usable.append(p)
            else:
                with contextlib.suppress(Exception):
                    await p.close()
        self.opened = []
        if usable:
            self.page = usable[-1]
            with contextlib.suppress(Exception):
                await self.page.bring_to_front()
        elif self.page.is_closed():
            alive = [p for p in self.page.context.pages if not p.is_closed()]
            if alive:
                self.page = alive[-1]
        with contextlib.suppress(Exception):
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)

    # ------------------------------------------------------------ main loop
    def _guarded_frame(self, request) -> bool:
        """Before any data entry, is this non-GET document request one a saved application draft could be sending? Decided by the
        frame that ISSUES the navigation (the frame whose document is being replaced), never by the destination host: yes for the
        top page, and for any frame below it (or whose parents) currently displaying the application's own site / an allowed host;
        no only for third-party widget frames (captcha, consent), which legitimately POST while a page loads. In doubt: yes."""
        from jobhunt.apply.accounts import registrable_host  # local import: keeps the engine importable on its own

        try:
            frame = request.frame
        except Exception:
            return True
        if frame is None or frame.parent_frame is None:
            return True
        page_host = (urlparse(self.page.url).hostname or "").lower()
        if not page_host:
            return True
        allowed = self.host_allowed
        cur = frame
        while cur is not None and cur.parent_frame is not None:  # the frames below the top page
            host = (urlparse(cur.url or "").hostname or "").lower()
            if not host:
                return True  # about:blank / unknown document: doubt
            if registrable_host(host) == registrable_host(page_host) or (allowed is not None and bool(allowed(host))):
                return True
            cur = cur.parent_frame
        return False

    def _is_form_get_submission(self, request) -> bool:
        """dry run: a GET document navigation that is the GET submission of the form our last click belongs to (the request URL, query
        stripped, is that form's action). Anchors, location changes and buttons outside any form do not match."""
        fi = self.click_form
        if self.mode != "dry_run" or not fi or not fi.get("get") or time.monotonic() > self.click_window or request.method != "GET":
            return False
        return request.url.split("#")[0].split("?")[0] == fi.get("action")

    async def run(self) -> RunResult:
        if self.mode not in ("dry_run", "supervised") and not self.autonomous:
            res = self._end("blocked", UNSUPPORTED_DISPATCH)
            res.finished_at = datetime.now()
            return res
        if self.mode == "supervised" and (self.before_submit is None or self.uploads.cv_bytes is None):
            return self._end("blocked", UNSUPPORTED_DISPATCH)
        return await self._run_page()

    @property
    def _inspection_only(self) -> bool:
        return self.mode == "dry_run"

    def _account_stop(self, code: str, status: RunStatus = "needs_human") -> RunResult:
        result = self._end(status, ACCOUNT_REASONS[code])
        result.diagnostic_code = code
        return result

    def _inline_signup(self, plan: StepPlan, snap: PageSnapshot) -> tuple[FormField, list[Action]] | None:
        """An account-creation form that is on the page itself (Talentsoft's "Création compte" box next to the login box,
        talentview's "Inscription" form): its own submit button, the e-mail / password / name fields that belong to IT (never
        the login box of a shared form: those are told apart by their creation-looking name), and the planner's other
        account-fact fills inside it."""
        for button in snap.fields:
            if button.kind != "button" or not button.form or not SIGNUP_LINK_RE.search(fold(button.label)) or SOCIAL_RE.search(fold(button.label)):
                continue
            members = [f for f in snap.fields if f.form == button.form and f.kind not in ("button", "link")]
            mixed = any(f.kind == "button" and f.form == button.form and LOGIN_BUTTON_RE.search(fold(f.label)) and f.ref != button.ref
                        for f in snap.fields)
            scope = [f for f in members if not mixed or CREATION_NAME_RE.search(fold(f"{f.name} {f.group}"))]
            self.login_box = {f.ref for f in members if f not in scope}  # the sign-in fields that share the form: not ours to fill
            passwords = [f for f in scope if f.kind == "password" and _is_empty(f)]
            emails = [f for f in scope if f.kind in {"text", "email"} and (self.trusting or _is_empty(f))
                      and (f.kind == "email" or EMAIL_FIELD_RE.search(fold(f"{f.label} {f.name}")))]
            if not passwords or not emails:
                continue
            refs = {f.ref for f in scope}
            email_fields = emails[:1]
            if self.trusting:
                email_fields += [f for f in emails[1:] if re.search(r"confirm|repeat|wiederhol", fold(f"{f.label} {f.name}"))]
            actions = [Action(ref=f.ref, op="fill", fact="account_email") for f in email_fields]
            actions += [Action(ref=f.ref, op="fill", fact="account_password") for f in passwords]
            used = {action.ref for action in actions}
            actions += [a for a in plan.actions if a.op in ("fill", "select") and a.fact in ACCOUNT_FACTS - {"account_email", "account_password"}
                        and a.ref in refs and a.ref not in used]
            used |= {a.ref for a in actions}
            for f in scope:  # the identity fields of the form the planner did not propose: answered from the candidate's own facts
                text = fold(f"{f.label} {f.name}")
                key = ("first_name" if re.search(r"prenom|first ?name|given name|first_name", text) else
                       "last_name" if re.search(r"^nom\b|\bnom de famille|last ?name|family name|surname|last_name", fold(f.label).strip() + " " + text) else
                       "phone" if f.kind == "tel" or re.search(r"telephone|phone|mobile", text) else "")
                fact = self.facts.get(key) if key else None
                if f.ref not in used and fact is not None and fact.value and _is_empty(f) and f.kind in {"text", "tel"}:
                    actions.append(Action(ref=f.ref, op="fill", fact=key))
            actions.append(Action(ref=button.ref, op="click", click_role="signup"))
            return button, actions
        return None

    def _account_handoff(self) -> RunResult:
        if self.mode == "supervised" and self.attended_accounts:
            return self._end("needs_human", "Connectez-vous ou créez le compte vous-même.")
        return self._end("blocked", DRY_UNTESTED)

    async def _run_page(self) -> RunResult:
        """Page lifecycle for callers that have already authorized their interaction policy."""
        ctx = self.page.context

        def on_page(p: Page) -> None:  # (a bound builtin is not accepted as a Playwright handler)
            self.all_pages.append(p)
            if self.mode == "dry_run" and self.touched:  # never follow a popup once data was entered
                asyncio.ensure_future(self._close(p))
            else:
                self.opened.append(p)

        async def guard(route, request) -> None:
            """dry_run: once data was entered the engine never navigates: EVERY document request (any method, any frame,
            popups included) is page-initiated and is aborted."""
            try:
                # any document request once data was entered; before that, any NON-GET one (a dry run never needs one: its pre-data
                # clicks are cookie banners and "Postuler" links)
                if self.discovering_apply_entry:
                    await route.fallback()
                elif self.inspection_offline or (self._inspection_only and request.method not in {"GET", "HEAD"}):
                    await route.abort()
                elif request.resource_type == "document" and (self.touched or self._is_form_get_submission(request)
                        or (request.method != "GET" and self._guarded_frame(request))):
                    # Aborted in every case. It ENDS the run only when it can be our doing: after data entry, or right after
                    # a click of ours (a saved draft being posted). A widget that posts while the page loads (e.g. an
                    # "apply with LinkedIn" iframe) is just not loaded.
                    if self.touched or time.monotonic() <= self.click_window:
                        self.blocked_requests.append(f"{request.method} {urlparse(request.url).hostname or ''}")
                    else:
                        self.widget_blocked += 1
                    await route.abort()
                else:
                    if self._inspection_only and not self.apply_navigation:
                        await route.continue_()
                    else:
                        await route.fallback()
            except Exception:
                pass

        ctx.on("page", on_page)
        if self.mode == "dry_run":
            await ctx.route("**/*", guard)
        try:
            try:
                if not self.current_step:
                    await (self.goto or _default_goto)(self.page, self.url)
                if self._inspection_only and not self.apply_entry:
                    self.inspection_offline = True
                    await ctx.set_offline(True)
                res = await self._loop()
            except _DecisionHold as hold:
                res = hold.result
            except LLMError as e:
                self.stop_note = f"LLMError {e.code}"
                res = self._end("failed", f"Planificateur indisponible ({e.code}).")
            except Exception as e:  # message: exception type only, never page content
                self.stop_note = f"{type(e).__name__}: {e}"  # (local failure record only, redacted and trimmed when written)
                res = self._end("failed", f"Erreur technique ({type(e).__name__}).")
                if self.autonomous or self.apply_navigation:
                    res.diagnostic_code = self.diagnostics.exception_code(e)
                if self.steps:
                    await self._shot(self.steps[-1])
            if res.status in DIAGNOSED_STATUSES:  # before a dry run sends its pages to about:blank
                await self._diagnose(res)
        finally:
            with contextlib.suppress(Exception):
                ctx.remove_listener("page", on_page)
            with contextlib.suppress(Exception):
                self.final_url = self.page.url
            if self.mode == "dry_run":
                released = False
                with contextlib.suppress(Exception):
                    released = await self._release()  # (pending timers die with the document) BEFORE the guard goes away
                if released:  # a page that could not be sent to about:blank keeps the guard: the caller closes it
                    with contextlib.suppress(Exception):
                        await ctx.unroute("**/*", guard)
                        await ctx.set_offline(False)
        res.steps = self.steps
        res.final_url = self.final_url
        # (trusting mode: only account sign-in / sign-up clicks are still "pending"; without application data they are not a send)
        res.submit_clicked = self.final_clicked or (self.nav_pending and (self.app_touched or not self.trusting))
        res.finished_at = datetime.now()
        self._scrub(res)
        return res

    stop_note = ""
    rehearse = False  # scripts/rehearse_apply.py: stop before any click that could send what was typed
    rehearse_ok: re.Pattern | None = None  # ... except in-page controls named by the developer (a dialog opener)

    def _trace_plan(self, seen: PageSnapshot, plan: StepPlan) -> None:
        """Keep what the planner was shown and what it answered at the latest steps (labels and kinds, never a value), for the
        local failure record of a trusting run."""
        if not self.trusting:
            return
        by_ref = {f.ref: f for f in seen.fields}
        self.plan_trace.append({
            "step": len(self.steps), "url": bare_url(seen.url), "title": seen.title[:120], "text": seen.text[:700],
            "signals": page_signals(seen.text),
            "fields": [{"ref": f.ref, "kind": f.kind, "label": f.label[:80], "required": f.required, "filled": not _is_empty(f),
                        "error": f.error[:80], "options": len(f.options),
                        # (the option a choice control shows - a country, a level, yes / no - never free text)
                        "chosen": f.value[:60] if f.kind in ("select", "combobox", "radio", "checkbox") or trusting.is_dial(f, seen) else ""}
                       for f in seen.fields][:150],
            "plan": {"page_kind": plan.page_kind, "note": plan.note[:300],
                     "actions": [{"ref": a.ref, "label": _label(by_ref[a.ref])[:60] if a.ref in by_ref else "?", "op": a.op, "fact": a.fact,
                                  "click_role": a.click_role, "has_value": bool(a.value), "inferred": a.inferred} for a in plan.actions][:80],
                     "unknown": [q.label[:80] for q in plan.unknown][:20]}})
        del self.plan_trace[:-14]

    def _stop_reason(self, res: RunResult) -> str:
        """Why the run stopped, for the local failure record: no secret, no typed value (the message is cut when it may quote one)."""
        sensitive = {"account_login", "account_signup", "email_verification"}
        last = self.steps[-1].page_kind if self.steps else ""
        parts = [f"page_kind={last}"] if last else []
        if self.stop_note:
            parts.append(self.stop_note)
        if res.status == "needs_input":
            cats = sorted({q.category or q.reason or "other" for q in res.questions})
            parts.append(f"{len(res.questions)} unanswered question(s): {','.join(cats)}")
            labels = [q.label.strip()[:40] for q in res.questions if q.kind != "password" and q.category != "credentials" and q.label.strip()]
            if labels:  # the field LABELS only (never a value, never a password field's)
                parts.append("fields: " + " | ".join(dict.fromkeys(labels)))
            note = "; ".join(self.steps[-1].note.split("; ")[1:]) if self.trusting and self.steps else ""
            if note and not contains_verification_code(note):  # why each answer failed ("label : reason"), no value
                parts.append("notes: " + note[:500])
        elif last in sensitive or res.status == "needs_email" or contains_verification_code(res.message):
            parts.append("account / verification page (message withheld)")
        else:
            parts.append(res.message)
        if res.diagnostic_code:
            parts.append(f"code={res.diagnostic_code}")
        return safe_reason(" ; ".join(parts), self._r, limit=1000 if self.trusting else 400)

    async def _diagnose(self, res: RunResult) -> None:
        """A run that stops short of sending leaves, in its own folder, a masked screenshot and a value-free page outline."""
        try:
            page = next((p for p in [self.page, *reversed(self.all_pages)] if not p.is_closed()), None)
            if page is None:
                return
            await save_failure_diagnostics(page, self.out_dir, status=res.status, diagnostic_code=res.diagnostic_code or "",
                                           step=len(self.steps), reason=self._stop_reason(res),
                                           screenshot=not self.secrets or self.trusting, redact=self._r)  # (every input is masked in it)
            if self.trusting:  # the step trace (kinds, clicked labels, what failed and why; never a value), local only
                trace = [{"step": s.index, "page_kind": s.page_kind, "url": bare_url(s.url), "clicked": safe_reason(s.clicked, self._r, 80),
                          "filled": [safe_reason(f.label, self._r, 60) for f in s.filled][:60],
                          "note": "" if contains_verification_code(s.note) else safe_reason(s.note, self._r, 900)} for s in self.steps]
                (self.out_dir / "failure-steps.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")
                def masked(value):  # (secrets masked as everywhere)
                    if isinstance(value, dict):
                        return {key: masked(item) for key, item in value.items()}
                    if isinstance(value, list):
                        return [masked(item) for item in value]
                    return self._r(value) if isinstance(value, str) else value

                (self.out_dir / "failure-plans.json").write_text(json.dumps(masked(self.plan_trace), ensure_ascii=False, indent=1),
                                                                 encoding="utf-8")
        except Exception:
            pass

    async def _release(self) -> bool:
        """dry run, end of the run: every page of the run goes to about:blank (no network request, kills the timers of a page
        that planned a delayed form.submit()), then a short wait; the caller removes the request guard afterwards - but ONLY when
        every page did get there (False otherwise: the guard then stays installed, the caller closes the page)."""
        if not RELEASE_PAGES:
            return True
        ok = True
        seen: set[int] = set()
        for p in [self.page, *self.all_pages]:
            if id(p) in seen or p.is_closed():
                continue
            seen.add(id(p))
            try:
                await p.goto("about:blank", timeout=3000)
            except Exception:
                ok = False
        await asyncio.sleep(RELEASE_WAIT_S)
        return ok

    async def _close(self, p: Page) -> None:
        with contextlib.suppress(Exception):
            await p.close()

    async def _observe(self, snap: PageSnapshot, resolve: Resolver, log: StepLog) -> RunResult | tuple[PageSnapshot, Resolver]:
        """A post-data click is unresolved (`nav_pending` / a final click awaiting its receipt): NOTHING is clicked or typed and the
        planner is not asked to plan. The page is polled for (a) a receipt (the planner only CLASSIFIES a page without a form),
        (b) a verified distinct continuation step (`nav_pending` only), (c) a rejection by the page (validation errors on the form:
        correcting and resubmitting is allowed again). Otherwise, when OBSERVE_S is over, the unconfirmed result."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + (TRUSTING_OBSERVE_S if self.trusting else OBSERVE_S)
        asked: tuple[str, str] | None = None
        errors_seen = False
        while True:
            has_form = self._has_form(snap)
            if self.autonomous and self.nav_pending:
                has_form = has_form or any(field.form and field.kind in FILLABLE for field in snap.fields)
            continuation = getattr(self.autonomous, 'continuation', None)
            proven_continuation = bool(continuation and continuation(snap))
            if self.last_sent is not None and not self.last_sent.final and not has_form and not proven_continuation:
                self.final_clicked = True  # the form of a navigation click is gone: it may have been sent
            if snap.has_captcha:
                await self._shot(log)
                return self._end("needs_human", "Un CAPTCHA doit être résolu à la main.")
            if self.autonomous and self.final_awaiting:
                trace = self.diagnostics
                document_receipt = getattr(self.autonomous, 'document_receipt', None)
                if not self.autonomous.final_response:
                    trace.receipt_reason = "no_final_response"
                elif document_receipt:
                    form_present = has_form or any(field.form for field in snap.fields) or not self._form_gone(snap)
                    error_present = any(field.error for field in snap.fields) or await self._banner_error()
                    kind = 'none' if form_present or error_present else await document_receipt(
                        self.page, snap, positive_text=self._confirmed(snap, self.last_sent))
                    if kind != 'none' and self.autonomous.receipt_settled():
                        trace.receipt_kind = kind
                        trace.receipt_reason = 'attested'
                        return self._end('submitted', 'Candidature envoyée.')
                    trace.receipt_reason = 'form_still_present' if form_present or error_present else 'no_confirmation'
                elif self.autonomous.final_json and not (has_form and (
                        any(f.error for f in snap.fields) or await self._banner_error()
                        or {(_norm(_label(f)), f.kind) for f in snap.fields if f.kind in FILLABLE}
                        - (self.last_sent.fields if self.last_sent else set()))):
                    # A positive JSON body alone is not enough: many sites return an id for a draft. Refuse it while
                    # the page still shows a form with an error or new fields to fill (the next step of the form).
                    trace.receipt_reason = "confirmation_json"
                elif not has_form and (self.autonomous.final_receipt_url or self.autonomous.receipt_url(self.page.url)):
                    trace.receipt_reason = "confirmation_url"
                elif self._confirmed(snap, self.last_sent):
                    trace.receipt_reason = "confirmation_text"
                else:
                    trace.receipt_reason = "form_still_present" if has_form else "no_confirmation"
                if (trace.receipt_reason in ("confirmation_url", "confirmation_text", "confirmation_json")
                        and self.autonomous.receipt_settled()):
                    trace.receipt_kind = "first_party_confirmation"
                    return self._end("submitted", "Candidature envoyée.")
                if self.trusting:
                    with contextlib.suppress(Exception):
                        if await trusting.enter_emailed_code(self, snap, resolve):  # the site asks for an e-mailed code first
                            deadline = loop.time() + TRUSTING_OBSERVE_S
                            started = loop.time()
                            snap, resolve = await self._snapshot()
                            continue
                    # The page itself is the evidence, whatever the transport showed: the form of the click is gone and the
                    # page says the application was received (its wording, else the planner's reading of the page).
                    gone = not has_form or self._form_gone(snap)
                    if gone and not any(f.error for f in snap.fields) and not await self._banner_error():
                        received = self._confirmed(snap, self.last_sent)
                        if not received and (snap.url, snap.text) != asked and loop.time() - started >= 3:
                            asked = (snap.url, snap.text)
                            pub, _ = self._public(snap)
                            verdict = await self.planner.plan(pub, self._planner_facts(), job_title=self.job_title,
                                                              company=self.company, history=self.steps[:-1], mode=self.mode)
                            log.page_kind = verdict.page_kind
                            received = verdict.page_kind == "confirmation"
                        if received:
                            trace.receipt_kind, trace.receipt_reason = "first_party_confirmation", "confirmation_text"
                            return self._end("submitted", "Candidature envoyée.")
                    if loop.time() - started >= 5 and await self._submit_rejected(snap):
                        if self.rejected_submits <= 2:
                            return snap, resolve  # the site refused the form (field errors): correct it and send again
                        # Refused a third time: nothing was received, and the form cannot be completed as it stands.
                        missing = [_question(f) for f in snap.fields if f.kind not in ("button", "link") and (
                            f.error or (f.required and _is_empty(f)))][:8]
                        result = self._end("needs_input", "Le site refuse le formulaire : champs obligatoires à compléter.", missing)
                        result.diagnostic_code = "submit_refused"
                        return result
                if loop.time() >= deadline:
                    if self.trusting and await self._submit_rejected(snap, silent=True):
                        # the click sent nothing at all to the site and the form is still there: not a "possible send"
                        if self.rejected_submits <= 2:
                            return snap, resolve
                        result = self._end("failed", "Le bouton d'envoi ne déclenche aucun envoi sur ce site.")
                        result.diagnostic_code = "submit_no_effect"
                        return result
                    return self._unconfirmed()
                await asyncio.sleep(OBSERVE_POLL_S)
                snap, resolve = await self._snapshot()
                continue
            # a field error / error banner is NEVER proof that nothing was sent, and takes precedence over "continuation": it only
            # changes the message of the final (unconfirmed) result
            errors_seen = errors_seen or (has_form and any(f.error for f in snap.fields if f.kind not in ("button", "link"))) \
                or await self._banner_error()
            if self.nav_pending and not errors_seen and ((has_form and self._distinct_step(snap))
                                                        or proven_continuation):
                self.nav_pending = False
                if self.autonomous:
                    await self.autonomous.after_click(self.page)
                return snap, resolve
            # a page without a form, or one where the form of the final click is GONE (an optional upsell / "connect" form of the
            # confirmation page does not make it "still a form"): the planner only CLASSIFIES it
            if (not has_form or (self.final_awaiting and self._form_gone(snap))) and (snap.url, snap.text) != asked:
                asked = (snap.url, snap.text)
                pub, _ = self._public(snap)
                plan = await self.planner.plan(pub, self._planner_facts(), job_title=self.job_title, company=self.company,
                                               history=self.steps[:-1], mode=self.mode)
                log.page_kind = plan.page_kind
                if plan.page_kind == "email_verification":
                    if self.trusting and self._account_only_pending():
                        self._release_account_click()
                        return snap, resolve
                    if await self._verify_email(snap, resolve):
                        return await self._snapshot()
                    return await self._email_hold(log, plan.note if not self.email_verification else '')
                if plan.page_kind == "captcha":
                    await self._shot(log)
                    return self._end("needs_human", "Un CAPTCHA doit être résolu à la main.")
                if plan.page_kind == "closed":
                    await self._shot(log)
                    return self._end("blocked", "Offre clôturée")
                if (plan.page_kind == "confirmation" and self.mode == "submit" and self.final_clicked and not errors_seen
                        and self._confirmed(snap, self.last_sent)):
                    await self._shot(log)
                    return self._end("submitted", "Candidature envoyée.")
            if self._offer_gone(snap):  # an account step the site bounced to its home with a "no such offer" banner: nothing was sent
                await self._shot(log)
                return self._gone_result()
            if loop.time() >= deadline:
                if self._account_only_pending():  # the account page came back (or did not move): the main loop reads what it says
                    self._release_account_click()
                    return snap, resolve
                await self._shot(log)
                return self._end("failed", FIELD_ERROR_MSG) if errors_seen else self._unconfirmed()
            await asyncio.sleep(OBSERVE_POLL_S)
            snap, resolve = await self._snapshot()

    async def _submit_rejected(self, snap: PageSnapshot, *, silent: bool = False) -> bool:
        """The final click did not send the application: same page, the form we filled and its send button are still there,
        and either fields show errors (the page's own validation refused it) or - `silent`, asked when the wait is over - the
        browser sent NO request at all to the site since the click. Nothing was received: the send record is withdrawn and
        the form can be corrected and sent again (twice at most; a third silent click ends the run as not sent)."""
        sent = self.last_sent
        withdraw = getattr(self.autonomous, "unmark_submit", None)
        if sent is None or withdraw is None or urldefrag(self.page.url).url != self.sent_url:
            return False
        present = {(_norm(_label(f)), f.kind) for f in snap.fields}
        intact = bool(sent.fields & present) and any(
            f.kind in ("button", "link") and _norm(_label(f)) == sent.button for f in snap.fields)
        errors = any(f.error for f in snap.fields if f.kind not in ("button", "link"))
        if not errors and intact and self.trusting:
            # The form is still there with a required field empty - one knowingly left so, or one a choice brought with it -
            # while what was filled still holds its values (a form emptied after a send does not): the site's own
            # validation kept it (some mark the field only with a colour or a message we cannot tie to it).
            empty = [f for f in snap.fields if f.required and _is_empty(f) and f.kind not in ("button", "link")
                     and not (f.kind == "file" and self._file_satisfied(f, snap))]
            kept = any(not _is_empty(f) for f in snap.fields
                       if f.kind not in ("button", "link") and (_norm(_label(f)), f.kind) in sent.fields)
            errors = bool(empty) and (kept or any((_norm(_label(f)), f.kind) in self.sent_empty for f in empty))
        exists = False
        if not errors and intact and self.trusting and any(f.kind == "password" for f in snap.fields):
            # A form that creates the account and applies at once came back saying the account exists: nothing was received.
            # The account is signed in to, then the application is made from there.
            errors = exists = await self._says_account_exists()
        if not errors and intact and not silent:
            # The same form came back under an error banner or an "Erreur ..." page title (a server-side validation that
            # lists the missing fields in a banner instead of marking them).
            errors = bool(ERROR_TITLE_RE.match(fold(snap.title).strip())) or await self._banner_error()
        nothing_left = (silent and getattr(self.autonomous, "click_write", object()) is None and not self.autonomous.final_response
                        and not getattr(self.autonomous, "foreign_writes", 1))
        if not intact or not (nothing_left or errors):
            return False
        try:
            await withdraw()
        except Exception:
            return False
        if exists:
            address = self._account_address(self.page.url)
            self.account_exists_hosts.add(address)
            self.login_failed.discard(address)
        self.rejected_submits += 1
        self.final_awaiting = self.final_clicked = False
        self.submit_clicks = max(0, self.submit_clicks - 1)
        self.pending = self.last_sent = self.last_click_rec = None
        self.replans.pop(urldefrag(self.page.url).url, None)
        self.diagnostics.receipt_reason = "before_submit"
        return True

    def _progress(self) -> RunResult:
        """The run so far (redacted), handed to `on_progress` right before a potentially sending click."""
        res = RunResult(status="running", steps=[x.model_copy(deep=True) for x in self.steps], final_url=self.page.url,
                        submit_clicked=self.final_clicked or self.nav_pending)
        self._scrub(res)
        return res

    def _distinct_step(self, snap: PageSnapshot) -> bool:
        """Evidence that the page after a recorded click is a DISTINCT continuation (a new fillable field AND a field or the clicked
        button gone / relabelled; a URL change alone proves nothing) a validation marker proves nothing)."""
        if self.nav_ref is None:
            return True
        _, before, button = self.nav_ref
        inputs = [f for f in snap.fields if f.kind not in ("button", "link")]
        if any(f.error for f in inputs):
            return False  # validation errors take precedence: a re-rendered form with errors is not a next step
        now = {(_norm(_label(f)), f.kind) for f in inputs}
        gone_button = not any(f.kind in ("button", "link") and _norm(_label(f)) == button for f in snap.fields)
        # the FORM advanced: a field that was not there AND something that was there is gone (a URL change proves nothing)
        return bool(now - before) and (bool(before - now) or gone_button)

    async def _gate(self, n: int) -> RunResult | None:
        """submit mode: the send policy (`before_submit`) is consulted ONCE per step, before the first field interaction or click of it.
        A non-empty answer ends the run `blocked`."""
        if self.gated_step == n:
            return None
        reasons = await self.before_submit() if self.before_submit else ["garde-fou d'envoi absent"]
        if reasons:
            return self._end("blocked", "Envoi bloqué : " + " ; ".join(reasons))
        self.gated_step = n
        return None

    def _account_only_pending(self) -> bool:
        """The unresolved click was a candidate-account login / signup (no application data entered, nothing final sent): its
        outcome is judged by the account logic of the main loop, not reported as an unconfirmed send."""
        return bool(self.autonomous and self.mode == "submit" and self.account_try and not self.app_touched
                    and not self.final_clicked and not self.final_awaiting)

    def _release_account_click(self) -> None:
        self.nav_pending = False
        self.pending = self.last_sent = self.nav_ref = None

    def _unconfirmed(self) -> RunResult:
        return self._end("failed", UNCONFIRMED_MSG)

    async def _email_hold(self, log: StepLog, note: str = '') -> RunResult:
        await self._shot(log)
        return self._end('needs_email', note or "Le site attend la validation d'un lien envoyé par e-mail.")

    async def _verify_email(self, snap: PageSnapshot, resolve: Resolver) -> bool:
        if (not self.email_verification or self.email_attempted or not self.autonomous or not self.before_submit
                or self.final_clicked or self.final_awaiting or self.app_touched or snap.has_captcha):
            return False
        self.email_attempted = True
        url = self.page.url
        if url != snap.url or not self.autonomous.write_recipient(url) or await self.before_submit():
            return False
        fields = [field for field in snap.fields if field.kind in {'text', 'number', 'tel', 'password'}
                  and (field.autocomplete == 'one-time-code' or field.automation_id.casefold() in {'verificationcode', 'otp'}
                       or mail.CODE_LABEL.search(field.label))]
        target = await self._el(resolve, fields[0].ref) if len(fields) == 1 else None
        async def still_current():
            if self.page.is_closed() or self.page.url != url or await self.before_submit():
                return ['Vérification interrompue.']
            nonce = await self.page.evaluate('() => document.__bp_autonomous_document', isolated_context=True)
            return [] if nonce and nonce == self.autonomous.document_nonce else ['Document modifié.']

        try:
            verification, code = await self.email_verification.poll()
            if code:
                self.diagnostics.email = code
            if not verification or await still_current():
                return False
            self.diagnostics.control = 'login_otp'
            if verification.kind == 'link':
                self.secrets.add(verification.value)
                if not await self.email_verification.open(self.page, verification.value, still_current):
                    return False
                self.diagnostics.email = 'email_link_used'
                self.pending = self.last_sent = self.nav_ref = None
                self.nav_pending = False
                return True
            if target is None:
                return False
            field = fields[0]
            live = await target.evaluate("""node => ({ok: node.matches('input:not([readonly]):not([disabled])')
                && ['text', 'number', 'tel', 'password'].includes(node.type),
                label: node.getAttribute('autocomplete'), name: node.name, size: node.maxLength,
                value: node.value, lone: !node.form || [...node.form.elements].every(el => el === node || el.disabled
                     || ['submit', 'button', 'reset', 'hidden', 'fieldset'].includes(el.type))})""")
            if (not live['ok'] or not live['lone'] or live['name'] != field.name or field.value or live['value']
                    or (field.autocomplete == 'one-time-code' and live['label'] != field.autocomplete)
                    or (live['size'] >= 0 and live['size'] < len(verification.value))):
                return False
            await self.autonomous.target(target)
            self.secrets.add(verification.value)
            await target.fill(verification.value, timeout=ACTION_TIMEOUT_MS)
            self.diagnostics.email = 'email_code_used'
            buttons = [button for button in snap.fields if button.kind == 'button' and button.native_submit and field.form
                       and button.form == field.form and re.fullmatch(
                           r'(?:verify|validate|confirm|activate|verifier|valider|confirmer|activer|bestatigen)(?: (?:code|le code|email|e-mail))?',
                           fold(button.label).strip())]
            if len(buttons) != 1:
                return False
            button = buttons[0]
            control = await self._el(resolve, button.ref)
            if not await self._same_target(control, button):
                return False
            async def check_click():
                if await still_current() or not await self._same_target(control, button):
                    raise ManualField()
                await self._live_decisions(snap, button, {})
                await self.autonomous.target(control, button=True)
                if (self.autonomous.method != 'POST' or not mail.same_recipient(url, self.autonomous.endpoint)
                        or not await control.evaluate("""(node, code) => node.form && node.form === code.form && code.isConnected
                            && [...code.form.elements].every(el => el === code || el.disabled
                            || ['submit', 'button', 'reset', 'hidden', 'fieldset'].includes(el.type))""", target)):
                    raise ManualField()
                adapter_verify = getattr(self.autonomous, 'arm_email_code', None)
                if adapter_verify:
                    adapter_verify()
                elif mail.VERIFY_ENDPOINT.search(urlparse(self.autonomous.endpoint).path):
                    self.autonomous.arm_click(scripted=False, account=True)
                else:
                    raise ManualField()

            await self._click(control, recheck=check_click)
            await self._after_click()
            await self.autonomous.after_click(self.page)
            if not self.autonomous.click_write:
                return False
            self.pending = self.last_sent = self.nav_ref = None
            self.nav_pending = False
            return True
        except Exception:
            return False

    @staticmethod
    def _application_form(snap: PageSnapshot) -> bool:
        """At least 3 required, still empty input fields and no password field: the page is a step of an application form
        (a job ad's own widgets - job alert, search, share - do not ask that much)."""
        if any(f.kind == "password" for f in snap.fields):
            return False
        return sum(f.kind in FILLABLE - {"checkbox"} and f.required and _is_empty(f) and not trusting.honeypot(f)
                   for f in snap.fields) >= 3

    @staticmethod
    def _signup_submit(snap: PageSnapshot) -> FormField | None:
        """The one account-creation button of a page holding the creation form (the same label twice - a click overlay over
        the real button - is one control): the first of them, in page order."""
        forms = {f.form for f in snap.fields if f.kind == "password"}
        buttons = [f for f in snap.fields if f.kind == "button" and SIGNUP_LINK_RE.search(fold(f.label))
                   and not SOCIAL_RE.search(fold(f.label)) and (not f.form or f.form in forms)]
        if not forms or len({fold(f.label).strip() for f in buttons}) != 1:
            return None
        return buttons[0]

    @staticmethod
    def _has_form(snap: PageSnapshot) -> bool:
        """A form with fillable fields is on the page: a file input or at least 3 fillable fields (a header search box or a
        lone newsletter tick does not make a form)."""
        return any(f.kind == "file" for f in snap.fields) or sum(f.kind in FILLABLE for f in snap.fields) >= 3

    def _form_gone(self, snap: PageSnapshot) -> bool:
        """None of the fields filled on the page of the last send click, nor the clicked button, is on this page any more."""
        sent = self.last_sent
        if sent is None:
            return False
        if sent.fields & {(_norm(_label(f)), f.kind) for f in snap.fields}:
            return False
        return not any(f.kind in ("button", "link") and _norm(_label(f)) == sent.button for f in snap.fields)

    @staticmethod
    def _confirmed(snap: PageSnapshot, sent: _Sent | None) -> bool:
        """Positive proof of a received application: the form of the click is gone (none of the fields filled there, nor the
        clicked button, in any frame), no field shows an error, and the visible text says so."""
        if sent is None or any(f.error for f in snap.fields):
            return False
        if sent.fields & {(_norm(_label(f)), f.kind) for f in snap.fields}:
            return False
        if any(f.kind in ("button", "link") and _norm(_label(f)) == sent.button for f in snap.fields):
            return False
        return confirmation_evidence(f"{snap.title} {snap.text}")

    def _auto_submit_result(self, log: StepLog | None) -> RunResult:
        if log is not None:
            log.note = (log.note + "; " if log.note else "") + AUTO_SUBMIT_NOTE
        return self._end("ready", "Formulaire rempli : une soumission automatique de la page a été bloquée (simulation), rien n'a été envoyé.")

    async def _open_posting_apply(self, *, required: bool, has_form: bool) -> tuple[bool, RunResult | None]:
        live = await self.page.evaluate(r"""() => ({url: location.href, nonce: document.__bp_autonomous_document,
          posting: !!document.querySelector('[itemtype="https://schema.org/JobPosting"]') ||
            Array.from(document.querySelectorAll('script[type="application/ld+json"]')).some(
                s => /"@type"\s*:\s*(?:\[\s*)?"JobPosting"/.test(s.textContent || '')),
          links: Array.from(document.querySelectorAll('a[href]'))
            .filter(a => !a.hasAttribute('download') && a.getClientRects().length
                      && getComputedStyle(a).visibility === 'visible'
                      && /^(?:je postule|postuler(?: maintenant| à cette offre)?|apply(?: now| for this job)?)$/i
                          .test((a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ')))
            .map(a => a.href).slice(0, 40)})""", isolated_context=True)
        required = required or (live["posting"] and not self.apply_link_followed)
        if not required and (has_form or not live["links"]):
            return False, None
        candidates = list(dict.fromkeys(clean_url(link, navigation=True) for link in live["links"]))
        target = candidates[0] if len(candidates) == 1 else ""
        if target and not self.apply_link_followed:
            self.inspection_offline = False
            try:
                if await self.apply_navigation(target, live["url"], live.get("nonce")):
                    self.apply_link_followed = True
                    return True, None
            finally:
                self.inspection_offline = self._inspection_only
        code = ("cross_domain_apply_target" if target and origin(target) != origin(self.page.url)
                else "job_page_no_apply_control")
        result = self._end("needs_human", "Lien de candidature non vérifié.")
        result.diagnostic_code = code
        return False, result

    async def _loop(self) -> RunResult:
        idle = 0
        while self.consumed < self.max_steps or (self.autonomous and self.final_awaiting):
            n = len(self.steps) + 1
            self.consumed += 1
            await self._settle()
            if self.blocked_requests:  # a page script navigated by itself after the last step
                return self._auto_submit_result(self.steps[-1] if self.steps else None)
            if not self.final_awaiting:
                self.diagnostics.step = n
                self.diagnostics.control = "scan"
            if self.current_step:
                await self._supervised_checkpoint()
            if getattr(self.autonomous, "core", False):
                # Core guard mode: the page may have moved at any time; follow it, never refuse it (the reservation can).
                try:
                    if not self.final_awaiting:
                        await self.autonomous.sync(self.page)
                except ApplyError:
                    result = self._end("needs_human", SUPERVISED_REQUIRED)
                    result.diagnostic_code = "reservation_refused"
                    return result
            follow = None if getattr(self.autonomous, "core", False) else getattr(self.autonomous, "follow_landing", None)
            if (follow and not self.final_awaiting and not self.autonomous.data_entered and not self.page.is_closed()
                    and clean_url(self.page.url, navigation=True) != self.autonomous.document):
                # The page moved before any input (redirect, script, pre-data click): bind the landing or stop here.
                if not await follow(self.page):
                    result = self._end("needs_human", SUPERVISED_REQUIRED)
                    result.diagnostic_code = "apply_target_unverified"
                    return result
            snap, resolve = await self._snapshot()
            for _ in range(RENDER_WAITS if self.mode != "supervised" and not self.render_waited and not self.touched and not self.app_touched else 0):
                if snap.fields or len(snap.text.strip()) >= RENDER_TEXT_MIN or snap.has_captcha or self.page.is_closed():
                    break
                self.render_waited = True   # once per run: a page that stays empty is not waited for again
                await asyncio.sleep(RENDER_WAIT_S)
                await self._settle()
                snap, resolve = await self._snapshot()
            if (self.autonomous and self.autonomous.entry_binding and not self.final_awaiting
                    and not getattr(self.autonomous, "core", False)):
                from jobhunt.apply.autonomous import POSTING_IDS_JS

                ids = await self.page.evaluate(POSTING_IDS_JS, isolated_context=True)
                if not self.autonomous.write_recipient(self.page.url) or not self.autonomous.posting_matches(self.page.url, ids):
                    await self.page.close()
                    result = self._end("needs_human", SUPERVISED_REQUIRED)
                    result.diagnostic_code = "posting_mismatch"
                    return result
            if self.current_step:
                await self._supervised_checkpoint()
            if snap.has_captcha and self.captcha_wait_s > 0:  # visible window: give the human time to solve it, touching nothing
                deadline = asyncio.get_running_loop().time() + self.captcha_wait_s
                while snap.has_captcha and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(CAPTCHA_POLL_S)
                    snap, resolve = await self._snapshot()
            self._scrub(self.steps)
            pub, _ = self._public(snap)
            host = self._host()
            log = StepLog(index=n, url=pub.url, page_kind="other", note=f"hôte {host}"
                          + ("" if self.host_allowed is None or not self._bad_host(self.page.url) else " (hors hôte de l'offre)"))
            self.steps.append(log)
            if self.trusting and n == 1 and not self.touched and not self.final_clicked and trusting.posting_to_home(self.url, snap.url):
                await self._shot(log)
                return self._gone_result()
            sent, self.pending = self.pending, None
            if sent is not None and not sent.final and not self.autonomous and not self._has_form(snap):
                self.final_clicked = True  # a click made with application data: the form is gone, it may have been sent
            if snap.has_captcha:
                log.page_kind = "captcha"
                await self._shot(log)
                if self.captcha_wait_s > 0:  # waited for in vain (or its completion cannot be detected): no automatic retry
                    minutes = max(1, round(self.captcha_wait_s / 60))
                    return self._end("needs_human", "CAPTCHA non résolu dans le délai : rouvrir avec « Reprendre dans une fenêtre » depuis la page Envois "
                                                    f"(la fenêtre reste ouverte environ {minutes} minutes) ; si un clic d'envoi avait déjà eu lieu, "
                                                    "vérifier d'abord sur le site de l'entreprise.")
                return self._end("needs_human", "Un CAPTCHA doit être résolu à la main.")
            if self.attach_pending:  # an upload-confirm click ("Ajouter"): an error now is an UPLOAD problem, nothing was sent
                self.attach_pending = False
                # (trusting mode: the planner reads what the page says after the attach click; a "saved" banner is not an error)
                if not self.trusting and (any(f.error for f in snap.fields if f.kind not in ("button", "link")) or await self._banner_error()):
                    files = [f for f in snap.fields if f.kind == "file"]
                    q = _question(files[0]) if files else Question(label="Fichier à joindre", kind="file")
                    await self._shot(log)
                    return self._end("needs_input", f"Informations manquantes : {q.label}.", [q])
            if self.nav_pending or self.final_awaiting:  # a post-data click is unresolved: observe only, plan / click / type nothing
                out = await self._observe(snap, resolve, log)
                if isinstance(out, RunResult):
                    return out
                snap, resolve = out
                pub, _ = self._public(snap)
                log.url = pub.url
            if self.trusting:
                if self.dismissed:
                    log.note += "; fenêtre fermée : " + ", ".join(dict.fromkeys(self._r(x)[:40] for x in self.dismissed))
                    self.dismissed = []
                with contextlib.suppress(Exception):
                    if hint := await trusting.blocker_hint(self):
                        pub.text = self._r(hint) + pub.text
                if self.stuck_fields:
                    pub.text = self._r(trusting.stuck_hint(self.stuck_fields)) + pub.text
                    self.stuck_fields = []
            if self._inspection_only:
                inspection, keys = inspection_inputs(pub, self._planner_facts())
                plan = await self.planner.plan(inspection, keys, job_title="", company="", history=[], mode=self.mode)
            else:
                plan = await self.planner.plan(pub, self._planner_facts(), job_title=self.job_title, company=self.company,
                                               history=self.steps[:-1], mode=self.mode)
            kind = log.page_kind = plan.page_kind
            self._trace_plan(pub, plan)
            for attempt in (1, 2):
                if not (self.trusting and kind == "job_page" and not plan.actions and not self._inspection_only and not self.final_clicked
                        and self.form_hints.get(urldefrag(self.page.url).url, 0) < 2 and self._application_form(snap)):
                    break
                # The planner took a step of the application form (the job title is its heading) for the job ad and did
                # nothing: read it again saying what it is; the second time without the page text and title that mislead it
                # (the fields alone say what the page asks).
                page_key = urldefrag(self.page.url).url
                self.form_hints[page_key] = self.form_hints.get(page_key, 0) + 1
                log.note += "; formulaire de candidature lu comme une offre : relecture"
                hinted = pub.model_copy(update={"text": FORM_HINT + pub.text} if self.form_hints[page_key] == 1
                                        else {"text": FORM_HINT, "title": "Formulaire de candidature"})
                plan = await self.planner.plan(hinted, self._planner_facts(),
                                               job_title=self.job_title, company=self.company, history=self.steps[:-1], mode=self.mode)
                kind = log.page_kind = plan.page_kind
                self._trace_plan(hinted, plan)
            if (self.trusting and kind == "account_login" and not self.email_attempted and not self.app_touched
                    and self._account_address(self.page.url) in self.signed_up and VERIFY_PENDING_RE.search(fold(snap.text))):
                # After the account-creation click the site shows its sign-in page saying the new account must first be
                # verified through the e-mail it sent: the account exists; follow the e-mail, then sign in.
                address = self._account_address(self.page.url)
                self.account_exists_hosts.add(address)
                self.login_failed.discard(address)
                kind = log.page_kind = "email_verification"
                plan = plan.model_copy(update={"page_kind": kind, "actions": []})
                log.note += "; compte créé, en attente de vérification par e-mail"
            core = bool(getattr(self.autonomous, "core", False))
            if (self.apply_entry and not self.touched and not self.app_touched and not self.apply_link_followed
                    and not (core and (self.entry_tries >= ENTRY_TRIES_CORE
                                       or (urldefrag(self.page.url).url, kind) in self.entry_tried))):
                required = kind == "job_page"
                self.entry_tried.add((urldefrag(self.page.url).url, kind))
                self.entry_tries += 1
                self.inspection_offline = False
                self.discovering_apply_entry = True
                try:
                    landing, code = await self.apply_entry(self.page, require_posting=not required and self._has_form(snap))
                finally:
                    self.discovering_apply_entry = False
                    self.inspection_offline = self._inspection_only
                    if self._inspection_only:
                        await self.page.context.set_offline(True)
                if landing is not None:
                    self.page = landing
                    self.apply_link_followed = True
                    continue
                if core:
                    # Core guard mode: no entry proof is required. Without one the run goes on from the current page and the
                    # planner acts on it (it can click "apply" on a job page). Only the reservation refuses.
                    if code == "reservation_refused":
                        result = self._end("needs_human", SUPERVISED_REQUIRED)
                        result.diagnostic_code = code
                        return result
                elif required or code != "job_page_no_apply_control":
                    result = self._end("needs_human", "Accès au formulaire de candidature non vérifié.")
                    result.diagnostic_code = code
                    return result
            if self.apply_navigation and not self.apply_entry and not self.touched and not self.apply_link_followed:
                navigated, result = await self._open_posting_apply(required=kind == "job_page", has_form=self._has_form(snap))
                if result:
                    return result
                if navigated:
                    continue
            if self.autonomous and self.apply_entry and kind == "job_page" and not getattr(self.autonomous, "core", False):
                result = self._end("needs_human", "Page d’offre, pas de formulaire de candidature vérifié.")
                result.diagnostic_code = "job_page_no_apply_control"
                return result
            if (not self.autonomous and self.pre_steps < PRE_STEP_CAP and not self.touched and not self.app_touched
                    and (kind == "job_page" or kind in ACCOUNT_KINDS
                         or (plan.actions and all(a.op == "click" and a.click_role == "cookies" for a in plan.actions)))):
                self.pre_steps += 1   # a pure pre-data step (cookie banner, open_form, account page): the budget is for the application
                self.consumed -= 1
            if plan.note and not (self._inspection_only or self.mode == "supervised" or self.autonomous):
                log.note = f"{log.note}; {self._r(plan.note[:300])}"
            errors = any(f.error for f in snap.fields)
            account_outcome = self.account_try if self.trusting and not self.app_touched and not self.final_clicked else None
            if account_outcome and account_outcome[1] != self._account_address(snap.url):
                account_outcome = None
            account_text = fold("\n".join((snap.title, snap.text, *(field.error for field in snap.fields if field.error)))) if account_outcome else ""
            rejected_login = bool(account_outcome and account_outcome[0] == "login" and trusting.LOGIN_REJECTED.search(account_text))
            if (kind == "email_verification" and account_outcome and account_outcome[0] == "signup"
                    and trusting.EMAIL_CONFIRM_ERROR.search(account_text) and self._inline_signup(plan, snap)):
                kind = log.page_kind = "account_signup"
                plan = plan.model_copy(update={"page_kind": kind})
            if rejected_login:
                kind = log.page_kind = "account_login"
                plan = plan.model_copy(update={"page_kind": kind})
            retry_signup = False
            if kind in ("other", "closed", "error") and not self.touched and AGGREGATOR_RE.search(fold(plan.note or "")):
                await self._shot(log)  # not the employer's posting at all: the service clears the origin and looks it up again
                return self._end("blocked", f"Lien de l'offre inexploitable : {self._host()}")
            if self.login_unknown_try:  # a login was tried on a site where the candidate has no known account
                self.login_unknown_try = False
                if kind == "account_login" and (rejected_login or errors or await self._banner_error()):
                    if self.trusting:
                        retry_signup = True
                    else:
                        await self._shot(log)
                        host = self._host()
                        q = Question(label=f"Compte requis sur {host} : créer le compte à la main ou donner les identifiants", kind="text")
                        return self._end("needs_input", q.label + ".", [q])

            if self.account_try:  # what the last login / signup click led to
                role, address = self.account_try
                self.account_try = None
                if account_key(address) == account_key(self._account_address(self.page.url)):
                    has_password = any(field.kind == "password" for field in snap.fields)
                    if (kind not in (*ACCOUNT_KINDS, "email_verification", "captcha", "error", "closed", "other") and not has_password
                            and not errors and not (self.trusting and any(
                                f.kind in ("button", "link") and SIGN_IN_WITH_RE.search(fold(f.label)) for f in snap.fields))):
                        self._confirm_account(address)   # past the account pages: the account exists on the site
                    elif role == "signup" and (kind in ACCOUNT_KINDS or has_password) and await self._says_account_exists():
                        if address in self.login_failed:  # logging in with the stored credential already failed here
                            await self._shot(log)
                            q = Question(label=f"Compte déjà existant sur {self._host()} : donner les identifiants ou se connecter à la main", kind="text")
                            return self._end("needs_input", q.label + ".", [q])
                        self.account_exists_hosts.add(address)
                    elif self.trusting and role == "signup" and not errors and (kind == "account_login" or (
                            not has_password and any(f.kind in ("button", "link") and SIGN_IN_WITH_RE.search(fold(f.label))
                                                     for f in snap.fields))):
                        # The creation form is gone and the site shows its sign-in page: the account was created (it may
                        # still await an e-mail verification). Sign in with it instead of creating it again.
                        self.account_exists_hosts.add(address)
                        self.login_failed.discard(address)  # (a login tried before the account existed)
                        log.note += "; compte créé : connexion"

            if kind == "captcha":
                await self._shot(log)
                return self._end("needs_human", "Un CAPTCHA doit être résolu à la main.")
            if kind == "email_verification":
                if self.trusting:
                    with contextlib.suppress(Exception):
                        if await trusting.enter_emailed_code(self, snap, resolve):
                            continue
                explicit_verification = self.trusting and trusting.offers_verification(snap)
                login_first = bool(account_outcome and account_outcome[0] == "signup" and not errors
                                   and not explicit_verification and trusting.login_actions(snap))
                if not (login_first or (self.trusting and self.email_login_tried and not explicit_verification)) \
                        and await self._verify_email(snap, resolve):
                    if self.trusting:  # the account the link verified exists: sign in with it, never create it again
                        address = self._account_address(self.page.url)
                        self.account_exists_hosts.add(address)
                        self.login_failed.discard(address)
                    continue
                if self.trusting and not self.email_login_tried and not self.app_touched and not self.final_clicked:
                    # No verification link or code came (many sites only send a "your account was created" notice): the
                    # account exists, so start again from the posting and sign in with it. Once per run.
                    self.email_login_tried = True
                    address = self._account_address(self.page.url)
                    self.account_exists_hosts.add(address)
                    self.login_failed.discard(address)
                    log.note += "; connexion avec le compte créé" if login_first else "; aucun lien de vérification reçu : connexion avec le compte créé"
                    with contextlib.suppress(Exception):
                        await self.page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
                    continue
                return await self._email_hold(log)
            if rejected_login and self.email_login_tried:
                await self._shot(log)
                return self._account_stop("account_login_failed")
            if (kind == "closed" or self._error_page_gone(kind, snap)
                    or (kind in (*ACCOUNT_KINDS, "other", "error", "job_page") and self._offer_gone(snap))):
                # (also: an account step the site bounced to its home with a "this offer no longer exists" banner)
                await self._shot(log)
                return self._gone_result()
            if kind == "confirmation":
                await self._shot(log)
                if (self.trusting and self.app_touched and not self.final_clicked and self.last_sent is not None
                        and (not self._has_form(snap) or self._form_gone(snap)) and not any(f.error for f in snap.fields)):
                    # The click before was not announced as the final one, yet the page that followed is the receipt:
                    # record the send now.
                    with contextlib.suppress(Exception):
                        if self.on_submit_click is not None:
                            await self.on_submit_click()
                        self.final_clicked = True
                        self.diagnostics.control = "final_submit"
                        self.diagnostics.receipt_kind, self.diagnostics.receipt_reason = "first_party_confirmation", "confirmation_text"
                        return self._end("submitted", "Candidature envoyée.")
                if self.mode == "submit" and self.final_clicked:
                    return self._end("submitted", "Candidature envoyée.") if self._confirmed(snap, self.last_sent) else self._unconfirmed()
                return self._end("failed", "Page de confirmation inattendue")
            if self.final_clicked:
                await self._shot(log)
                return self._unconfirmed()
            if self.mode != "submit" and (kind in ACCOUNT_KINDS or (
                    self._inspection_only and self.apply_link_followed and any(field.kind == "password" for field in snap.fields))):
                await self._shot(log)
                result = self._account_handoff()
                if self._inspection_only and self.apply_link_followed:
                    result.diagnostic_code = "dry_account_gate"
                    result.final_url = origin(self.page.url)
                return result

            if self.current_step:
                await self._supervised_checkpoint()
            # Core guard mode: the entry step may not have bound the landing, but an account page on a trusted host (the
            # employer, its ATS, or reached from them) is the same case: create / use the candidate account there.
            account_host_ok = bool(self.autonomous and (self.autonomous.entry_binding or (
                getattr(self.autonomous, "core", False) and self.autonomous.trusted(self.page.url))))
            bound_account = bool(account_host_ok and
                                 (kind in ACCOUNT_KINDS or any(field.kind == "password" for field in snap.fields))
                                 and not self._combined_form(kind, snap))
            signup_page, inline, known_account = False, None, False
            trust = self.trusting
            if bound_account:
                if not trust and (self.app_touched or any(field.kind == "file" for field in snap.fields)):
                    return self._end("needs_human", SUPERVISED_REQUIRED)
                if self.known_login_try:  # the stored credential was just tried and the site shows a password page again: no such account
                    self.known_login_try = False
                    if any(field.kind == "password" for field in snap.fields):
                        address = self._account_address(self.page.url)
                        self.login_failed.add(address)
                        with contextlib.suppress(Exception):
                            if unconfirm := getattr(self.account_known, "unconfirm", None):
                                unconfirm(address)
                known_account = self._account_exists()
                inline = None if known_account else self._inline_signup(plan, snap)
                if kind not in ACCOUNT_KINDS:  # the planner called it otherwise: the page itself says what it is
                    login_form = any(f.kind == "button" and LOGIN_BUTTON_RE.search(fold(f.label)) for f in snap.fields)
                    if inline is None and not login_form and not trust:
                        return self._end("needs_human", SUPERVISED_REQUIRED)
                    if inline is not None or login_form:
                        kind = log.page_kind = "account_signup" if inline is not None else "account_login"
                        plan = plan.model_copy(update={"page_kind": kind})
                signup = [field for field in snap.fields if field.kind in {"button", "link"} and not SOCIAL_RE.search(fold(field.label)) and
                          (SIGNUP_LINK_RE.search(fold(field.label)) or (trust and trusting.norm(field.label) == "inscrivez vous"))
                          and (field.kind == "link" or not field.form)]
                signin = [field for field in snap.fields if field.kind == "link" and re.fullmatch(
                    r"(?:se connecter|connexion|sign[ -]?in|log[ -]?in|anmelden)", fold(field.label).strip())]
                if trust and len(signin) != 1:
                    # A sign-in BUTTON counts too (an account page that toggles between its create and sign-in forms); the
                    # same label twice (header + form) is one control: the one in the form, last in page order.
                    toggles = [field for field in snap.fields if field.kind in {"link", "button"} and re.fullmatch(
                        r"(?:se connecter|connexion|connectez[ -]vous|identifiez[ -]vous|sign[ -]?in|log[ -]?in|anmelden)", fold(field.label).strip())]
                    if len({fold(field.label).strip() for field in toggles}) == 1:
                        signin = [toggles[-1]]
                # The same control twice (header + body) is one control; two different ones are ambiguous.
                one_signup = len({fold(field.label).strip() for field in signup}) == 1
                login = trusting.login_actions(snap) if trust and known_account and not self.app_touched else []
                if login and not rejected_login:
                    kind = log.page_kind = "account_login"
                    plan = plan.model_copy(update={"page_kind": kind, "actions": login, "unknown": []})
                elif retry_signup and inline is None and one_signup and self.signup_link_clicks < 2:
                    self.signup_link_clicks += 1
                    signup.sort(key=lambda field: field.kind != "link")
                    plan = plan.model_copy(update={"actions": [Action(ref=signup[0].ref, op="click", click_role="open_form")], "unknown": []})
                elif retry_signup and inline is None:
                    await self._shot(log)
                    return self._account_stop("account_signup_missing_link")
                elif inline is not None:
                    signup_page = True
                    extra = [action for action in plan.actions if action.op != "click"
                             and action.ref not in {a.ref for a in inline[1]}] if trust else []
                    plan = plan.model_copy(update={"page_kind": "account_signup", "actions": [*extra, *inline[1]]})
                elif trust and kind not in ACCOUNT_KINDS:
                    pass  # a password field on a page the planner calls otherwise: its own plan stands
                elif trust and (
                        (kind == "account_login" and not known_account and (not one_signup or self.signup_link_clicks >= 2))
                        or (kind == "account_signup" and known_account and len(signin) != 1)):
                    signup_page = kind == "account_signup"  # no unambiguous way by code: the planner's own plan stands
                elif kind == "account_login" and not known_account:
                    if not one_signup:
                        return self._account_stop("account_signup_missing_link")
                    if self.signup_link_clicks >= 2:
                        return self._account_stop("account_signup_loop")
                    self.signup_link_clicks += 1
                    signup.sort(key=lambda field: field.kind != "link")
                    plan = plan.model_copy(update={"actions": [Action(ref=signup[0].ref, op="click", click_role="signup")]})
                elif kind == "account_signup" and known_account:
                    if len(signin) != 1:
                        return self._end("needs_human", SUPERVISED_REQUIRED)
                    plan = plan.model_copy(update={"actions": [Action(ref=signin[0].ref, op="click", click_role="open_form")]})
                elif trust:
                    signup_page = kind == "account_signup"
                else:
                    signup_page = kind == "account_signup"
                    byref = {field.ref: field for field in snap.fields}
                    plan = plan.model_copy(update={"actions": [action for action in plan.actions if
                        (action.op == "fill" and action.fact in ACCOUNT_FACTS) or
                        (action.op == "click" and action.click_role == ("login" if kind == "account_login" else "signup")
                         and not (byref.get(action.ref) and SOCIAL_RE.search(fold(byref[action.ref].label))))]})
            self.signup_scope = ({a.ref for a in (inline[1] if trust else plan.actions)}
                                 if bound_account and signup_page and inline is not None else None)
            v = self._validate(plan, snap, resolve)
            self.signup_scope = None
            if trust and bound_account and kind == "account_signup" and v.click is None and not known_account:
                # The account-creation form came back (an error the fills above correct: an unticked consent box...) and the
                # plan has no click: click its one creation button again (ACCOUNT_CLICKS_MAX and the same-click check bound it).
                if (create := self._signup_submit(snap)) is not None:
                    v.click = (create, "signup")
                    v.notes.append("clic de création du compte ajouté")
            if bound_account and not trust:
                v.items = [item for item in v.items if item.act.op in ("fill", "select") and item.used in ACCOUNT_FACTS]
                if signup_page:  # the terms / privacy boxes an account form requires: the owner authorizes ticking them
                    target = v.click[0].form if v.click else ""
                    taken = {item.field.ref for item in v.items}
                    for field in snap.fields:
                        text = fold(f"{field.label} {field.group}")
                        if (field.kind == "checkbox" and field.required and _is_empty(field) and field.ref not in taken
                                and (not target or field.form == target) and SIGNUP_CONSENT_RE.search(text) and not blocked_consent(text)
                                and recruiter_ai_polarity(text) is None):
                            code = "application_privacy" if SIGNUP_PRIVACY_RE.search(text) else "application_terms"
                            v.items.append(_Item(Action(ref=field.ref, op="check"), field, used=code))
            if self.autonomous and kind == "form" and not bound_account and not trust:  # the owner authorizes ticking the REQUIRED privacy / terms box
                taken = {item.field.ref for item in v.items}
                target = v.click[0].form if v.click else ""
                for field in snap.fields:
                    text = fold(f"{field.label} {field.group}")
                    if (field.kind == "checkbox" and field.required and _is_empty(field) and field.ref not in taken
                            and (not target or not field.form or field.form == target)
                            and (_site_consent(field) or SIGNUP_CONSENT_RE.search(text) or ACCEPT_BOX_RE.search(text))
                            and not blocked_consent(text) and not MARKETING_RE.search(text) and recruiter_ai_polarity(text) is None):
                        code = "application_privacy" if SIGNUP_PRIVACY_RE.search(text) else "application_terms"
                        v.items.append(_Item(Action(ref=field.ref, op="check"), field, used=code))
            if not self._inspection_only:
                await self._verify(v)
            if self.current_step:
                await self._supervised_checkpoint()
            if v.items and not self.generic_step and not (_secure_url(self.page.url) and all(_secure_url(self._frame_url(resolve, it.field.ref), self.page.url)
                                                                   for it in v.items)):
                log.note = f"{log.note}; page non sécurisée"
                return self._end("blocked", "Page non sécurisée (HTTPS requis) : aucune donnée n'a été saisie.")
            if v.items and not self.generic_step:  # destination binding, before the first character is typed / file uploaded
                bad = await self._dest_result(resolve, [it.field.ref for it in v.items], v.click[0] if v.click else None)
                if bad is not None:
                    return bad
            handled: set[str] = set()
            approved_choices: dict[str, tuple] = {}
            # Many ATS parse the uploaded CV and re-render (and pre-fill) the form: every other ref of this snapshot would
            # be stale. So a step that uploads AND fills does the uploads alone first, then the page is read again. Once per
            # page URL, so a form that keeps asking for the file cannot loop.
            page_key = urldefrag(self.page.url).url
            uploads_first = [it for it in v.items if it.act.op in ("upload_cv", "upload_letter")]
            split = (not self._inspection_only and self.mode != "supervised" and SPLIT_UPLOADS
                     and bool(uploads_first) and len(uploads_first) < len(v.items)
                     and page_key not in self.upload_split)
            if split:
                self.upload_split.add(page_key)
                v.items = uploads_first
            if self._inspection_only:
                handled.update(it.field.ref for it in v.items)
                value_dependent = {it.field.ref for it in v.items if it.llm}
                untested = [_label(field) for field in snap.fields if field.kind not in ("button", "link", "password")
                            and _label(field) and (field.ref not in handled or field.ref in value_dependent)]
                if untested:
                    log.note += self._r(" ; Champs non testés : " + ", ".join(dict.fromkeys(untested)))[:500]
            if self.mode == "submit" and v.items:  # policy gate BEFORE the first field interaction of the step (a tick / choice can
                bad = await self._gate(n)           # trigger page behaviour): nothing is entered when it says no
                if bad is not None:
                    return bad
            dirty = False
            pending_choices = {item.field.ref for item in v.items if item.field.kind in CHOICE_KINDS}
            for it in v.items if not self._inspection_only else ():
                self.diagnostics.control = ("upload" if it.act.op.startswith("upload_") else
                                            "choice" if it.field.kind in CHOICE_KINDS else "text")
                try:
                    if self.generic_step or self.autonomous:
                        target = await self._el(resolve, it.field.ref)
                        if target is None:
                            raise ManualField()
                        if self.generic_step:
                            await self._supervised_checkpoint(target)
                    elif self.mode == "supervised":
                        bad = await self._dest_result(resolve, [it.field.ref], None)
                        if bad is not None:
                            return bad
                        destination = it.field.destination
                        urls = (self.page.url, self._frame_url(resolve, it.field.ref), destination[0] if destination else "")
                        expected = urlparse(self.url)
                        if any(not clean_url(url, navigation=True) or not _secure_url(url)
                               or (urlparse(url).scheme, urlparse(url).netloc) != (expected.scheme, expected.netloc)
                               for url in urls):
                            return self._end("blocked", "Destination du champ non vérifiée : saisie annulée.")
                        target = await self._el(resolve, it.field.ref)
                        if target is None:
                            return self._end("blocked", "Champ introuvable : saisie annulée.")
                    try:
                        await self._live_decisions(snap, it.field, approved_choices, pending=pending_choices, applying=it)
                    except _DecisionHold:
                        if self.generic_step:
                            await self._supervised_checkpoint(target)
                        raise
                except _DecisionHold as hold:
                    return hold.result
                except ManualField:
                    pending_choices.discard(it.field.ref)
                    v.notes.append(f"{_label(it.field)} : à remplir à la main")
                    v.questions.append(_question(it.field))
                    continue
                except Exception:
                    return self._end("blocked", "Impossible de vérifier les décisions du formulaire.")
                pending_choices.discard(it.field.ref)
                self.touched = True  # (before the action: a handler may react while it runs; also closes the dry-run guard)
                if plan.page_kind not in ACCOUNT_KINDS and not (it.fact and (it.fact.secret or it.fact.key.startswith("account_"))):
                    self.app_touched = True
                try:
                    if trust:
                        shown = await trusting.execute(self, it, target, resolve, snap)
                    else:
                        shown = await self._execute(it, target if self.autonomous or self.mode == "supervised" else resolve(it.field.ref), snap, log)
                except _DecisionHold as hold:
                    return hold.result
                except ManualField:
                    v.notes.append(f"{_label(it.field)} : à remplir à la main")
                    v.questions.append(_question(it.field))
                    continue
                except _Skip as e:
                    if it.used == "confirmed_form_choice":
                        return self._end("blocked", "Le choix confirmé n’a pas pu être appliqué : action annulée.")
                    v.notes.append(f"{_label(it.field)} : {e}")
                    if it.field.required:
                        v.questions.append(_question(it.field))
                    if it.act.op in ("upload_cv", "upload_letter"):
                        v.upload_failed = v.upload_failed or self._upload_question(it.act.op)
                        v.upload_refs.add(it.field.ref)
                    shown = None
                except _Dirty as e:
                    if it.used == "confirmed_form_choice":
                        return self._end("blocked", "Le choix confirmé n’a pas pu être vérifié : action annulée.")
                    v.notes.append(f"{_label(it.field)} : {e}")
                    dirty = True
                    if not any(q.ref == it.field.ref for q in v.questions):
                        v.questions.append(_question(it.field))
                    shown = None
                except Exception as e:
                    if it.used == "confirmed_form_choice":
                        return self._end("blocked", "Le choix confirmé n’a pas pu être appliqué : action annulée.")
                    v.notes.append(f"{_label(it.field)} : échec ({type(e).__name__})")
                    if it.field.required:
                        v.questions.append(_question(it.field))
                    if it.act.op in ("upload_cv", "upload_letter"):
                        v.upload_failed = v.upload_failed or self._upload_question(it.act.op)
                        v.upload_refs.add(it.field.ref)
                    shown = None
                if shown is not None:
                    try:
                        await self._record_choice(snap, it, shown, approved_choices)
                    except _DecisionHold as hold:
                        if self.mode == "dry_run" and self.blocked_requests:
                            break
                        return hold.result
                    except Exception:
                        if self.mode == "dry_run" and self.blocked_requests:
                            break
                        return self._end("blocked", "Impossible de vérifier la sélection appliquée.")
                    if it.act.op in ("upload_cv", "upload_letter"):
                        self.uploaded_files.append((self._host(), _norm(_label(it.field)), fold(Path(shown).stem)))
                        if trust and it.act.op == "upload_cv":
                            self.uploaded_cv_names.setdefault(self._host(), set()).add(trusting.norm(Path(shown).name))
                    handled.add(it.field.ref)
                    log.filled.append(FilledField(label=self._r(_label(it.field)), op=it.act.op, fact=it.used or it.act.fact,
                                                  value="Choix confirmé" if it.used in {"confirmed_form_choice", "eeo_race_ethnicity"}
                                                  else self._r(shown),
                                                  inferred=it.inferred, basis=self._r(it.basis)))
                if self.mode == "dry_run" and self.blocked_requests:  # (also when the aborted navigation made the action fail)
                    break
            if self.mode == "dry_run" and self.blocked_requests:
                await self._shot(log)
                return self._auto_submit_result(log)
            if v.upload_failed:  # a file that could not be put on the site: nothing is clicked in this step (nothing was sent)
                log.note = self._r("; ".join(([log.note] if log.note else []) + v.notes))[:900]
                await self._shot(log)
                q = Question(label=v.upload_failed, kind="file")
                qs = [x for x in v.questions if x.ref not in v.upload_refs] + [q]  # (the upload's own question is replaced by this one)
                return self._end("needs_input", "Informations manquantes : " + ", ".join(x.label for x in qs) + ".", qs)
            if dirty:  # a wrong radio may be selected and cannot be undone: nothing else happens in this step (no click)
                log.note = self._r("; ".join(([log.note] if log.note else []) + v.notes))[:900]
                await self._shot(log)
                return self._end("needs_input", "Informations manquantes : " + ", ".join(q.label for q in v.questions) + ".", v.questions)
            if split:
                log.note = self._r("; ".join(x for x in (log.note, "CV déposé : relecture de la page avant de remplir le reste") if x))[:900]
                await self._settle()
                await asyncio.sleep(UPLOAD_SETTLE_S)
                continue

            # "This page holds candidate data" is read from the LIVE page, whoever filled it (a saved draft): the clicked control is a
            # submit control of a form that already holds a value / a selected file. Such a click is treated like any post-data click.
            self.page_data, live, info = False, None, None
            if v.click:
                live = await self._el(resolve, v.click[0].ref)
                info = await self._ctl(live) if live is not None else None
                # (a whole-label cookie-banner button is exempt; everything else - any control type, with or without a <form> - counts)
                self.page_data = not self._is_cookie_click(v.click[0], v.click[1]) and (
                    await self._holds_data(live) or (self.mode == "dry_run" and live is not None and await self._holds_choice(live)))
                if self.page_data and plan.page_kind not in ACCOUNT_KINDS:
                    self.app_touched = True
            attach = False
            if v.click and self.uploaded and live is not None and v.click[0].kind in ("button", "link"):
                lab = fold(v.click[0].label)
                try:  # the button that ATTACHES the file just chosen: in the small container of a file input holding a file
                    ai = await live.evaluate(_ATTACH_JS)
                    attach = bool(ai["filled"]) and (bool(UPLOAD_CONFIRM_RE.search(lab)) or (bool(ai["small"]) and not NEXT_OR_SEND_RE.search(lab)))
                except Exception:
                    attach = False
            if attach:
                self.page_data = False
            post_data = (self.touched or self.page_data) and not attach
            role = self._effective_role(*v.click, snap) if v.click else None
            if attach:
                role = "attach"
            elif (v.click and plan.page_kind == "account_login" and role in ("signup", "login") and v.click[0].kind in ("link", "button")
                    and SIGNUP_LINK_RE.search(fold(v.click[0].label))
                    and (v.click[0].kind == "link" or v.click[0].form not in {x.form for x in snap.fields if x.kind == "password"})):
                role = "open_form"  # the way to the account-creation page, not a login: a plain navigation
            if role is not None and role != v.click[1]:  # type: ignore[index]
                v.notes.append(f"clic « {v.click[0].label} » reclassé en {role}")  # type: ignore[index]
            final = (not attach) and self._is_final(v.click[0], role) if v.click else False  # type: ignore[arg-type]
            if bound_account and not trust and (final or role not in {None, "open_form", "login", "signup"}):
                return self._end("needs_human", SUPERVISED_REQUIRED)
            # Whatever the planner called it, a click that is final or made after data entry may send the form: the unanswered
            # required questions are checked first. Only a pre-data cookies / open_form click is exempt.
            cookie_banner = bool(v.click) and self._is_cookie_click(v.click[0], role) and not final  # a real banner button, by its label
            guarded = v.click is None or role in GUARDED or final or (post_data and not cookie_banner)
            if self.mode == "submit" and v.click and (post_data or final):
                bad = await self._dest_result(resolve, [], v.click[0])
                if bad is not None:
                    return bad
            if trust and final and (foreign := trusting.foreign_dial(self, snap)):
                # The page about to be sent shows another country's calling code in front of the candidate's number.
                result = self._end("needs_input", f"Indicatif téléphonique affiché ({foreign}) différent de celui du candidat : envoi retenu.")
                result.diagnostic_code = "foreign_dial"
                return result
            if trust:
                # Stop only for what nobody could answer: the planner's own unknowns and the answers that failed. Required
                # fields it left alone are the site's to judge (its validation is the truth, not our reading of "required").
                blocking, soft = trusting.open_questions(self, plan, snap, v, handled, v.click[0] if v.click else None) if guarded else ([], [])
                # (each re-read follows a step that filled something: a one-page form whose answers bring new fields with
                # them needs more than two of them)
                if (blocking or soft) and handled and self.replans.get(page_key, 0) < 5:
                    self.replans[page_key] = self.replans.get(page_key, 0) + 1  # progress was made: read the page again first
                    log.note = self._r("; ".join(x for x in (log.note, *v.notes, "relecture avant le clic") if x))[:900]
                    await self._shot(log)
                    continue
                chosen = {f.ref for f in snap.fields if f.kind in ("select", "combobox", "radio", "checkbox")} & handled
                if final and chosen and self.presend.get(page_key, 0) < 2:
                    # A choice made in this step may have brought a new field with it ("Autre, précisez :"): the page is read
                    # again before the click that sends it.
                    self.presend[page_key] = self.presend.get(page_key, 0) + 1
                    log.note = self._r("; ".join(x for x in (log.note, *v.notes, "relecture avant l'envoi") if x))[:900]
                    await self._shot(log)
                    continue
                v.questions = [*blocking, *soft] if blocking else []
                self.sent_empty = {(_norm(q.label), q.kind) for q in soft} if not blocking else set()
                if soft and not blocking:
                    v.notes.append("champs requis laissés vides : " + ", ".join(q.label[:40] for q in soft[:6]))
            elif guarded or self.mode == "supervised":
                questions = list(v.questions)
                questions += [q for q in plan.unknown if q.required]
                # Required fields of ANOTHER <form> than the one being sent (e.g. the "déjà inscrit ?" login box next to the
                # application form) are not ours to fill: only the clicked button's form (and fields outside any form) count.
                target = v.click[0].form if v.click else ""
                mine = [f for f in snap.fields if not target or not f.form or f.form == target]
                # The ad itself has no application form: its required inputs are the site's own widgets (job-alert box, newsletter,
                # rating, search), never questions of the candidacy.
                ad = plan.page_kind == "job_page"
                questions += [_question(f) for f in mine if not ad and f.required and f.kind not in ("button", "link")
                              and f.ref not in handled and _is_empty(f) and not (self.mode == "dry_run" and f.kind == "password")
                              and not self._file_satisfied(f, snap)]
                questions += [_question(f) for f in mine if f.ref not in handled and self._open_decision(f, snap)]
                seen: set[str] = set()
                uniq = []
                for q in questions:
                    key = q.ref or _norm(q.label)
                    if key not in seen:
                        seen.add(key)
                        uniq.append(q)
                v.questions = uniq
            if v.notes:
                log.note = self._r("; ".join(([log.note] if log.note else []) + v.notes))[:900]
            await self._shot(log)
            if v.questions and (guarded or self._inspection_only or self.mode == "supervised"):
                result = self._end("needs_input", "Informations manquantes : " + ", ".join(q.label for q in v.questions) + ".",
                                   v.questions)
                if bound_account and kind == "account_signup":
                    result.diagnostic_code = "account_signup_field_unfilled"
                if self._inspection_only and self.apply_link_followed and kind == "form" and self._has_form(snap):
                    result.diagnostic_code = "dry_apply_form_needs_input"
                return result
            if self._inspection_only:
                result = self._end("blocked", DRY_UNTESTED)
                if self.apply_link_followed and kind == "form" and self._has_form(snap):
                    result.diagnostic_code = "dry_apply_form_found"
                elif self.apply_link_followed and (kind in ACCOUNT_KINDS or any(f.kind == "password" for f in snap.fields)):
                    result.diagnostic_code = "dry_account_gate"
                    result.final_url = origin(self.page.url)
                return result
            if self.mode == "supervised":
                return self._end("ready", "Vérifiez le formulaire. Vous seul pouvez cliquer sur Envoyer.")

            if v.click is None:
                idle = 0 if handled else idle + 1
                if trust and idle and not handled:  # the planner only retried fields the program cannot set: tell it to move on
                    self.stuck_fields = list(dict.fromkeys(_label(it.field)[:60] for it in v.items
                                                           if any(n.startswith(f"{_label(it.field)} : ") for n in v.notes)))[:6]
                if (trust and idle and not self.app_touched and self.account_clicks and not self.back_to_posting
                        and urldefrag(self.page.url).url != urldefrag(self.url).url):
                    # Signed in, and the site left us on its candidate home with nothing to do: go back to the posting, once.
                    self.back_to_posting = True
                    log.note += "; connecté : retour à l'offre"
                    with contextlib.suppress(Exception):
                        await self.page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
                    idle = 0
                    continue
                if (trust and idle and not self.app_touched and not self.print_view_left
                        and PRINT_VIEW_RE.search(urlparse(self.page.url).path.lower())):
                    self.print_view_left = True
                    log.note += "; version imprimable de l'offre : retour à l'offre"  # (it has no apply control: open the normal view)
                    with contextlib.suppress(Exception):
                        await self.page.goto(PRINT_VIEW_RE.sub("/emploi-", self.page.url, count=1), wait_until="domcontentloaded", timeout=30_000)
                    idle = 0
                    continue
                if idle >= 2:
                    if trust and not self.app_touched and await self._page_says_gone():
                        return self._gone_result()  # (said on a disabled apply button or below the excerpt the planner read)
                    return self._end("failed", "Aucune action possible sur cette page.")
                continue
            idle = 0
            field, planned = v.click
            if self.rehearse and (final or role in ("login", "signup") or (post_data and not (
                    self.rehearse_ok and self.rehearse_ok.search(field.label)))):  # dev rehearsal on a live page: fake data is never sent
                return self._end("ready", f"Répétition : clic « {field.label[:60]} » ({role}) non effectué.")
            if v.click and not attach and self.last_click_rec == (urldefrag(self.page.url).url, _norm(field.label)):
                loop_sig = [f for f in snap.fields if f.kind not in ("button", "link") and f.ref not in handled and not self._file_satisfied(f, snap)
                            and (f.error or (f.required and _is_empty(f) and (not field.form or not f.form or f.form == field.form)))]
                if loop_sig:  # the same click on the same page with the same problem still showing: stop instead of burning the steps
                    qs = [_question(f) for f in loop_sig]  # every field still in error / empty, by name
                    result = self._end("needs_input", "Informations manquantes : " + ", ".join(q.label for q in qs) + ".", qs)
                    if bound_account:
                        result.diagnostic_code = "account_login_failed" if kind == "account_login" else "account_signup_field_unfilled"
                    return result
            if bound_account and role in ("login", "signup") and self.account_clicks >= ACCOUNT_CLICKS_MAX + (3 if trust else 0):
                # (trusting mode: a refused password, "this account exists", the way to the sign-in form, then the sign-in)
                await self._shot(log)  # the site keeps sending the account page back: stop with a diagnosable reason
                return self._account_stop("account_not_accepted")
            if self.mode == "dry_run" and not attach and not self._dry_may_click(field, role, snap):  # type: ignore[arg-type]
                return self._dry_end(plan, field, role, planned)  # type: ignore[arg-type]
            if self.mode == "dry_run":  # "post-data" is decided from the PAGE too: a pre-filled form must not be submitted
                if (live is not None and info is None) or self.page_data:
                    log.note = self._r(f"{log.note}; formulaire déjà rempli sur le site : clic refusé")[:900]
                    return self._end("ready", f"Formulaire déjà rempli sur le site : le clic « {field.label[:60]} » n'a pas été effectué, "
                                              "rien n'a été envoyé (simulation).")
                if post_data or final:
                    bad = await self._dest_result(resolve, [], field)
                    if bad is not None:
                        return bad
            if not trust and planned == "cookies" and re.search(r"\b(?:accepter|accepte|accept|agree|allow|autoriser)\b", _norm(field.label)) \
                    and not re.search(r"\b(?:sans|without|refuser|reject)\b", _norm(field.label)):
                return self._end("needs_input", "Préférences de cookies à confirmer sur le site.")
            if self.mode == "submit" and (final or post_data):
                # EVERY click after candidate data was entered (account pages, cookie buttons, "Continuer"...) may send it
                if final and self.submit_clicks >= MAX_SUBMIT_CLICKS:
                    return self._unconfirmed()
                bad = await self._gate(n)
                if bad is not None:
                    return bad
            target = await self._el(resolve, field.ref)
            if not await self._same_target(target, field) and not (trust and (live_now := await live_button(target) if target is not None else None)
                                                                   and live_now[1] and live_now[2]):
                log.note = f"{log.note}; « {field.label[:40]} » a changé depuis la lecture : clic annulé"
                continue
            async def recheck():
                try:
                    if _site_consent(field) and not trust:  # (trusting mode: the candidate's standing authorization covers it)
                        raise _DecisionHold(self._end("needs_input", "Consentement à donner sur le site."))
                    await self._live_decisions(snap, field, approved_choices)
                except _DecisionHold:
                    raise
                except Exception:
                    raise _DecisionHold(self._end("blocked", "Impossible de vérifier les décisions du formulaire.")) from None

            recheck_click = guarded or attach or post_data or bool(info and info.get("submit"))
            if recheck_click:
                try:
                    await recheck()
                except _DecisionHold as hold:
                    return hold.result
            before = (self.final_clicked, self.submit_clicks, self.nav_pending, self.pending, self.last_sent, self.nav_ref, self.final_awaiting)
            if final:
                try:
                    if self.on_submit_click is not None and not self.autonomous:
                        await self.on_submit_click()
                except Exception:
                    return self._end("blocked", "Envoi bloqué : enregistrement de l'envoi impossible.")
                self.final_clicked = True  # from here on the application MAY have been sent, whatever happens next
                self.final_awaiting = True
                self.submit_clicks += 1
                self.sent_url = urldefrag(self.page.url).url
            elif self.mode == "submit" and post_data:
                try:
                    if self.on_nav_click is not None:
                        await self.on_nav_click()
                except Exception:
                    return self._end("blocked", "Envoi bloqué : enregistrement du clic impossible.")
                if (role in {"login", "signup"} and self.autonomous) if trust else (
                        self.app_touched or (self.autonomous and role in {"next", "login", "signup"})):
                    # (trusting mode: after a "next" / in-page click the planner simply reads the page that follows; a dialog,
                    # a validation error or the same step is not a "possible send")
                    self.nav_pending = True  # recorded: from here on the application MAY have been sent, until a snapshot shows the form again
            if self.mode == "submit" and (final or self.app_touched) and not attach:
                keys = {(_norm(_label(f)), f.kind) for f in snap.fields if f.ref in handled}
                if not keys:
                    keys = {(_norm(_label(f)), f.kind) for f in snap.fields if f.kind not in ("button", "link")}
                keys = {k for k in keys if k[0]}
                self.pending = self.last_sent = _Sent(final=final, fields=keys, button=_norm(field.label))
            if self.nav_pending and not final:
                self.nav_ref = (urldefrag(self.page.url).url, {(_norm(_label(f)), f.kind) for f in snap.fields if f.kind not in ("button", "link")},
                                _norm(field.label))
            if self.mode == "submit" and (final or post_data) and self.on_progress is not None:
                log.clicked = self._r(field.label)
                try:
                    await self.on_progress(self._progress())
                except Exception:  # the record could not be kept: no click, and the engine state goes back to "nothing dispatched"
                    log.clicked = ""
                    (self.final_clicked, self.submit_clicks, self.nav_pending, self.pending, self.last_sent, self.nav_ref,
                     self.final_awaiting) = before
                    return self._end("blocked", "Envoi bloqué : enregistrement de la progression impossible.")
            if self.mode == "dry_run":
                self.click_form = None
                with contextlib.suppress(Exception):
                    self.click_form = await target.evaluate(_FORM_INFO_JS)  # type: ignore[union-attr]
            if attach:
                self.attach_pending = True
                self.attach_done = True
            else:
                self.last_click_rec = (urldefrag(self.page.url).url, _norm(field.label))
            if bound_account and role in ("login", "signup"):
                self.account_try = (role, self._account_address(self.page.url))
                self.account_clicks += 1
                if role == "signup":
                    self.signed_up.add(self.account_try[1])
            if plan.page_kind == "account_login" and role == "login" and (self.account_known is not None or (trust and bound_account)):
                with contextlib.suppress(Exception):
                    known = self._account_exists()
                    self.login_unknown_try = not known
                    self.known_login_try = known and bool(self.autonomous and (self.autonomous.entry_binding or trust))
            self.diagnostics.control = ("final_submit" if final else "login_otp" if role == "login" else
                                        "account_creation" if role == "signup" else "navigation")
            try:
                if self.autonomous:
                    async def dispatch_check():
                        await recheck()
                        try:
                            await self.autonomous.target(target, button=True, final=final)
                            if check_click := getattr(self.autonomous, 'check_click', None):
                                await check_click(target, field, role, final)
                                self.autonomous.recheck()
                            if final and self.on_submit_click is not None:
                                await self.on_submit_click()
                                self.diagnostics.receipt_reason = "submit_unverified"
                            self.autonomous.arm_click(scripted=final or role in {"next", "login", "signup"}, account=bound_account)
                        except (ManualField, ApplyError):
                            raise _DecisionHold(self._end("needs_human", SUPERVISED_REQUIRED)) from None
                    await self._click(target, recheck=dispatch_check)
                elif recheck_click:
                    await self._click(target, recheck=recheck)  # type: ignore[arg-type]
                else:
                    await self._click(target)  # type: ignore[arg-type]
            except _DecisionHold as hold:
                log.clicked = ""
                if self.autonomous:
                    (self.final_clicked, self.submit_clicks, self.nav_pending, self.pending, self.last_sent, self.nav_ref,
                     self.final_awaiting) = before
                return hold.result
            except _Dispatched as e:  # the click may have fired: after data / final it is "possibly sent", never retried
                if trust and not final:
                    # Not the send click: it was dispatched and the page moved or reloaded under it (an upload-confirm button,
                    # a "next" that navigates). Read the page that follows instead of failing the run.
                    log.clicked = self._r(field.label)
                    log.note += "; clic interrompu par la page : relecture"
                    await self._after_click()
                    with contextlib.suppress(Exception):
                        await self.autonomous.after_click(self.page, observing=False)
                    continue
                if self.mode == "submit" and (final or post_data):
                    return self._unconfirmed()
                if self.autonomous and not self.app_touched and not post_data and GONE_PATH_RE.search(urlparse(self.page.url).path.lower()):
                    await self._shot(log)  # the click took the page to the site's own "expired / error" page: the ad is gone
                    return self._gone_result()
                return self._end("failed", f"Clic impossible sur « {_label(field)} » ({e}).")
            log.clicked = self._r(field.label)
            self.replans.pop(page_key, None)  # (a multi-step form keeps its URL: each step gets its own re-reads)
            self.presend.pop(page_key, None)
            self.combo_options.clear()  # (and the lists of the next step are other lists, whatever their labels)
            await self._after_click()
            if self.autonomous:
                if await self.autonomous.after_click(self.page, observing=self.final_awaiting or self.nav_pending):
                    self.nav_pending = False
        if self.autonomous:
            return self._end("needs_human", "Trop d'étapes : limite atteinte.")
        return self._unconfirmed() if self.final_clicked or self.nav_pending else self._end("failed", "Trop d'étapes : limite atteinte.")


async def _default_goto(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)


async def run_application(page: Page, *, url: str, facts: FactSheet, planner: Planner, uploads: Uploads, mode: Mode, out_dir: Path,
                          job_title: str = "", company: str = "", password: Callable[[str], str] | None = None,
                          before_submit: Callable[[], Awaitable[list[str]]] | None = None,
                          on_submit_click: Callable[[], Awaitable[None]] | None = None,
                          on_nav_click: Callable[[], Awaitable[None]] | None = None, verifier: AnswerVerifier | None = None,
                          allowed_hosts: set[str] | None = None, host_allowed: Callable[[str], bool] | None = None,
                          max_steps: int = 12, goto: Callable[[Page, str], Awaitable[None]] | None = None,
                          on_progress: Callable[[RunResult], Awaitable[None]] | None = None, captcha_wait_s: float = 0.0,
                          account_known: Callable[[str], bool] | None = None, choices: dict[str, int] | None = None,
                          supervised_attempt: Attempt | None = None,
                          before_field: Callable[[str], None] | None = None, current_step: bool = False, generic_step: bool = False,
                          attended_accounts: bool = False,
                          supervised_widgets: bool = False,
                          live_mutation_check: Callable[[ElementHandle | Locator | None], Awaitable[None]] | None = None,
                          autonomous=None, autonomous_attempt: Attempt | None = None,
                          diagnostics: DispatchDiagnostics | None = None, apply_navigation=None, apply_entry=None,
                          email_verification=None) -> RunResult:
    """Inspect or fill a form; only a bound, guarded unattended flow can submit.

    Legacy dispatch callbacks alone do not authorize live sends. Dry callers
    should supply a fresh headless context, as the apply service does.
    """
    url = clean_url(url, navigation=True)
    if not url:
        return RunResult(status="blocked", message="Lien de candidature non autorisé.")
    if autonomous and (mode != "submit" or not autonomous_attempt or not autonomous_attempt.autonomous
            or autonomous_attempt.url != url or not uploads.cv_bytes
            or hashlib.sha256(uploads.cv_bytes).hexdigest() != autonomous_attempt.pdf_sha256
            or before_submit is None or not binding_active(autonomous_attempt)):
        return RunResult(status="blocked", message=UNSUPPORTED_DISPATCH)
    if mode == "supervised":
        if (not supervised_attempt or supervised_attempt.url != url or not uploads.cv_bytes
                or hashlib.sha256(uploads.cv_bytes).hexdigest() != supervised_attempt.pdf_sha256 or before_submit is None
                or not binding_active(supervised_attempt)):
            return RunResult(status="blocked", message="Réservation supervisée absente ou périmée.")
        policy_check = before_submit

        async def recheck() -> list[str]:
            problems = await policy_check()
            if not problems and not binding_active(supervised_attempt):
                return ["Réservation supervisée périmée."]
            return problems

        before_submit = recheck
    if host_allowed is None and allowed_hosts:
        hosts = {h.lower() for h in allowed_hosts}
        host_allowed = lambda h: h.lower() in hosts  # noqa: E731
    flow = _Run(page, url=url, facts=facts, planner=planner, uploads=uploads, mode=mode, out_dir=out_dir,
                      job_title=job_title, company=company, password=password, before_submit=before_submit,
                      on_submit_click=on_submit_click, on_nav_click=on_nav_click, verifier=verifier,
                      host_allowed=host_allowed, max_steps=max_steps, goto=goto, on_progress=on_progress,
                      captcha_wait_s=captcha_wait_s, account_known=account_known, choices=choices)
    flow.before_field = before_field
    flow.apply_navigation = apply_navigation or apply_entry
    flow.apply_entry = apply_entry
    flow.current_step = current_step
    flow.generic_step = generic_step
    flow.live_mutation_check = live_mutation_check
    flow.autonomous = autonomous
    flow.email_verification = email_verification if mode == 'submit' else None
    if diagnostics is not None:
        flow.diagnostics = diagnostics
    flow.attended_accounts = attended_accounts
    flow.supervised_widgets = (mode == "supervised" or autonomous is not None) and supervised_widgets
    return await flow.run()
