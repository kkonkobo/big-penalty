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
import random
import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, quote_plus, urldefrag, urlparse

from patchright.async_api import ElementHandle, Locator, Page
from pydantic import BaseModel

from jobhunt.apply.adapters import Attempt
from jobhunt.apply.answer_check import (AnswerCheckItem, AnswerVerifier, as_verdict, derived_from, fold, is_hard_fact)
from jobhunt.apply.missing_answers import capture, fact_key
from jobhunt.apply.planner import Planner, facts_payload
from jobhunt.apply.prompt_rules import decision_key, device_type, international_calling_code, technical_key
from jobhunt.apply.scoped_choices import field_choices
from jobhunt.apply.snapshot import Resolver, live_button, take_snapshot
from jobhunt.apply.store import binding_active
from jobhunt.apply.types import (Action, Fact, FactSheet, FilledField, FormField, Mode, PageSnapshot, Question, RunResult,
                                 RunStatus, StepLog, StepPlan)
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
    r"application (?:has been |was |is |had been )?(?:successfully )?(?:received|submitted|sent|recorded)|"
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
DIAL_OPTION_RE = re.compile(r"^\s*\(?\+\d{1,4}")
TEXT_KINDS = {"text", "email", "tel", "url", "number", "date", "password", "textarea", "combobox"}
PROFILE_KINDS = {"text", "email", "tel", "url", "number", "date", "textarea"}  # a pre-filled value there = a form with a profile
GUARDED = {"next", "submit", "signup", "login"}
FILLABLE = {"text", "email", "tel", "textarea", "select", "radio", "checkbox"}  # "the form is still there" = a file or >= 3 of these
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
ACTION_TIMEOUT_MS = 8000
MAX_SUBMIT_CLICKS = 1  # at most ONE final dispatch per run: a field error is never proof that nothing was sent
IDLE_TIMEOUT_MS = 3000   # network-idle wait of a step (never fatal)
TYPE_DELAY_MS = (15, 45)  # per-keystroke delay range
CLICK_WAIT_S = 0.5       # lets a popup / navigation start after a click
RELEASE_WAIT_S = 0.2     # dry run: pause between "pages sent to about:blank" and removing the request guard
RELEASE_PAGES = True     # dry run: send the pages to about:blank at the end (tests that inspect the final DOM switch it off)
CLICK_ATTRIBUTION_S = 4.0  # a navigation aborted within this delay after a click of ours is attributed to that click
SPLIT_UPLOADS = True     # uploads get a step of their own, then the page is read again (tests with scripted plans switch it off)
UPLOAD_SETTLE_S = 2.5    # lets a CV parser re-render / pre-fill the form after an upload
OBSERVE_S = 12.0         # after a post-data click: how long the engine only observes the page before giving up ("not confirmed")
OBSERVE_POLL_S = 2.0     # re-snapshot interval of that observation
FILE_OPENER_RE = re.compile(r"(?:choisir|choose|select|selectionner|parcourir|browse|joindre|telecharger|televerser|upload|importer|deposer|ajouter|attach|add)\b"
                            r".{0,20}\b(?:fichier|file|document|cv|resume|curriculum|piece)|^\s*(?:parcourir|browse)")  # folded label of a button that opens a file dialog
UPLOAD_CONFIRM_RE = re.compile(r"^\s*(?:ajouter|add|attach|joindre|telecharger|televerser|upload|importer|charger|envoyer le fichier|aggiungi|carica|allega|"
                               r"hinzufugen|hochladen|anhangen|anadir|adjuntar|subir|cargar|adicionar|anexar|toevoegen|uploaden)\b")
NEXT_OR_SEND_RE = re.compile(r"postuler|envoyer|soumettre|valider|confirmer|terminer|finish|enregistrer|save|suivant|continuer|next|continue|submit|apply|send|"
                             r"weiter|senden|avanti|siguiente|seguinte|volgende|inviare|enviar|\bok\b")
SIGNUP_LINK_RE = re.compile(r"creer (?:un|votre|mon) compte|s'?inscrire|inscription|nouveau candidat|pas encore de compte|create (?:an )?account|sign ?up|"
                            r"register|new user|konto erstellen|registrieren|nouvel utilisateur|premiere visite")
AGGREGATOR_RE = re.compile(r"agregat|aggregat|app ?store|google play|application mobile|mobile app|app[- ]only|lien inexploitable|not the employer")
CAPTCHA_POLL_S = 2.0     # re-snapshot interval while waiting for a human to solve a CAPTCHA
PRE_STEP_CAP = 8         # pure pre-data steps (cookie banner, open_form, account pages) that do not use the max_steps budget
SECRET_LOG = "•••"
AUTO_SUBMIT_NOTE = "soumission automatique bloquée"
NO_SHOT_NOTE = "capture désactivée après saisie du mot de passe"
TOKEN_RE = re.compile(r"submit_clicked", re.I)   # bookkeeping token of the service: never produced by the engine / planner / page
FIELD_ERROR_MSG = ("Le site signale une erreur de saisie après le clic : vérifier sur le site de l'entreprise si la candidature "
                   "est partie")
UNCONFIRMED_MSG = "Envoi non confirmé : vérifier sur le site de l'entreprise"
UNSUPPORTED_DISPATCH = "Envoi bloqué : aucun adaptateur d'envoi et de reçu vérifié pour ce site."
DRY_UNTESTED = "Simulation non testée : inspection hors ligne, sans saisie, fichier ni clic sur le site."
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


class _Dispatched(Exception):
    """The single activation of a click raised: it may have fired. Never retried."""


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
    return (f.form, f.kind, f.name, f.label, f.wording, f.group, f.required, f.multiple, f.autocomplete,
            f.destination, f.choice_destination, f.choice_options, f.options)


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
    if fact_key(_question(f)) == "phone_calling_code":
        return True
    if f.kind == "select" and f.options and 2 * sum(bool(DIAL_OPTION_RE.match(o.label)) for o in f.options) >= len(f.options):
        return any(x.kind == "tel" and x.group == f.group for x in snap.fields)
    return False


def pick_variant(fact: Fact, f: FormField, maxlength: int | None = None, pattern: str | None = None) -> str:
    """Exact fact value, or the variant that best fits the field (placeholder shape, maxlength, pattern). Default: the value."""
    cands = [fact.value or "", *fact.variants]
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
        self.verifier, self.host_allowed = verifier, host_allowed
        self.max_steps, self.goto = max_steps, goto
        self.on_progress, self.captcha_wait_s = on_progress, captcha_wait_s
        self.account_known = account_known   # host -> does the credential store hold an account for it (None: not told to the planner)
        self.choices = choices or {}
        self.choice_fields: dict[str, tuple[Question, str, list[int]]] = {}
        self.questions_by_ref: dict[str, Question] = {}
        self.login_unknown_try = False       # a login was attempted on a host without a known account
        self.uploaded = False                # a file was set by the engine in this run
        self.uploaded_files: list[tuple[str, str, str]] = []  # (host, normalized label, file name stem in lowercase) of every upload done
        self.attach_done = False             # an upload-confirm click was made
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
        self.blocked_requests: list[str] = []   # dry run: page-initiated navigations aborted by the request guard
        self.secrets: set[str] = set()
        self.upload_split: set[str] = set()  # page URLs where the uploads were already done in a step of their own
        self.no_shot_noted = False
        self.final_url = ""
        self._letter: Letter | None = None
        self._letter_done = False

    # ------------------------------------------------------------ secrets / redaction
    def _r(self, s: str) -> str:
        """Mask the secrets and strip the service's bookkeeping token."""
        for sec in sorted(self.secrets, key=len, reverse=True):
            # as typed, URL-encoded (query string), half-decoded by a script ("+" for a space), HTML-escaped
            for form in {sec, quote(sec, safe=""), quote_plus(sec), quote(sec), sec.replace(" ", "+"), html.escape(sec), html.escape(sec, quote=False)}:
                s = re.sub(re.escape(form), SECRET_LOG, s, flags=re.I)
        return TOKEN_RE.sub("", s)

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
        masked = bool(self.secrets) and pub.model_dump_json() != snap.model_dump_json()
        for field in pub.fields:
            if technical_key(field.label, field.kind, group=field.group, autocomplete=field.autocomplete) == "credentials":
                field.value = SECRET_LOG if field.value else ""
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
            return self.password(urlparse(frame_url).hostname or "") or None
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
        return RunResult(status=status, message=self._r(message), questions=bound)

    async def _shot(self, log: StepLog) -> None:
        if log.screenshot:
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
        for attempt in range(3):
            try:
                snapshot, resolve = await take_snapshot(self.page)
                choices = {}
                repeated = set()
                self.questions_by_ref = {}
                for field in snapshot.fields:
                    question = _question(field)
                    self.questions_by_ref[field.ref] = question
                    if (question.reason == "technical" or question.category == "ambiguous_country"
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
        if self.account_known is None:
            return self.facts
        try:
            known = bool(self.account_known(self._host()))
        except Exception:
            return self.facts
        extra = Fact(key="account_exists", label="Un compte existe déjà sur ce site pour le candidat", value="oui" if known else "non")
        return FactSheet(facts=[*self.facts.facts, extra])

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
        v = _Validated()
        by_ref = {f.ref: f for f in snap.fields}
        seen: set[str] = set()
        clicks: list[tuple[FormField, str]] = []
        for field in snap.fields:
            entry = self.choice_fields.get(field.ref)
            if not entry or entry[0] != _question(field) or field_choices(self.page.url, field) != entry[1:]:
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
            for f in snap.fields:  # a password / confirmation field the planner left alone gets the same secret
                if f.kind == "password" and f.ref not in seen and _is_empty(f):
                    frame_url = self._frame_url(resolve, f.ref) if resolve else self.page.url
                    if self._secret(frame_url) is not None:
                        seen.add(f.ref)
                        v.items.append(_Item(Action(ref=f.ref, op="fill", fact=pw.key), f, pw, used=pw.key, frame_url=frame_url))
        if clicks:
            v.click = clicks[-1]
            if len(clicks) > 1:
                v.notes.append("plusieurs clics proposés : seul le dernier est conservé")
        v.items = [item for item in v.items if item.used != "confirmed_form_choice" or (
            v.click and v.click[0].native_submit and v.click[0].form == item.field.form
            and v.click[0].destination == item.field.choice_destination)]
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
    def _dial_option(f: FormField, fact: Fact) -> int:
        """Only a unique exact option for the country calling code of a confirmed international number."""
        code = international_calling_code(fact.value or "")
        matches = [index for index, option in enumerate(f.options)
                   if re.findall(r"\+\s*[0-9]+", option.label) == [code]] if code else []
        if not fact.exact or len(matches) != 1:
            return -1
        option = f.options[matches[0]]
        if f.choice_options and sum(value == option.value or code in re.findall(r"\+\s*[0-9]+", label)
                                    for value, label, _, _ in f.choice_options) != 1:
            return -1
        return matches[0]

    def _confirmed_dial_selection(self, field: FormField, snap: PageSnapshot) -> bool:
        phone = self.facts.get("phone")
        if not phone or field.kind != "select" or field.multiple or not _dial_selector(field, snap):
            return False
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
        if field.kind not in CHOICE_KINDS:
            return (field.kind != "password" and _question(field).category == "credentials"
                    and (field.required or active))
        if _site_consent(field):
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
                              pending: set[str] = frozenset(), applying: _Item | None = None) -> None:
        """Read the whole live decision scope without retagging the original dispatch target."""
        fresh, _ = await take_snapshot(self.page, read_only=True)
        current = next((field for field in fresh.fields if field.live_ref == target.ref), None)
        if (fresh.url != snap.url or fresh.has_captcha or current is None or current.form != target.form
                or current.kind != target.kind or current.destination != target.destination or current.native_submit != target.native_submit
                or current.name != target.name or current.label != target.label or current.wording != target.wording
                or current.group != target.group or current.autocomplete != target.autocomplete):
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
            if field.kind in CHOICE_KINDS:
                expected = approved.get(field.live_ref)
                if expected is not None and expected == _choice_state(field):
                    continue
                if ((original and _choice_state(original) != _choice_state(field)) or expected is not None
                        or (not original and (field.required or not _is_empty(field)))):
                    raise _DecisionHold(self._end("blocked", "Une question ou sa sélection a changé : action annulée."))
            awaiting_action = field.live_ref in pending and (_is_empty(field) or field.live_ref == current.live_ref)
            if not awaiting_action and self._open_decision(field, fresh):
                questions.append(_question(original) if original else _question(field).model_copy(update={"ref": ""}))
        if questions:
            raise _DecisionHold(self._end("needs_input", "Décisions à vérifier avant de continuer.", questions))

    async def _record_choice(self, snap: PageSnapshot, item: _Item, shown: str, approved: dict[str, tuple]) -> None:
        field = item.field
        if field.kind not in CHOICE_KINDS:
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
        if _question(f).category == "credentials" and k != "password":
            return drop("mot de passe et champ incompatibles" if a.fact == "account_password"
                        else "identifiant technique à saisir sur le site")
        if op in ("upload_cv", "upload_letter"):
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
        exact_key = fact_key(_question(f))
        dial = k in ("select", "combobox") and _dial_selector(f, snap)
        choice = decision_key(f.label) or decision_key(f.group)
        if k in CHOICE_KINDS and _unlabeled(f):
            return drop("case ou liste sans libellé lisible : jamais cochée ni choisie")
        if a.fact == "consent_required" or (k in CHOICE_KINDS and _site_consent(f)):
            return drop("consentement à donner sur le site")
        if choice and exact_key not in {"work_authorization", "needs_visa_sponsorship"} and not (
                choice == "ambiguous_country" and (dial or exact_key == "country")):
            return drop("décision à confirmer sur le site")
        if a.fact == "work_authorization" or exact_key == "work_authorization":
            if exact_key != "work_authorization" or a.fact != exact_key or op != "fill" or a.inferred:
                return drop("autorisation de travail : question ou format non confirmé")
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

        def need_verdict(answer: str, backed: bool) -> _Item | None:
            if item.hard and (a.inferred or fact is None):
                return drop("fait dur (état civil, adresse, pièces, permis...) : jamais deviné")
            item.llm, item.answer, item.backed = True, answer, backed
            return item

        if op in ("check", "uncheck"):
            if op == "check" and not f.required and ((fact is not None and fact.key == "consent_optional") or MARKETING_RE.search(f.label)):
                return drop("case facultative non cochée")
            return need_verdict("true" if op == "check" else "false", False)
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
        if not pend:
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
    async def _execute(self, it: _Item, loc: Locator | ElementHandle, snap: PageSnapshot, log: StepLog) -> str:
        """Carry out one validated action. Returns the (redacted) value for the log; raises _Skip when it cannot be done."""
        a, f = it.act, it.field
        T = ACTION_TIMEOUT_MS
        if self.mode == "supervised":
            if a.op == "upload_cv" and f.kind == "file" and self.uploads.cv_bytes is not None:
                await loc.set_input_files({"name": "CV.pdf", "mimeType": "application/pdf", "buffer": self.uploads.cv_bytes}, timeout=T)
                self.uploaded = True
                return "CV.pdf"
            if f.kind not in ("select", "text", "email", "tel", "url", "number", "date", "textarea") or a.op not in ("fill", "select"):
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
            await loc.select_option(value=o.value, timeout=T) if o.value else await loc.select_option(label=o.label, timeout=T)
            shown = str(await loc.evaluate("e => { const o = e.selectedOptions[0]; return o ? (o.label || o.textContent).trim() : ''; }") or "")
            if _norm(shown) != _norm(o.label):  # read back what the control really holds: log THAT, and never keep another choice
                if it.used == "confirmed_form_choice":
                    raise _Dirty("la sélection confirmée n’a pas été retenue")
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
            await self._type(loc, f, pw)
            return SECRET_LOG
        text = it.value or ""
        if fact is not None and fact.exact and fact.variants:
            if isinstance(loc, ElementHandle):
                ml, pat = await loc.evaluate("e => [e.getAttribute('maxlength'), e.getAttribute('pattern')]")
            else:
                ml = await loc.get_attribute("maxlength", timeout=T)
                pat = await loc.get_attribute("pattern", timeout=T)
            text = pick_variant(fact, f, int(ml) if ml and ml.isdigit() else None, pat)
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
        if f.kind != "textarea":
            text = text.replace("\r", " ").replace("\n", " ")  # never a stray Enter in a single-line field
        if self.mode == "supervised" or f.kind in ("date", "number") or len(text) > 120:
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
        hit = [o for o, t in found if option_matches_fact(t, want)]
        if not hit:
            await self._abandon_combobox(loc)
            raise _Skip("option introuvable dans la liste")
        await hit[0].click(timeout=T)
        await human_pause(0.1, 0.4)
        live = await self._combo_value(loc)
        if not option_matches_fact(live, want):
            await self._abandon_combobox(loc)
            raise _Skip("valeur lue différente de la réponse validée")
        return live

    @staticmethod
    async def _trial(el: ElementHandle, timeout: int) -> bool:
        """Is the element clickable? (Playwright's trial: every actionability check, NO event dispatched.)"""
        try:
            await el.click(trial=True, timeout=timeout)
            return True
        except Exception:
            return False

    async def _click(self, target: ElementHandle | Locator, *, recheck=None) -> None:
        """Activate the exact element EXACTLY ONCE. Actionability is learnt with non-dispatching trials (retried after scrolling
        into view); the dispatch is then one normal click (trial passed) or, when the element stays covered, one `e.click()`.
        A dispatch that raises is never retried: it may have fired -> `_Dispatched`."""
        if isinstance(self, _Run):  # (unit tests call this unbound)
            self.click_window = time.monotonic() + CLICK_ATTRIBUTION_S
        el = target.first if isinstance(target, Locator) else target
        ok = await _Run._trial(el, ACTION_TIMEOUT_MS)
        if not ok:
            with contextlib.suppress(Exception):
                await el.scroll_into_view_if_needed(timeout=2000)
            with contextlib.suppress(Exception):
                await el.evaluate("e => e.scrollIntoView({block: 'center', inline: 'center'})")
            ok = await _Run._trial(el, ACTION_TIMEOUT_MS // 2)
        if recheck is not None:
            await recheck()
        try:
            if ok:
                await el.click(timeout=ACTION_TIMEOUT_MS)
            else:  # still covered (a sticky widget / overlay): activate the SAME element directly, once
                await el.evaluate("e => e.click()")
        except Exception as e:
            raise _Dispatched(type(e).__name__) from e

    async def _after_click(self) -> None:
        await asyncio.sleep(CLICK_WAIT_S)
        usable: list[Page] = []
        for p in self.opened:  # a popup / new tab is followed only when it is an http(s) page
            with contextlib.suppress(Exception):
                await p.wait_for_load_state("domcontentloaded", timeout=5000)
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
        if self.mode not in ("dry_run", "supervised"):
            res = self._end("blocked", UNSUPPORTED_DISPATCH)
            res.finished_at = datetime.now()
            return res
        if self.mode == "supervised" and (self.before_submit is None or self.uploads.cv_bytes is None):
            return self._end("blocked", UNSUPPORTED_DISPATCH)
        return await self._run_page()

    @property
    def _inspection_only(self) -> bool:
        return self.mode == "dry_run"

    def _account_handoff(self) -> RunResult:
        return self._end("blocked", DRY_UNTESTED)

    async def _run_page(self) -> RunResult:
        """Page lifecycle for callers that have already authorized their interaction policy."""
        ctx = self.page.context
        offline = False

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
                if offline or (self._inspection_only and request.method not in {"GET", "HEAD"}):
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
                    if self._inspection_only:
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
                await (self.goto or _default_goto)(self.page, self.url)
                if self._inspection_only:
                    offline = True
                    await ctx.set_offline(True)
                res = await self._loop()
            except LLMError as e:
                res = self._end("failed", f"Planificateur indisponible ({e.code}).")
            except Exception as e:  # message: exception type only, never page content
                res = self._end("failed", f"Erreur technique ({type(e).__name__}).")
                if self.steps:
                    await self._shot(self.steps[-1])
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
        res.submit_clicked = self.final_clicked or self.nav_pending
        res.finished_at = datetime.now()
        self._scrub(res)
        return res

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
        deadline = loop.time() + OBSERVE_S
        asked: tuple[str, str] | None = None
        errors_seen = False
        while True:
            has_form = self._has_form(snap)
            if self.last_sent is not None and not self.last_sent.final and not has_form:
                self.final_clicked = True  # the form of a navigation click is gone: it may have been sent
            if snap.has_captcha:
                await self._shot(log)
                return self._end("needs_human", "Un CAPTCHA doit être résolu à la main.")
            # a field error / error banner is NEVER proof that nothing was sent, and takes precedence over "continuation": it only
            # changes the message of the final (unconfirmed) result
            errors_seen = errors_seen or (has_form and any(f.error for f in snap.fields if f.kind not in ("button", "link"))) \
                or await self._banner_error()
            if has_form and self.nav_pending and not errors_seen and self._distinct_step(snap):
                self.nav_pending = False
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
                    await self._shot(log)
                    return self._end("needs_email", plan.note or "Le site attend la validation d'un lien envoyé par e-mail.")
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
            if loop.time() >= deadline:
                await self._shot(log)
                return self._end("failed", FIELD_ERROR_MSG) if errors_seen else self._unconfirmed()
            await asyncio.sleep(OBSERVE_POLL_S)
            snap, resolve = await self._snapshot()

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

    def _unconfirmed(self) -> RunResult:
        return self._end("failed", UNCONFIRMED_MSG)

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

    async def _loop(self) -> RunResult:
        idle = 0
        while self.consumed < self.max_steps:
            n = len(self.steps) + 1
            self.consumed += 1
            await self._settle()
            if self.blocked_requests:  # a page script navigated by itself after the last step
                return self._auto_submit_result(self.steps[-1] if self.steps else None)
            snap, resolve = await self._snapshot()
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
            sent, self.pending = self.pending, None
            if sent is not None and not sent.final and not self._has_form(snap):
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
                if any(f.error for f in snap.fields if f.kind not in ("button", "link")) or await self._banner_error():
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
            plan = await self.planner.plan(pub, self._planner_facts(), job_title=self.job_title, company=self.company,
                                           history=self.steps[:-1], mode=self.mode)
            kind = log.page_kind = plan.page_kind
            if (self.pre_steps < PRE_STEP_CAP and not self.touched and not self.app_touched
                    and (kind == "job_page" or kind in ACCOUNT_KINDS
                         or (plan.actions and all(a.op == "click" and a.click_role == "cookies" for a in plan.actions)))):
                self.pre_steps += 1   # a pure pre-data step (cookie banner, open_form, account page): the budget is for the application
                self.consumed -= 1
            if plan.note:
                log.note = f"{log.note}; {self._r(plan.note[:300])}"
            errors = any(f.error for f in snap.fields)
            if kind in ("other", "closed", "error") and not self.touched and AGGREGATOR_RE.search(fold(plan.note or "")):
                await self._shot(log)  # not the employer's posting at all: the service clears the origin and looks it up again
                return self._end("blocked", f"Lien de l'offre inexploitable : {self._host()}")
            if self.login_unknown_try:  # a login was tried on a site where the candidate has no known account
                self.login_unknown_try = False
                if kind == "account_login" and (errors or await self._banner_error()):
                    await self._shot(log)
                    host = self._host()
                    q = Question(label=f"Compte requis sur {host} : créer le compte à la main ou donner les identifiants", kind="text")
                    return self._end("needs_input", q.label + ".", [q])

            if kind == "captcha":
                await self._shot(log)
                return self._end("needs_human", "Un CAPTCHA doit être résolu à la main.")
            if kind == "email_verification":
                await self._shot(log)
                return self._end("needs_email", plan.note or "Le site attend la validation d'un lien envoyé par e-mail.")
            if kind == "closed":
                await self._shot(log)
                return self._end("blocked", "Offre clôturée")
            if kind == "confirmation":
                await self._shot(log)
                if self.mode == "submit" and self.final_clicked:
                    return self._end("submitted", "Candidature envoyée.") if self._confirmed(snap, self.last_sent) else self._unconfirmed()
                return self._end("failed", "Page de confirmation inattendue")
            if self.final_clicked:
                await self._shot(log)
                return self._unconfirmed()
            if self.mode != "submit" and kind in ACCOUNT_KINDS:
                await self._shot(log)
                return self._account_handoff()

            v = self._validate(plan, snap, resolve)
            await self._verify(v)
            if v.items and not (_secure_url(self.page.url) and all(_secure_url(self._frame_url(resolve, it.field.ref), self.page.url)
                                                                   for it in v.items)):
                log.note = f"{log.note}; page non sécurisée"
                return self._end("blocked", "Page non sécurisée (HTTPS requis) : aucune donnée n'a été saisie.")
            if v.items:  # destination binding, before the first character is typed / file uploaded
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
            if self.mode == "submit" and v.items:  # policy gate BEFORE the first field interaction of the step (a tick / choice can
                bad = await self._gate(n)           # trigger page behaviour): nothing is entered when it says no
                if bad is not None:
                    return bad
            dirty = False
            pending_choices = {item.field.ref for item in v.items if item.field.kind in CHOICE_KINDS}
            for it in v.items if not self._inspection_only else ():
                try:
                    if self.mode == "supervised":
                        reasons = await self.before_submit()
                        if reasons:
                            return self._end("blocked", " ; ".join(reasons))
                        bad = await self._dest_result(resolve, [it.field.ref], None)
                        if bad is not None:
                            return bad
                        destination = it.field.destination
                        urls = (self.page.url, self._frame_url(resolve, it.field.ref), destination[0] if destination else "")
                        expected = urlparse(self.url)
                        if any(not _secure_url(url) or (urlparse(url).scheme, urlparse(url).netloc) != (expected.scheme, expected.netloc)
                               for url in urls):
                            return self._end("blocked", "Destination du champ non vérifiée : saisie annulée.")
                        target = await self._el(resolve, it.field.ref)
                        if target is None:
                            return self._end("blocked", "Champ introuvable : saisie annulée.")
                    await self._live_decisions(snap, it.field, approved_choices, pending=pending_choices, applying=it)
                except _DecisionHold as hold:
                    return hold.result
                except Exception:
                    return self._end("blocked", "Impossible de vérifier les décisions du formulaire.")
                pending_choices.discard(it.field.ref)
                self.touched = True  # (before the action: a handler may react while it runs; also closes the dry-run guard)
                if plan.page_kind not in ACCOUNT_KINDS and not (it.fact and (it.fact.secret or it.fact.key.startswith("account_"))):
                    self.app_touched = True
                try:
                    shown = await self._execute(it, target if self.mode == "supervised" else resolve(it.field.ref), snap, log)
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
                        return hold.result
                    except Exception:
                        return self._end("blocked", "Impossible de vérifier la sélection appliquée.")
                    if it.act.op in ("upload_cv", "upload_letter"):
                        self.uploaded_files.append((self._host(), _norm(_label(it.field)), fold(Path(shown).stem)))
                    handled.add(it.field.ref)
                    log.filled.append(FilledField(label=self._r(_label(it.field)), op=it.act.op, fact=it.used or it.act.fact,
                                                  value="Choix confirmé" if it.used == "confirmed_form_choice" else self._r(shown),
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
            # Whatever the planner called it, a click that is final or made after data entry may send the form: the unanswered
            # required questions are checked first. Only a pre-data cookies / open_form click is exempt.
            cookie_banner = bool(v.click) and self._is_cookie_click(v.click[0], role) and not final  # a real banner button, by its label
            guarded = v.click is None or role in GUARDED or final or (post_data and not cookie_banner)
            if self.mode == "submit" and v.click and (post_data or final):
                bad = await self._dest_result(resolve, [], v.click[0])
                if bad is not None:
                    return bad
            if guarded or self.mode == "supervised":
                questions = list(v.questions)
                questions += [q for q in plan.unknown if q.required]
                # Required fields of ANOTHER <form> than the one being sent (e.g. the "déjà inscrit ?" login box next to the
                # application form) are not ours to fill: only the clicked button's form (and fields outside any form) count.
                target = v.click[0].form if v.click else ""
                mine = [f for f in snap.fields if not target or not f.form or f.form == target]
                questions += [_question(f) for f in mine if f.required and f.kind not in ("button", "link")
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
                return self._end("needs_input", "Informations manquantes : " + ", ".join(q.label for q in v.questions) + ".",
                                 v.questions)
            if self._inspection_only:
                return self._end("blocked", DRY_UNTESTED)
            if self.mode == "supervised":
                return self._end("ready", "Vérifiez le formulaire. Vous seul pouvez cliquer sur Envoyer.")

            if v.click is None:
                idle = 0 if handled else idle + 1
                if idle >= 2:
                    return self._end("failed", "Aucune action possible sur cette page.")
                continue
            idle = 0
            field, planned = v.click
            if v.click and not attach and self.last_click_rec == (urldefrag(self.page.url).url, _norm(field.label)):
                loop_sig = [f for f in snap.fields if f.kind not in ("button", "link") and f.ref not in handled and not self._file_satisfied(f, snap)
                            and (f.error or (f.required and _is_empty(f) and (not field.form or not f.form or f.form == field.form)))]
                if loop_sig:  # the same click on the same page with the same problem still showing: stop instead of burning the steps
                    qs = [_question(f) for f in loop_sig]  # every field still in error / empty, by name
                    return self._end("needs_input", "Informations manquantes : " + ", ".join(q.label for q in qs) + ".", qs)
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
            if planned == "cookies" and re.search(r"\b(?:accepter|accepte|accept|agree|allow|autoriser)\b", _norm(field.label)) \
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
            if not await self._same_target(target, field):
                log.note = f"{log.note}; « {field.label[:40]} » a changé depuis la lecture : clic annulé"
                continue
            async def recheck():
                try:
                    if _site_consent(field):
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
                    if self.on_submit_click is not None:
                        await self.on_submit_click()
                except Exception:
                    return self._end("blocked", "Envoi bloqué : enregistrement de l'envoi impossible.")
                self.final_clicked = True  # from here on the application MAY have been sent, whatever happens next
                self.final_awaiting = True
                self.submit_clicks += 1
            elif self.mode == "submit" and post_data:
                try:
                    if self.on_nav_click is not None:
                        await self.on_nav_click()
                except Exception:
                    return self._end("blocked", "Envoi bloqué : enregistrement du clic impossible.")
                if self.app_touched:
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
            if plan.page_kind == "account_login" and role == "login" and self.account_known is not None:
                with contextlib.suppress(Exception):
                    self.login_unknown_try = not self.account_known(self._host())
            try:
                if recheck_click:
                    await self._click(target, recheck=recheck)  # type: ignore[arg-type]
                else:
                    await self._click(target)  # type: ignore[arg-type]
            except _DecisionHold as hold:
                log.clicked = ""
                return hold.result
            except _Dispatched as e:  # the click may have fired: after data / final it is "possibly sent", never retried
                if self.mode == "submit" and (final or post_data):
                    return self._unconfirmed()
                return self._end("failed", f"Clic impossible sur « {_label(field)} » ({e}).")
            log.clicked = self._r(field.label)
            await self._after_click()
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
                          supervised_attempt: Attempt | None = None) -> RunResult:
    """Inspect `url` or fill a reserved, attended form. Never submit it.

    Legacy dispatch callbacks do not authorize generic live sends. Dry callers
    should supply a fresh headless context, as the apply service does.
    """
    if mode == "supervised":
        if (not supervised_attempt or supervised_attempt.adapter or supervised_attempt.url != url or not uploads.cv_bytes
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
    return await _Run(page, url=url, facts=facts, planner=planner, uploads=uploads, mode=mode, out_dir=out_dir,
                      job_title=job_title, company=company, password=password, before_submit=before_submit,
                      on_submit_click=on_submit_click, on_nav_click=on_nav_click, verifier=verifier,
                      host_allowed=host_allowed, max_steps=max_steps, goto=goto, on_progress=on_progress,
                      captcha_wait_s=captcha_wait_s, account_known=account_known, choices=choices).run()
