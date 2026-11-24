"""Planner-trusting field handling of autonomous runs (guard mode `core`).

Owner decision (2026-11-20): maximize sends. The deterministic validation layers of the engine (label regexes, exact-fact
lists, the answer verifier, the live re-read of every decision before each action) stopped every real form before a
single field was filled. Here the planner's mapping is executed as given: every field it answers from a fact, or by a
sensible deduction from the profile, is filled; the run stops only for an answer nobody knows.

What stays by code:
  - the owner's standing rules (required privacy / terms boxes ticked, marketing boxes left alone, "a relative works
    here?" -> No, the declared ethnicity, "prefer not to say" on other diversity questions);
  - hard facts (birth date, address, permits, criminal record...) are never invented: they need a known fact;
  - passwords only ever go into password fields, from the credential store;
  - the guard itself (`autonomous.target`): data is only typed into / sent from pages of the employer or its ATS.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import re
import secrets
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urldefrag, urlsplit

from jobhunt.apply.answer_check import fold, is_hard_fact
from jobhunt.apply.autonomous import blocked_consent
from jobhunt.apply.prompt_rules import DIAL_COUNTRY_NAMES, decision_key, french_phone, international_calling_code, recruiter_ai_polarity
from jobhunt.apply.types import Action, FormField, PageSnapshot, StepPlan

if TYPE_CHECKING:
    from jobhunt.apply.engine import _Item, _Run, _Validated

# Decisions the owner took once for every application (2026-11-19 / 20): shown to the planner as a fact, so a consent it
# covers is acted on instead of being sent back as a question.
AUTHORIZATIONS_LABEL = "Autorisations permanentes déjà données par le candidat (décisions prises : ne jamais les redemander)"
AUTHORIZATIONS = (
    "Pour toute candidature, le candidat a explicitement autorisé d'avance : accepter les politiques de confidentialité, mentions "
    "d'information, conditions d'utilisation et traitements de données nécessaires à la candidature ; fermer ou accepter les fenêtres de "
    "confidentialité et de cookies (refuser si possible, sinon accepter) ; consentir aux vérifications d'antécédents obligatoires "
    "demandées par l'employeur (répondre Oui) ; certifier l'exactitude des informations ; s'engager à respecter la confidentialité "
    "(ne pas divulguer d'informations confidentielles de tiers ou d'anciens employeurs : répondre Oui, j'accepte) ; créer un compte candidat. "
    "Refusé d'avance : newsletters, alertes emploi, viviers de talents, partage avec des partenaires.")
YES = frozenset({"oui", "yes", "ja", "o", "y", "true", "vrai", "si"})
NO = frozenset({"non", "no", "nein", "n", "false", "faux"})
_OPTION_JS = ("e => [...e.querySelectorAll('[role=option], option, li')].filter(o => o.getClientRects().length)"
              ".map(o => (o.innerText || o.textContent || '').trim().replace(/\\s+/g, ' ')).filter(Boolean).slice(0, 200)")
# What a listbox popup renders as: the options a combobox opened, wherever the page mounted them.
_POPUP_OPTIONS = "[role=option]:visible, [role=listbox] li:visible, [role=menuitemradio]:visible, [role=menuitem]:visible"


class ScriptedThenLLM:
    """Planner of a trusting run on a site that has a scripted (adapter) planner. The scripted plan stands when it detects
    a CAPTCHA / e-mail verification or when it ends with a click on a page that is not an application form (the entry and
    account clicks it was written for). Everything else - form steps above all - is planned by the LLM planner."""

    def __init__(self, scripted, llm):
        self.scripted, self.llm = scripted, llm

    async def plan(self, snapshot, facts, **context):
        first = await self.scripted.plan(snapshot, facts, **context)
        if first.page_kind in ("captcha", "email_verification"):
            return first
        if first.page_kind != "form" and any(action.op == "click" for action in first.actions):
            return first
        return await self.llm.plan(snapshot, facts, **context)


def _engine():
    from jobhunt.apply import engine  # noqa: PLC0415 - the engine imports this module lazily too

    return engine


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold(text or "")).strip()


def posting_to_home(posting: str, landed: str) -> bool:
    source, target = urlsplit(posting), urlsplit(landed)
    return (source.hostname == target.hostname and target.path.lower() == "/accueil.aspx"
            and re.fullmatch(r"/(?:offre-de-emploi|job)/(?:offre|job)-[^/]+_\d+\.aspx", source.path, re.I) is not None)


def login_actions(snap: PageSnapshot) -> list[Action]:
    engine = _engine()
    for button in snap.fields:
        if (button.kind != "button" or not button.form or not engine.LOGIN_BUTTON_RE.fullmatch(norm(button.label))
                or engine.SOCIAL_RE.search(norm(button.label))):
            continue
        members = [field for field in snap.fields if field.form == button.form]
        if "$" in button.name:
            prefix = button.name.rpartition("$")[0]
            members = [field for field in members if field.name.rpartition("$")[0] == prefix]
        emails = [field for field in members if field.kind == "email" or (field.kind == "text"
                  and engine.EMAIL_FIELD_RE.search(fold(f"{field.label} {field.name}")))]
        passwords = [field for field in members if field.kind == "password"]
        if len(emails) == len(passwords) == 1:
            return [Action(ref=emails[0].ref, op="fill", fact="account_email"),
                    Action(ref=passwords[0].ref, op="fill", fact="account_password"),
                    Action(ref=button.ref, op="click", click_role="login")]
    return []


LOGIN_REJECTED = re.compile(r"(?:login et le mot de passe n.ont pas ete reconnus|username and password not recognized|"
                            r"(?:e.?mail|identifiant).{0,45}mot de passe (?:sont )?incorrects)")
EMAIL_CONFIRM_ERROR = re.compile(r"\b(?:l.)?e.?mail doit etre confirme\b")
VERIFY_INSTRUCTION = re.compile(r"\b(?:lien|link|code)\b.{0,100}\b(?:activation|verification|confirmation|verify|confirm|envoye|sent)\b|"
                                r"\b(?:activation|verification|confirmation|verify|confirm)\b.{0,40}\b(?:lien|link|code)\b")


def offers_verification(snap: PageSnapshot) -> bool:
    return bool(VERIFY_INSTRUCTION.search(fold(f"{snap.title}\n{snap.text}")) or any(
        field.autocomplete == "one-time-code" or _engine().mail.CODE_LABEL.search(field.label) for field in snap.fields))


def attached_cv(run: _Run, field: FormField, snap: PageSnapshot) -> _Item | None:
    if field.kind != "select" or field.multiple or norm(field.label) not in {"mon cv", "my cv", "my resume"}:
        return None
    names = getattr(run, "uploaded_cv_names", {}).get(urlsplit(snap.url).hostname, set())
    matches = [index for index, option in enumerate(field.options) if option.value and norm(Path(option.label.strip()).name) in names]
    if len(matches) != 1:
        return None
    index = matches[0]
    return _engine()._Item(Action(ref=field.ref, op="select"), field, option_index=index, value=field.options[index].label, used="cv")


# (a careers site opened in another language lists the candidate's country under that language's name)
_COUNTRY_ALIASES = ({"france", "frankreich", "francia", "frankrijk", "franca"},)
# (a form in another language names the candidate's languages in that language: "French" for "Français")
_LANGUAGE_ALIASES = (
    {"francais", "french", "franzosisch", "frances", "francese"}, {"anglais", "english", "englisch", "ingles", "inglese"},
    {"espagnol", "spanish", "spanisch", "espanol"}, {"allemand", "german", "deutsch", "aleman"}, {"italien", "italian", "italiano"},
    {"arabe", "arabic", "arabisch"}, {"portugais", "portuguese", "portugues"}, {"chinois", "chinese", "mandarin"},
    {"neerlandais", "dutch", "nederlands"}, {"russe", "russian"}, {"japonais", "japanese"})


def language_names(want: str | None) -> list[str]:
    """The other names of the language an answer starts with ("Français : langue maternelle" -> french, ...), else []."""
    words = norm(want).split()
    return sorted(next((group for group in _LANGUAGE_ALIASES if words and words[0] in group), set()) - set(words[:1]))


_GENDER_ALIASES = ({"homme", "masculin", "male", "man", "mann", "mannlich", "hombre", "uomo"},
                   {"femme", "feminin", "female", "woman", "frau", "weiblich", "mujer", "donna"})


# (a mobility list in another language: the whole country, under the words each site uses for it)
_AREA_ALIASES = ({"toute la france", "france entiere", "toute france", "partout en france", "all france", "all of france",
                  "whole france", "anywhere in france", "ganz frankreich"},)


def best_option(labels: list[str], want: str | None) -> int:
    """Index of the option an answer means, -1 when none: equality, yes / no and civility aliases, then the ONE option that
    starts with the answer, then the one that contains it (or that the answer contains)."""
    target = norm(want)
    if not target:
        return -1
    folded = [norm(label) for label in labels]
    for index, label in enumerate(folded):
        if label == target:
            return index
    aliases = (*_engine()._ALIASES, *_GENDER_ALIASES, *_COUNTRY_ALIASES, *_AREA_ALIASES)
    for group in aliases:
        if target in group:
            hits = [index for index, label in enumerate(folded) if label in group]
            if len(hits) == 1:
                return hits[0]
    names = set(language_names(target)) | set(target.split()[:1])
    if len(names) > 1:  # a language, under any of its names (the label may add a level: "French (native)")
        hits = [index for index, label in enumerate(folded) if label.split(" ")[0] in names]
        if len(hits) == 1:
            return hits[0]
    first = [index for index, label in enumerate(folded) if label and (label.startswith(target + " ") or target.startswith(label + " "))]
    if len(first) == 1:
        return first[0]
    polar = YES if target in YES else NO if target in NO else None
    if polar:  # "Non, je n'ai aucun proche" for "Non"
        hits = [index for index, label in enumerate(folded) if label.split(" ", 1)[0] in polar]
        if len(hits) == 1:
            return hits[0]
    padded = f" {target} "
    inside = [index for index, label in enumerate(folded) if label and (padded in f" {label} " or f" {label} " in padded)]
    if len(inside) == 1:
        return inside[0]
    if len(first) > 1:
        return min(first, key=lambda index: len(folded[index]))
    return -1


def _text(field: FormField) -> str:
    return fold(f"{field.label} {field.group}")


def _required_consent(field: FormField) -> bool:
    """A REQUIRED box that accepts the site's privacy policy / terms / data processing, or certifies the application. A box
    the site sent back with a validation error ("Please check the box to continue") is required, whatever its markup says."""
    engine = _engine()
    text = _text(field)
    return bool(field.kind == "checkbox" and (field.required or field.error.strip())
                and (engine._site_consent(field) or engine.SIGNUP_CONSENT_RE.search(text) or engine.ACCEPT_BOX_RE.search(text))
                and not blocked_consent(text) and not engine.MARKETING_RE.search(text) and recruiter_ai_polarity(text) is None)


def account_consent(field: FormField, snap: PageSnapshot) -> bool:
    """The privacy / terms / data-processing box of an account-creation form: the site will not create the account without
    it, even when its markup does not flag it required (a large ATS's "Create Account" form answers "Please check the box to
    continue"). Only a box of the form that holds the password fields; never a marketing, job-alert or talent-pool box."""
    engine = _engine()
    text = _text(field)
    forms = {other.form for other in snap.fields if other.kind == "password"}
    return bool(field.kind == "checkbox" and forms and (not field.form or field.form in forms)
                and engine.SIGNUP_CONSENT_RE.search(text) and not blocked_consent(text)
                and not engine.MARKETING_RE.search(text) and recruiter_ai_polarity(text) is None)


_HONEYPOT = re.compile(r"\bfor robots\b|\brobots? only\b|if you(?: a|')re (?:a )?human|\bhoneypot\b|\bbeecatcher\b|"
                       r"leave (?:this (?:field )?)?(?:blank|empty)|ne pas remplir|laisse[rz] (?:ce champ )?vide|nicht ausfullen")


def honeypot(field: FormField) -> bool:
    """A trap input for bots ("Enter website. This input is for robots only, do not enter if you're human"): never filled."""
    return field.kind in ("text", "email", "tel", "url", "textarea", "number") and bool(
        _HONEYPOT.search(fold(f"{field.label} {field.placeholder} {field.name} {field.automation_id}")))


_AGREEMENT = re.compile(r"\b(?:i agree|i certify|i acknowledge|i understand and agree|j'?accepte|je m'?engage|je certifie|je reconnais|ich stimme zu)\b")
_AGREES = re.compile(r"(?:yes|oui|ja)\b.*\b(?:agree|accept\w*|d accord|engage|stimme zu)\b|(?:i agree|j accepte|ich stimme zu)\b")
_DECLINES = re.compile(r"\b(?:do not|don t|not agree|n accepte pas|ne m engage pas|refuse|nicht)\b")
_EXPOSED = re.compile(r"politiquement expos|politically exposed|politisch exponiert")


def declaration_key(field: FormField) -> str | None:
    """Declarations answered "No" unless the candidate recorded otherwise: a relative or acquaintance at the employer (owner
    rule), a conflict of interest through such a relation, a politically exposed person. The recorded fact wins when there is one."""
    text = _text(field)
    if _EXPOSED.search(text):
        return "politically_exposed"
    kinds = {decision_key(field.label), decision_key(field.group)}
    if "relatives" in kinds:
        return "relatives_at_employer"
    if "conflict" in kinds:
        return "conflict_of_interest"
    if "former_employer" in kinds:
        return "former_employer"
    return None


def declared_answer(run: _Run, key: str) -> str:
    fact = run.facts.get(key)
    return fact.value if fact is not None and fact.value else "Non"


def owner_item(run: _Run, field: FormField) -> _Item | None | bool:
    """The owner's standing rule for this field: an item to execute, True when the field is settled as it stands (nothing to
    do, the planner's own action is ignored), None when no rule covers it."""
    engine = _engine()
    item = engine._Item
    if field.kind in ("button", "link", "file", "password"):
        return None
    text = _text(field)
    choice = run._autonomous_decision(field)
    if choice:
        code, index = choice
        if field.kind == "checkbox":
            return item(Action(ref=field.ref, op="check"), field, used=code) if engine._is_empty(field) else True
        if field.options and field.value and norm(field.value) == norm(field.options[index].label):
            return True
        return item(Action(ref=field.ref, op="select"), field, option_index=index, value=field.options[index].label, used=code)
    if field.kind == "checkbox":
        if blocked_consent(text) and not field.required:  # newsletters, job alerts, talent pools, partner sharing: never ticked
            return True if engine._is_empty(field) else item(Action(ref=field.ref, op="uncheck"), field, used="optional_opt_out")
        if _required_consent(field):
            if not engine._is_empty(field):
                return True
            code = "application_privacy" if engine.SIGNUP_PRIVACY_RE.search(text) else "application_terms"
            return item(Action(ref=field.ref, op="check"), field, used=code)
        return None
    if field.kind in ("select", "radio") and field.required and not field.multiple and _AGREEMENT.search(text):
        # "I agree that ... [Yes, I agree | No, I do not agree (and withdraw)]": an undertaking the site requires to apply
        labels = [norm(option.label) for option in field.options]
        agree = [index for index, label in enumerate(labels) if _AGREES.match(label) and not _DECLINES.search(label)]
        if len(agree) == 1 and any(_DECLINES.search(label) for label in labels):
            if field.value and norm(field.value) == labels[agree[0]]:
                return True
            return item(Action(ref=field.ref, op="select"), field, option_index=agree[0], value=field.options[agree[0]].label,
                        used="application_terms")
    key = declaration_key(field)
    if key and field.kind == "combobox" and not field.options and not field.multiple:
        if field.value.strip():
            return True  # already answered (its list is only readable once opened)
        return item(Action(ref=field.ref, op="select", fact=key), field, value=declared_answer(run, key), used=key)
    if key and field.kind in ("select", "radio", "combobox") and field.options and not field.multiple:
        index = best_option([option.label for option in field.options], declared_answer(run, key))
        if index >= 0:
            if field.value and norm(field.value) == norm(field.options[index].label):
                return True
            return item(Action(ref=field.ref, op="select", fact=key), field, option_index=index,
                        value=field.options[index].label, used=key)
    return None


def _creation_bounced(run: _Run, field: FormField, snap: PageSnapshot) -> bool:
    """The account-creation button was just clicked and the same creation form is back (its password emptied or kept) with
    no error on any field: the site refused the password without saying so (its criteria list only changes colour)."""
    engine = _engine()
    last = getattr(run, "last_click_rec", None)
    if not last or any(f.error for f in snap.fields if f.kind not in ("button", "link")):
        return False
    create = engine._Run._signup_submit(snap)
    # (the click must have been made ON this creation form - same number of password fields - not on the link that leads
    # to it from a sign-in form, which carries the same words)
    passwords = sum(f.kind == "password" for f in snap.fields)
    return (create is not None and last == (urldefrag(run.page.url).url, norm(create.label))
            and getattr(run, "last_click_passwords", -1) == passwords)


def check(run: _Run, action: Action, field: FormField, v: _Validated, snap: PageSnapshot, resolve) -> _Item | None:
    """One planner action -> an executable item. Dropped (with a note, and a question when the field is required) only when
    it cannot be carried out at all or would invent a hard fact."""
    engine = _engine()
    item = engine._Item
    op, kind = action.op, field.kind
    drop = lambda why, ask=True: run._drop(v, field, why, ask)  # noqa: E731
    if kind in ("button", "link") and op not in ("upload_cv", "upload_letter"):
        return drop("action de saisie sur un bouton", ask=False)
    if op in ("upload_cv", "upload_letter"):
        if kind == "file" and run._file_satisfied(field, snap):
            return drop("fichier déjà déposé dans cette candidature", ask=False)
        if kind not in ("file", "button", "link"):
            return drop("envoi de fichier sur un champ qui n'est pas un fichier")
        if op == "upload_letter" and not field.required and not (
                field.error or getattr(run, "rejected_submits", 0) or engine.ERROR_TITLE_RE.match(fold(snap.title).strip())):
            # (a letter field not marked required is left alone - until the site refuses the form without it)
            return drop("lettre facultative : ignorée", ask=False)
        return item(action, field, used="cv" if op == "upload_cv" else "letter")
    if op == "letter_text":
        if kind not in ("textarea", "text"):
            return drop("texte de lettre sur un champ inadapté")
        return item(action, field, used="letter")
    fact = run.facts.get(action.fact) if action.fact else None
    if action.fact == "race_ethnicity":
        return drop("origine ethnique : sélection contrôlée uniquement")
    if kind == "file":
        return drop("champ fichier : seul le CV ou la lettre y est déposé")
    if kind == "password" or (fact is not None and fact.secret):
        secret = run.facts.get("account_password")
        if kind != "password" or secret is None or run.mode != "submit":
            return drop("mot de passe et champ incompatibles")
        frame_url = run._frame_url(resolve, field.ref) if resolve else run.page.url
        created = getattr(run, "maybe_created", None)  # (a creation form sent and left earlier in this run: hands off)
        if (not getattr(run, "password_rotated", False)
                and ((field.error and _PASSWORD_REFUSED.search(fold(field.error))) or _creation_bounced(run, field, snap))
                and not (created and run._account_address(frame_url) in created)):
            # The site's own password policy refuses the stored password of an account that was never created: a new one,
            # once per run (an existing account keeps its password: `rotate` answers nothing for it).
            rotate = getattr(getattr(run, "password", None), "rotate", None)
            with contextlib.suppress(Exception):
                if rotate and rotate(run._account_address(frame_url)):
                    run.password_rotated = True
                    v.notes.append("mot de passe refusé par le site : nouveau mot de passe généré")
        if run._secret(frame_url) is None:
            return drop("mot de passe indisponible pour ce site")
        return item(Action(ref=field.ref, op="fill", fact=secret.key), field, secret, used=secret.key, frame_url=frame_url)
    known = fact if fact is not None and fact.value not in (None, "") else None
    answer = (action.value or "").strip()
    declared = declaration_key(field)
    if declared and known is None:  # (choice controls are settled by `owner_item`; this covers a free-text declaration)
        answer = declared_answer(run, declared)
    # (a box that accepts the site's privacy / data-use conditions is a consent, whatever words of civil status its text
    # happens to carry: it is the planner's to tick, never a "hard fact" to guess)
    consent_box = bool(kind == "checkbox" and (engine.SIGNUP_CONSENT_RE.search(fold(_text(field))) or engine._site_consent(field))
                       and not blocked_consent(_text(field)) and not engine.MARKETING_RE.search(field.label))
    if not declared and not consent_box and is_hard_fact(f"{field.label} {field.group}", field.name, field.placeholder) and known is None:
        return drop("fait dur (état civil, adresse, permis, casier...) : jamais deviné")
    inferred = known is None or action.inferred
    basis = (action.basis.strip() or ("déduit du profil" if known is None else f"déduit du fait « {known.label} »"))[:200] if inferred else ""
    used = known.key if known else ""
    if kind == "checkbox":
        if op not in ("check", "uncheck"):
            said = norm(answer or (known.value if known else ""))
            op = "check" if said in YES else "uncheck" if said in NO else ""
            if not op:
                return drop("case à cocher : réponse illisible")
        if op == "check" and not field.required and (blocked_consent(_text(field)) or engine.MARKETING_RE.search(field.label)):
            return drop("case facultative non cochée", ask=False)
        if (op == "check") == (field.value == "true"):
            return None
        return item(Action(ref=field.ref, op=op, fact=action.fact), field, known, used=used, inferred=inferred, basis=basis)
    if op in ("check", "uncheck"):
        if kind != "radio" or not answer:
            return drop(f"opération {op} incompatible avec un champ {kind}")
    dial = is_dial(field, snap)
    phone = run.facts.get("phone")
    code = own_dial(run)
    if kind in ("select", "radio") or (kind == "combobox" and field.options):
        labels = [option.label for option in field.options]
        index = -1
        if dial and code:
            index = run._dial_option(field, phone)
        for want in (answer, known.value if known else ""):
            if index < 0:
                index = best_option(labels, want)
        if index < 0 and kind != "combobox":  # "Burkinabè" for "Burkinabé(e)", "Burkina Faso" in a list of countries
            for want in (answer, known.value if known else ""):
                if index < 0 and want:
                    index = _closest(labels, want)
        if index < 0 and kind == "combobox":
            pass  # the list shown in the snapshot may be partial: the answer is typed and looked up in the open list
        elif index < 0:
            return drop("valeur hors des options proposées")
        else:
            if kind != "combobox" and field.value and norm(field.value) == norm(labels[index]):
                return None
            return item(Action(ref=field.ref, op="select", fact=action.fact), field, known, value=labels[index], option_index=index,
                        used=used, inferred=inferred, basis=basis)
    want = known.value if known is not None and known.exact else (answer or (known.value if known else ""))
    if not (want or "").strip():
        return drop("valeur manquante")
    if kind == "combobox":
        if dial and code and number_sets_country(field, snap, code):
            return None  # (the number, typed with its code in the box beside it, chooses the country)
        out = item(Action(ref=field.ref, op="select", fact=action.fact), field, known, value=answer or want, used=used,
                   inferred=inferred, basis=basis)
        if dial and code:
            out.dial_code, out.value = code, code
        return out
    national = known is not None and known.key == "phone" and beside_dial(field, snap)
    current = field.value.strip()
    # (a site that refused the form says "numéro invalide" in plain text, with nothing on the field itself: after a refused
    # send, the phone the field holds is typed in another spelling)
    respell = bool(field.error) or (known is not None and known.key == "phone" and (
        bool(getattr(run, "rejected_submits", 0)) or phone_refused(getattr(run, "page_messages", ()))))
    if current and norm(current) == norm(want) and not respell and not (national and current.startswith(("+", "00"))):
        return None
    if (national and current and not respell and not current.startswith(("+", "00"))
            and re.sub(r"\D", "", current)[-9:] == re.sub(r"\D", "", want)[-9:]):
        return None
    out = item(Action(ref=field.ref, op="fill", fact=action.fact), field, known, value=want, used=used,
               inferred=inferred and not (known is not None and known.exact), basis=basis)
    out.national = national
    if kind in ("text", "tel") and code and _DIAL_LABEL.search(fold(f"{field.label} {field.placeholder}")) and not _PHONE_NUMBER.search(
            fold(field.label)):
        if re.findall(r"\+\s*[0-9]+", current) == [code]:
            return None  # the search box already holds the candidate's own calling code
        out.dial_code = code  # ("Indicatif de pays" as a search box: see `_search_box`; another country chosen there is replaced)
        out.national = False
    if known is not None and known.exact and known.variants and current and respell:
        # The site refused the spelling it holds (a phone with its code, a LinkedIn URL without scheme): type another one,
        # never one already tried in this run. The planner's own text counts when it is one of the fact's spellings.
        spellings = [known.value or "", *known.variants]
        digits = lambda text: re.sub(r"\D", "", text)  # noqa: E731
        beside = []
        if known.key == "phone" and french_phone(known.value or ""):  # (the two compact spellings most validators take)
            local = "0" + digits(known.value or "")[-9:]
            spellings += [local, "+33" + local[1:]]
            if national:  # (next to a calling-code list: the number without its trunk zero, then with it)
                beside = [local[1:], local]
                spellings += beside
        tried = getattr(run, "tried_spellings", None)
        if tried is None:
            tried = run.tried_spellings = set()
        tried.add(current)
        others = [text for text in dict.fromkeys(spellings) if text.strip() and text.strip() not in tried]
        asked = [text for text in others if answer and (text.strip() == answer or (digits(answer) and digits(text) == digits(answer)))]
        flipped = [text for text in others if text.lstrip().startswith(("+", "00")) != current.startswith(("+", "00"))]
        beside = [text for text in beside if text in others]
        if asked or beside or flipped or others:
            out.forced = (asked or beside or flipped or others)[0]
            tried.add(out.forced.strip())
    return out


_PHONE_WORD = re.compile(r"\b(?:telephone|phone|telefon\w*|mobile|portable|numero|number|handy)\b")
_REFUSED_WORD = re.compile(r"invalid|non valide|incorrect|not valid|format|ungultig|erron|errone|falsch")


def phone_refused(messages) -> bool:
    """Does one of the page's error messages say the phone number is refused ("The phone number is invalid")?"""
    return any(_PHONE_WORD.search(fold(text)) and _REFUSED_WORD.search(fold(text)) for text in messages or ())


_PASSWORD_REFUSED = re.compile(r"non valide|invalide|invalid|not valid|strategie|policy|politique de mot|exigence|requirement|"
                               r"ne respecte|does not meet|trop long|too long|ungultig")
_DIAL_LABEL = re.compile(r"indicatif|country code|calling code|dial(?:ing)? code|phone code|code (?:du |de )?pays|code pays|vorwahl|prefijo|prefisso")
_DIAL_OPTION = re.compile(r"\(\+\d{1,4}\)|^\s*\+\d{1,4}\b")


_PHONE_NUMBER = re.compile(r"\b(?:numero|number|nummer|n°|no|nº)\b")
_PHONE_FIELD = re.compile(r"\b(?:telephone|phone|mobile|portable|tel|handy|telefon)\b")
_SHOWN_DIAL = re.compile(r"[^\W\d_]\s*\(\s*(\+\s*\d{1,4})\s*\)")


def own_dial(run: _Run) -> str:
    """The calling code of the candidate's own number ("+33"), '' when it cannot be told."""
    phone = run.facts.get("phone")
    if phone is None or not phone.value:
        return ""
    return "+33" if french_phone(phone.value) else international_calling_code(phone.value) or ""


def foreign_dial(run: _Run, snap: PageSnapshot) -> str:
    """The calling code a page about to be SENT shows for another country than the candidate's number ("Burkina Faso (+226)"
    left by a country list in front of a French number): '' when the page shows none, or shows the candidate's."""
    mine = own_dial(run)
    if not mine:
        return ""
    texts = [snap.text, *(field.value for field in snap.fields if field.kind not in ("button", "link") and field.value)]
    shown = {code.replace(" ", "") for text in texts for code in _SHOWN_DIAL.findall(text)}
    return "" if not shown or mine in shown else sorted(shown)[0]


def is_dial(field: FormField, snap: PageSnapshot) -> bool:
    """A calling-code picker: what the engine recognizes, a label that names it ("Indicatif téléphonique du pays"), or a list
    whose options carry the codes ("France (+33)")."""
    named = bool(_DIAL_LABEL.search(fold(f"{field.label} {field.placeholder} {field.name}")))
    if field.kind in ("text", "tel"):  # a calling-code search box next to the number
        return named and not _PHONE_NUMBER.search(fold(field.label))
    if field.kind not in ("select", "combobox"):
        return False
    if _engine()._dial_selector(field, snap) or named:
        return True
    return bool(field.options) and 2 * sum(bool(_DIAL_OPTION.search(option.label)) for option in field.options) >= len(field.options)


_LEFT_FACTS = ((re.compile(r"\b(?:nationalit\w*|nazionalit\w*|staatsangehorigkeit|citizenship|nacionalidad)\b"), "nationality"),)


def _left_fact(field: FormField) -> str | None:
    label = fold(field.label)
    if re.search(r"\b(?:second|autre|other|altra|zweite|2)\b", label):
        return None  # ("Seconde nationalité": only the main one is recorded)
    return next((key for pattern, key in _LEFT_FACTS if pattern.search(label)), None)


_BARE_CODE = re.compile(r"\+\s*\d{1,4}\s*")


def carries_code(field: FormField) -> bool:
    """A number box that already shows a bare calling code ("+33"): an international phone widget. The number is typed
    there with its code, and the country shown beside it follows by itself."""
    return field.kind in ("tel", "text") and bool(_BARE_CODE.fullmatch(field.value or ""))


def beside_dial(field: FormField, snap: PageSnapshot) -> bool:
    """A phone number field that stands next to a calling-code picker: the number goes in without the code - unless the
    number box itself carries the code (see `carries_code`)."""
    return not carries_code(field) and any(
        other is not field and other.form == field.form and is_dial(other, snap) for other in snap.fields)


def number_sets_country(field: FormField, snap: PageSnapshot, code: str) -> bool:
    """The country picker of an international phone widget: the number box of its form shows a bare calling code, or
    already the candidate's own. The picker is left alone - typing the number with its code sets it."""
    return bool(code) and any(
        other is not field and other.form == field.form and other.kind in ("tel", "text")
        and (carries_code(other) or re.sub(r"\s", "", other.value or "").startswith(code)) for other in snap.fields)


def validate(run: _Run, plan: StepPlan, snap: PageSnapshot, resolve) -> _Validated:
    engine = _engine()
    v = engine._Validated()
    by_ref = {field.ref: field for field in snap.fields}
    seen: set[str] = set()
    scope = run.signup_scope
    fillable = plan.page_kind in ("form", "account_signup", "account_login", "other")
    for field in snap.fields:
        if honeypot(field):
            seen.add(field.ref)  # (neither the planner's action nor any fill by code)
    if fillable:
        for field in snap.fields:
            if field.ref in seen:
                continue
            if plan.page_kind == "form" and (cv := attached_cv(run, field, snap)) is not None:
                seen.add(field.ref)
                if field.value != cv.value:
                    v.items.append(cv)
                continue
            ruled = owner_item(run, field)
            if ruled is None and plan.page_kind == "account_signup" and account_consent(field, snap):
                ruled = (True if not engine._is_empty(field) else engine._Item(
                    Action(ref=field.ref, op="check"), field,
                    used="application_privacy" if engine.SIGNUP_PRIVACY_RE.search(_text(field)) else "application_terms"))
            if ruled is None:
                continue
            seen.add(field.ref)
            if ruled is not True:
                v.items.append(ruled)
    clicks: list[tuple[FormField, str]] = []
    for action in plan.actions:
        field = by_ref.get(action.ref)
        if field is None:
            v.notes.append(f"action ignorée : référence inconnue ({action.op})")
            continue
        if action.op == "click":
            if field.kind not in ("button", "link") or action.click_role is None:  # ("other": closing a dialog, opening a section)
                v.notes.append(f"clic ignoré sur « {engine._label(field)} »")
            else:
                clicks.append((field, action.click_role))
            continue
        if action.ref in seen:
            continue
        seen.add(action.ref)
        if scope is not None and ((field.kind == "password" and field.ref not in scope) or field.ref in run.login_box):
            continue  # the login box next to an inline account-creation form
        made = check(run, action, field, v, snap, resolve)
        if made is not None:
            v.items.append(made)
    if plan.page_kind in ("form", "account_signup"):
        password = run.facts.get("account_password")
        for field in snap.fields:
            if field.ref in seen:
                continue
            made = None
            if scope is not None and ((field.kind == "password" and field.ref not in scope) or field.ref in run.login_box):
                continue
            if field.kind == "password" and plan.page_kind == "account_signup" and password is not None and engine._is_empty(field):
                made = check(run, Action(ref=field.ref, op="fill", fact=password.key), field, v, snap, resolve)
            elif field.kind == "file" and plan.page_kind == "form" and field.required and engine._is_empty(field) \
                    and not run._file_satisfied(field, snap) and run._cv_by_code(field, snap):
                made = engine._Item(Action(ref=field.ref, op="upload_cv"), field, used="cv")
            elif field.kind in ("select", "radio") and field.options and engine._is_empty(field) and (key := _left_fact(field)):
                # A recorded fact the planner left unanswered because the list words it differently (nationality lists).
                made = check(run, Action(ref=field.ref, op="select", fact=key), field, v, snap, resolve) if run.facts.known(key) else None
            elif (code := own_dial(run)) and field.kind in ("text", "tel", "combobox") and is_dial(field, snap) and (
                    shown := re.findall(r"\+\s*[0-9]+", field.value)) and shown != [code]:
                # A calling-code box the plan left alone while it holds another country's code: the candidate's replaces it.
                fact = "phone_calling_code" if run.facts.known("phone_calling_code") else "phone"
                made = check(run, Action(ref=field.ref, op="fill", fact=fact, value=code), field, v, snap, resolve)
            elif (getattr(run, "rejected_submits", 0) and field.kind in ("text", "tel") and field.value.strip()
                    and _PHONE_FIELD.search(fold(f"{field.label} {field.name}")) and not is_dial(field, snap)
                    and run.facts.known("phone") and not any(
                        other.error or (other.required and engine._is_empty(other))
                        for other in snap.fields if other.kind not in ("button", "link"))):
                # The site refused the form and nothing on the page says why: the phone is the usual culprit (a format the
                # site does not name), so it is typed in another spelling even when the plan leaves it alone.
                made = check(run, Action(ref=field.ref, op="fill", fact="phone"), field, v, snap, resolve)
            elif field.kind in ("text", "email", "tel"):
                made = run._identity_item(field, snap)  # a name / e-mail / phone field the plan left empty
                if made is not None and made.used == "phone":
                    made.national = beside_dial(field, snap)
            if made is not None:
                seen.add(field.ref)
                v.questions = [question for question in v.questions if question.ref != field.ref]
                v.items.append(made)
    if clicks:
        v.click = clicks[-1]
    # files first: an upload often re-renders the form (the engine re-reads the page after it)
    v.items.sort(key=lambda entry: entry.act.op not in ("upload_cv", "upload_letter"))
    return v


_SENDS = re.compile(r"\b(?:absenden|abschicken|einreichen|bewerbung (?:ab)?senden|jetzt bewerben|inviare|invia candidatura|enviar|"
                    r"solicitar|verzenden|solliciteren|valider (?:ma|la|et envoyer)|deposer ma candidature|envoyer)\b")
_MOVES = re.compile(r"\b(?:weiter|nachste[rs]?|zuruck|avanti|siguiente|seguinte|volgende|suivante?|precedent|vorherige|back|previous)\b")
_PURE_MOVE = re.compile(r"(?:continuer|continue|next|next step|etape suivante|save and continue|enregistrer et continuer|"
                        + _MOVES.pattern + r")")


def effective_role(run: _Run, field: FormField, role: str, snap: PageSnapshot) -> str:
    """Resolve explicit send and pure navigation labels before dispatching the planner's click."""
    engine = _engine()
    label = fold(field.label)
    if norm(field.label) in getattr(run, "agreement_buttons", ()) and not getattr(run, "data_typed", False):
        # The "Submit" of an agreement dialog shown before the application (boxes to tick, nothing typed yet) sends the
        # agreement, not the application: it is a step like any other.
        return "next" if role == "submit" else role
    if role == "submit" and _PURE_MOVE.fullmatch(norm(label)) and not (engine.SUBMIT_RE.search(field.label) or _SENDS.search(label)):
        return "next"
    if role in ("next", "open_form", "other") and run.app_touched and (engine.SUBMIT_RE.search(field.label) or _SENDS.search(label)) \
            and not engine.NAV_RE.search(field.label) and not _MOVES.search(label):
        return "submit"
    if role in ("signup", "login") and any(entry.kind == "file" and entry.value.strip() for entry in snap.fields):
        return "submit"
    return role


def open_questions(run: _Run, plan: StepPlan, snap: PageSnapshot, v: _Validated, handled: set[str], click: FormField | None):
    """What still stands between this page and its send / next click, as (blocking, soft): the answers that failed and the
    planner's own unknowns block; required fields of the clicked form that are simply still empty are soft (the site's own
    validation decides). Site widgets of a job ad are never questions."""
    engine = _engine()
    if plan.page_kind == "job_page":
        return [], []
    by_ref = {field.ref: field for field in snap.fields}
    target = click.form if click else ""
    mine = [field for field in snap.fields if not target or not field.form or field.form == target]
    blocking = [question for question in v.questions if question.ref not in handled]
    for question in plan.unknown:
        field = by_ref.get(question.ref)
        if field is not None and not field.required and not field.error:
            continue  # a question the page does not require is left unanswered: the site's own validation decides
        if field is not None and field.kind == "file" and not field.error and _PHOTO.search(fold(f"{field.label} {field.name}")):
            continue  # no photo is on record: the form is sent without one, and the site says whether it insists
        if question.required and question.ref not in handled and (field is None or engine._is_empty(field)) and not (
                field is not None and (field.kind in ("button", "link") or owner_item(run, field) is True)):
            blocking.append(engine._question(field) if field is not None else question)
    soft = [engine._question(field) for field in mine if field.required and field.kind not in ("button", "link")
            and field.ref not in handled and engine._is_empty(field) and not run._file_satisfied(field, snap)]

    def unique(questions, seen):
        out = []
        for question in questions:
            key = question.ref or norm(question.label)
            if key not in seen:
                seen.add(key)
                out.append(question)
        return out

    seen: set[str] = set()
    blocking = unique(blocking, seen)
    return blocking, unique(soft, seen)


# ---------------------------------------------------------------- execution
# (a star of a rating row is "set" when it is the last marked item of its list)
_STATE_JS = r"""e => { if (e.checked === true || e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-pressed') === 'true') return true;
  const marked = (x) => !!x && /(^|[\s_-])(active|selected|checked|filled|full|on)([\s_-]|$)/i.test(x.getAttribute('class') || '');
  return e.tagName === 'LI' && marked(e) && !marked(e.nextElementSibling); }"""


async def _state(loc) -> bool:
    return bool(await loc.evaluate(_STATE_JS))


# Some sites redraw a box - or the whole form around it - when it is clicked: the element that was clicked leaves the page
# and a new one takes its place. Before the click the box's place among the boxes of its document and its name are noted;
# afterwards the box found at that place, under that name, tells what the click did.
_BOX_SIG = """const BOXES = 'input[type=checkbox], input[type=radio], [role=checkbox], [role=radio], [role=switch]';
  const named = (e) => { const ids = (e.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
    const said = ids.map((id) => (document.getElementById(id) || {}).textContent || '').join(' ')
      || [...(e.labels || [])].map((l) => l.textContent).join(' ') || e.getAttribute('aria-label') || (e.closest('label') || {}).textContent || '';
    return said.replace(/\\s+/g, ' ').trim().slice(0, 200) + '|' + (e.getAttribute('name') || '') + '|' + (e.getAttribute('value') || ''); };"""
_BOX_PLACE_JS = "e => { " + _BOX_SIG + " const all = [...e.ownerDocument.querySelectorAll(BOXES)]; return [all.indexOf(e), all.length, named(e)]; }"
_BOX_TWIN_JS = "([index, count, name]) => { " + _BOX_SIG + """ const all = [...document.querySelectorAll(BOXES)];
  const twin = all.length === count ? all[index] : null;
  if (!twin || named(twin) !== name) return null;
  return twin.checked === true || twin.getAttribute('aria-checked') === 'true'; }"""


async def _twin_state(run: _Run, loc, place: list) -> bool | None:
    """The state of the box now standing where the clicked one stood (None: no such box)."""
    frames = []
    with contextlib.suppress(Exception):
        frames = [await loc.owner_frame()]
    for frame in [f for f in frames if f is not None] or list(getattr(run.page, "frames", [])):
        with contextlib.suppress(Exception):
            found = await frame.evaluate(_BOX_TWIN_JS, place)
            if found is not None:
                return bool(found)
    return None


async def _toggle(run: _Run, loc, want: bool) -> None:
    """Bring a box / radio to the wanted state whatever hides it (a styled label, an overlay, a custom widget)."""
    engine = _engine()
    if await _state(loc) == want:
        return
    attempts = (
        lambda: (loc.check if want else loc.uncheck)(timeout=2500),
        lambda: loc.evaluate("e => { const l = (e.labels && e.labels[0]) || e.closest('label'); (l || e).click(); }"),
        lambda: (loc.check if want else loc.uncheck)(force=True, timeout=2500),
        lambda: loc.evaluate("e => e.click()"),
    )
    place = None
    with contextlib.suppress(Exception):
        place = await loc.evaluate(_BOX_PLACE_JS)
    for attempt in attempts:
        with contextlib.suppress(Exception):
            await attempt()
        there = False
        with contextlib.suppress(Exception):
            if await _state(loc) == want:
                return
            there = bool(await loc.evaluate("e => e.isConnected"))
        if there or not place or place[0] < 0:
            continue
        twin = await _twin_state(run, loc, place)  # (the click redrew the box: the one in its place is read)
        if twin == want:
            return
        if twin is not None:
            break  # (a further click would be made on an element that is no longer on the page)
    raise engine._Skip("case impossible à cocher")


_PRESS_JS = """e => { e.scrollIntoView({block: 'center'});
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'])
    e.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window})); }"""
# A modal that covers the page and holds no form: its text and its buttons (tagged so the chosen one can be clicked).
_BLOCKER_JS = r"""() => {
  const visible = n => { const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const centre = document.elementFromPoint(Math.floor(innerWidth / 2), Math.floor(innerHeight / 2));
  if (!centre) return null;
  const SEL = 'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"], [class*="modal" i], [class*="popup" i], '
    + '[class*="overlay" i], [id*="modal" i], [id*="popup" i]';
  let box = null;
  for (const node of document.querySelectorAll(SEL)) {
    if (node === document.body || node === document.documentElement || !visible(node)) continue;
    const pos = getComputedStyle(node).position;
    const dialog = node.matches('dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]');
    if (!dialog && pos !== 'fixed' && pos !== 'absolute') continue;
    if (!(node === centre || node.contains(centre))) continue;
    if (!box || box.contains(node)) box = node;   // the innermost one that still covers the centre
  }
  if (!box) return null;
  const inputs = [...box.querySelectorAll('input, select, textarea')].filter(n => visible(n)
    && !['hidden', 'button', 'submit', 'checkbox', 'radio'].includes((n.getAttribute('type') || '').toLowerCase()));
  if (inputs.length) return null;   // a form in a dialog is the application itself
  const label = n => (n.innerText || n.value || n.getAttribute('aria-label') || n.getAttribute('title') || '').trim().replace(/\s+/g, ' ');
  const buttons = [...box.querySelectorAll('button, [role="button"], a[href], input[type="button"], input[type="submit"]')].filter(visible);
  buttons.forEach((n, i) => n.setAttribute('data-bp-blocker', String(i)));
  const boxes = [...box.querySelectorAll('input[type=checkbox], input[type=radio], [role=checkbox], [role=radio], [role=switch]')].filter(visible).length;
  return {text: (box.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 400), buttons: buttons.map(label).slice(0, 12), boxes,
          links: buttons.map(n => n.matches('a[href]') && n.getAttribute('role') !== 'button').slice(0, 12)};
}"""
_TOPIC = re.compile(r"cookie|traceur|consent|tracking|privacy|confidentialit|donnees personnelles|datenschutz|terms|conditions d.utilisation|rgpd|gdpr")
_REFUSE = re.compile(r"^(?:tout refuser|refuser.*|continuer sans accepter|reject.*|decline.*|deny.*|necessary only|only necessary|alle ablehnen|ablehnen|nur notwendige)$")
_ACCEPT = re.compile(r"^(?:tout accepter|accepter.*|j.accepte.*|ok.*|accept.*|allow.*|i agree|agree.*|got it|j.ai compris|compris|continuer|continue|"
                     r"fermer|close|alle akzeptieren|akzeptieren|zustimmen|erlauben|verstanden|weiter|×|x)$")


_CLOSERS = frozenset({"fermer", "close", "×", "x"})
_OPENS_SETTINGS = re.compile(r"(?:ouvrir|open|afficher|show|gerer|manage|parametr|personnaliser|customi[sz]e|settings|preferences|en savoir plus|learn more)\b")
_PHOTO = re.compile(r"\b(?:photo|foto|picture|portrait|avatar)\b")
_STATEMENT = re.compile(r"declaration de confidentialit|privacy statement|data privacy|protection des donnees|datenschutzerkl")
_PRINT = frozenset({"imprimer", "print", "drucken"})


async def _shown(handle) -> bool:
    try:
        return bool(await handle.evaluate("e => e.isConnected && e.getClientRects().length > 0"))
    except Exception:
        return False


async def blocker(page) -> dict | None:
    """The modal that covers the centre of the page and holds no input (a privacy notice, a welcome pop-up), if any."""
    try:
        found = await page.evaluate(_BLOCKER_JS)
    except Exception:
        return None
    return found if isinstance(found, dict) else None


async def dismiss_blocker(run: _Run) -> str:
    """Owner rule: a cookie / privacy / terms notice never stops an application. Refuse when it offers that, otherwise accept
    (or take its only button). Any other modal is left to the planner, which is told about it (`blocker_hint`)."""
    for _ in range(2):
        found = await blocker(run.page)
        if not found or not found["buttons"] or not _TOPIC.search(fold(found["text"])):
            return ""
        labels = [fold(label).strip() for label in found["buttons"]]
        named = [index for index, label in enumerate(labels) if label]
        # Cookies / trackers are refused when the notice offers it. A privacy statement or terms the site requires to
        # apply is accepted (the candidate's standing authorization): refusing it would end the application.
        # (a box that opens as a privacy statement, or offers to print it, is the statement itself whatever it says of cookies:
        # refusing it ends the application - one ATS then offers to delete the whole candidate account)
        statement = bool(_STATEMENT.search(fold(found["text"])[:200])) or any(label in _PRINT for label in labels)
        cookies = bool(re.search(r"cookie|traceur|tracking", fold(found["text"]))) and not statement
        choice = next((index for index in named if _REFUSE.match(labels[index])), None) if cookies else None
        if choice is None:
            choice = next((index for index in named if _ACCEPT.match(labels[index]) and labels[index] not in _CLOSERS), None)
        if choice is None and cookies:
            choice = next((index for index in named if labels[index] in _CLOSERS), None)
        # (a link to the policy text is not the way out, nor is the button that OPENS the preferences: clicking it brings
        # the cookie dialog back at every step)
        real = [index for index in named if not found["links"][index] and not _OPENS_SETTINGS.match(labels[index])]
        if choice is None and len(real) == 1:
            choice = real[0]
        if choice is None:
            return ""
        try:
            await run.page.locator(f'[data-bp-blocker="{choice}"]').first.click(timeout=3000)
        except Exception:
            with contextlib.suppress(Exception):
                await run.page.locator(f'[data-bp-blocker="{choice}"]').first.evaluate(_PRESS_JS)
        await asyncio.sleep(0.5)
        after = await blocker(run.page)
        if not after or after["text"] != found["text"]:
            return found["buttons"][choice]
    return ""


async def blocker_hint(run: _Run) -> str:
    found = await blocker(run.page)
    run.agreement_buttons = ({norm(label) for label in found["buttons"] if label}
                             if found and found["buttons"] and found.get("boxes") else set())
    if not found or not found["buttons"]:
        return ""
    head = (f"[A MODAL DIALOG COVERS THE PAGE and blocks every other control: « {found['text'][:240]} ». Its buttons: "
            f"{' | '.join(label for label in found['buttons'] if label)[:200]}. ")
    if found.get("boxes"):
        # (a statement to agree to before going on: its close button gives the whole step up)
        return head + ("It asks for an agreement with boxes to tick: this step ticks the boxes it requires and clicks the button "
                       "that CONFIRMS it (submit / continue / accept / save). NEVER its close button: closing it abandons the application.] ")
    return head + "This step is ONLY the click that closes or accepts it.] "


def stuck_hint(labels: list[str]) -> str:
    return (f"[The program could NOT set these fields (no matching option / refused value): {' | '.join(labels)}. Do not try the same "
            "value again. A field of a block you added with an \"Add\" button: remove that block with its own \"Delete\" / "
            "\"Supprimer\" button (`other`). A list that shows its options: choose another listed value. Otherwise leave them as "
            "they are and answer with ONLY the click that moves on (`next` or `submit`), unless a field it did not list is still empty.] ")


def banner_hint(texts: list[str]) -> str:
    return ("[THE PAGE SHOWS THIS MESSAGE, most likely an error about the form: « " + " » « ".join(text[:300] for text in texts)
            + " ». If it names fields, fill or correct exactly those before any `next` / `submit` click.] ")


# (the items a multi-select already holds are rendered as options of a "selected" list: not choices of the open list)
_OPTION_TEXT_JS = ("e => e.closest('[data-automation-id=\"selectedItemList\"], [data-automation-id=\"selectedItem\"]') ? '' "
                   ": (e.innerText || e.textContent || '')")
_REMOVE_CHOSEN_JS = r"""(e, keep) => { let removed = 0;
  for (let p = e.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) {
    const items = p.querySelectorAll('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] [role="option"]');
    if (!items.length) continue;
    for (const item of items) {
      if ((item.textContent || '').includes(keep)) continue;
      const cross = item.querySelector('[data-automation-id="DELETE_charm"], [aria-label*="supprimer" i], [aria-label*="remove" i], [aria-label*="delete" i]');
      if (cross) { cross.click(); removed++; }
    }
    break;
  }
  return removed; }"""


# (a list named after its placeholder is not told apart from the next one: what it offered is not kept under that name)
_PLACEHOLDER_LABEL = re.compile(r"^(?:select|choose|selectionn\w*|choisi\w*|bitte wahlen|auswahlen|selezion\w*|scegli\w*|seleccion\w*"
                                r"|selecion\w*|escolh\w*)\b")


# What a list made of unmarked elements (no role, custom tags, shadow roots) offers is what APPEARED when it opened:
# `_MARK_SHOWN_JS` remembers what the page shows before the click, `_APPEARED_JS` returns the text leaves shown since.
_MARK_SHOWN_JS = r"""() => { const shown = new WeakSet();
  const walk = (root) => { for (const el of root.querySelectorAll('*')) {
    if (el.getClientRects().length) shown.add(el);
    if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document); window.__bpShown = shown; }"""
_APPEARED_JS = r"""() => { const shown = window.__bpShown, out = [];
  if (!shown) return out;
  const text = (el) => (el.innerText || el.textContent || '').trim();
  const walk = (root) => { for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) walk(el.shadowRoot);
    if (out.length >= 300 || shown.has(el) || !el.getClientRects().length || el.matches('script, style, input, textarea, select')) continue;
    // (a live region only ANNOUNCES the highlighted option to screen readers: clicking it chooses nothing)
    if (el.closest('[role=alert], [role=status], [role=log], [aria-live]')) continue;
    // (an icon written as a word in an icon font - "search", "unfold_more" - is no entry of the list)
    if (/(^|[\s_-])(material-(symbols|icons)[\w-]*|icon)([\s_-]|$)/i.test(el.getAttribute('class') || '') || el.closest('[aria-hidden=true]')) continue;
    const mine = text(el);
    if (!mine || mine.length > 120) continue;
    if ([...el.children].some((child) => child.getClientRects().length && text(child))) continue;  // (only the text leaves)
    out.push(el); } };
  walk(document); return out; }"""
# (what a list says when it has nothing to offer, or nothing yet: never an option)
_NO_RESULT = re.compile(r"^(?:(?:il n y a )?aucun resultat|aucune (?:option|correspondance|donnee|selection)|pas de resultat|"
                        r"no (?:results?|options?|matches|match|data|selection)|nothing found|keine (?:ergebnisse|treffer|auswahl)|"
                        r"sin resultados|nessun risultato|chargement|loading|recherche en cours|searching|wird geladen|laden|"
                        # (what an empty list still announces to screen readers while its entries load)
                        r"contient des elements selectionnables|contains selectable items|enthalt auswahlbare elemente)\b")
_WAS_SHOWN_JS = "e => !!(window.__bpShown && window.__bpShown.has(e))"
_CONTROL_SHAPE_JS = r"""e => { const parts = [];
  for (let n = e, i = 0; n && i < 4; i++) {
    parts.push(n.tagName.toLowerCase() + (n.getAttribute('role') ? '[' + n.getAttribute('role') + ']' : ''));
    n = n.parentElement || (n.getRootNode() && n.getRootNode().host); }
  return parts.join(' < '); }"""


# The arrow / value-help button of a text box that only suggests (`valueHelp` in the snapshot): it opens the WHOLE list, when
# typing only shows what the site's own search finds (a year, a country spelled another way: "no data").
_OPENER_JS = r"""e => {
  if (!e.matches('input') || e.getAttribute('aria-autocomplete') !== 'list' || !e.getAttribute('aria-haspopup')) return null;
  const root = e.getRootNode(), host = root && root.host, box = host || e.parentElement;
  if (!box) return null;
  const found = [...(host ? box.querySelectorAll('button, [role=button], [ui5-button]') : box.children)]
    .filter((b) => b.matches('button, [role=button], [ui5-button]') && b.getClientRects().length && !(b.innerText || b.textContent || '').trim());
  return found.length === 1 ? found[0] : null; }"""


# How many elements beside a list's control (up to three boxes around it, shadow roots included, the options of a list
# left aside) show exactly these words: one more after a choice is the tag a multiple-choice box adds for it.
_TAGS_JS = r"""(e, label) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const want = clean(label);
  let box = e, count = 0;
  for (let depth = 0; depth < 3; depth++) {
    const up = box.parentElement || (box.getRootNode() && box.getRootNode().host);
    if (!up || ['BODY', 'HTML', 'FORM', 'MAIN'].includes(up.tagName)) break;
    box = up;
  }
  const walk = (node) => {
    for (const el of [...node.children, ...(node.shadowRoot ? node.shadowRoot.children : [])]) {
      if (el === e || el.matches('[role=listbox], [role=option], [role=menu], datalist, select, script, style, template')) continue;
      if (el.getClientRects().length && clean(el.innerText || el.textContent) === want
          && ![...el.children].some((child) => clean(child.innerText || child.textContent) === want)) count++;
      walk(el);
    }
  };
  if (want && box !== e) walk(box);
  return count; }"""


async def mark_shown(run: _Run) -> None:
    for frame in run.page.frames:
        with contextlib.suppress(Exception):
            await frame.evaluate(_MARK_SHOWN_JS)


async def _popup_options(run: _Run) -> list[tuple[object, str]]:
    """(element, text) of every option the open list shows, in any frame. Elements, not positions: a virtualized list
    recycles its rows, so "the n-th option" is another one a moment later. A list without any option markup is read as
    what appeared on the page since `mark_shown`."""
    found, stale = [], []
    for frame in run.page.frames:
        with contextlib.suppress(Exception):
            for handle in (await frame.locator(_POPUP_OPTIONS).element_handles())[:300]:
                text = " ".join(str(await handle.evaluate(_OPTION_TEXT_JS)).split())
                if text and not _NO_RESULT.search(norm(text)):
                    # (an option already shown before this list was opened belongs to another list left open)
                    (stale if await handle.evaluate(_WAS_SHOWN_JS) else found).append((handle, text))
    if found or stale:
        return found or stale
    for frame in run.page.frames:
        with contextlib.suppress(Exception):
            listed = await frame.evaluate_handle(_APPEARED_JS)
            for prop in (await listed.get_properties()).values():
                handle = prop.as_element()
                if handle is None:
                    continue
                text = " ".join(str(await handle.evaluate(_OPTION_TEXT_JS)).split())
                if text and not _NO_RESULT.search(norm(text)):
                    found.append((handle, text))
    return found


def _closest(labels: list[str], want: str) -> int:
    """Last resort in a filtered list: the one option that clearly resembles the answer ("Français" for "Française")."""
    target = norm(want)
    scored = sorted(((difflib.SequenceMatcher(None, target, norm(label)).ratio(), index) for index, label in enumerate(labels)), reverse=True)
    if scored and scored[0][0] >= 0.72 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        return scored[0][1]
    if len(target) >= 6:  # the ONE option that starts like the answer: "Burkina Faso" for "Burkinabè", "France" for "Française"
        stem = [index for index, label in enumerate(labels) if norm(label).startswith(target[:5])]
        if len(stem) == 1:
            return stem[0]
    return -1


async def _combobox(run: _Run, loc, field: FormField, it: _Item) -> str:
    """Open the list, type the answer when the control takes text, pick the option that means it, and READ THE CONTROL BACK:
    a choice that does not show the wanted answer is undone (never leave another country, code or level selected)."""
    engine = _engine()
    want = it.value or ""
    if field.options and it.option_index >= 0:
        want = field.options[it.option_index].label
    code = it.dial_code
    typed = bool(await loc.evaluate("e => e.matches('input,textarea')"))

    def pick(found, loose=False):
        # (a list that renders each entry twice - a visible row and its screen-reader twin - offers each answer once:
        # "the one entry that means it" is looked for among the distinct labels, and the first row carrying it is taken)
        rows = [text for _, text in found]
        labels = list(dict.fromkeys(rows))
        index = _pick_label(labels, loose)
        return rows.index(labels[index]) if index >= 0 else -1

    def _pick_label(labels, loose):
        if code:
            hits = [index for index, text in enumerate(labels) if re.findall(r"\+\s*[0-9]+", text) == [code]]
            if not hits and not any(re.search(r"\+\s*[0-9]", text) for text in labels):  # a list of country names only
                names = DIAL_COUNTRY_NAMES.get(code, ())
                hits = [index for index, text in enumerate(labels) if norm(text) in names]
            return hits[0] if hits else -1
        index = best_option(labels, want)
        if index < 0 and loose:
            index = search_choice(labels, want, "")
        if index < 0 and loose and _PLACE_LABEL.search(fold(field.label)):
            # Suggestions of an address / city box: none is spelled like the answer ("12 Rue X, Toulouse, France" for
            # "12 rue X 31000 Toulouse"); the first of those sharing the most words with it is the place meant.
            shared = [_shared_words(label, want) for label in labels]
            if shared and max(shared) >= min(2, len(norm(want).split())):
                index = shared.index(max(shared))
        return index

    def shows(live: str, label: str) -> bool:
        if code:
            return code in live.replace(" ", "") or not re.search(r"\+\s*[0-9]", live)
        got, expected = norm(live), norm(label)
        return not got or got == expected or got in expected or expected in got or best_option([live], want) == 0

    async def choose(found, index) -> str:
        option, label = found[index]
        now = " ".join(str(await option.evaluate("e => e.isConnected ? (e.innerText || e.textContent || '') : ''")).split())
        if now != label:
            stats["recycled"] += 1
            return ""  # the row was recycled under us: read the list again
        before = await run._combo_value(loc)

        async def tags() -> int:
            try:
                return int(await loc.evaluate(_TAGS_JS, label))
            except Exception:
                return 0

        tags_before = await tags()
        try:
            await option.click(timeout=2500)
        except Exception:
            await option.evaluate(_PRESS_JS)  # covered or off-screen: the events a list listens to, in order
        await asyncio.sleep(0.4)

        async def tagged() -> bool:
            return await tags() > tags_before

        async def taken() -> bool:
            return not await _shown(option) or (await run._combo_value(loc)) != before or await tagged()

        for _ in range(4):  # (a list that closes, or shows the choice, a moment after the click: pressing again would undo it)
            if await taken():
                break
            await asyncio.sleep(0.4)
        if not await taken() and not field.multiple:  # the list did not take it: Enter picks the highlighted row
            with contextlib.suppress(Exception):
                await option.evaluate(_PRESS_JS)
                await asyncio.sleep(0.3)
            if not await taken():
                with contextlib.suppress(Exception):
                    await loc.press("Enter", timeout=2000)
                    await asyncio.sleep(0.3)
        closed = not await _shown(option)
        if closed and not field.multiple and not await _shown(loc):
            return label  # the page re-rendered around the choice (a country that reloads the address block): it was taken
        live = await run._combo_value(loc)
        if (not live or live == before) and await tagged():
            # A multiple-choice box: the choice became a tag beside the (still empty) text box, and the list may stay open.
            if not closed:  # (an open list would cover the fields below it)
                with contextlib.suppress(Exception):
                    await loc.evaluate("e => e.blur()")
                if await _shown(option):
                    with contextlib.suppress(Exception):
                        await loc.press("Escape", timeout=2000)
                    with contextlib.suppress(Exception):
                        await loc.evaluate("e => e.blur()")
            chosen = getattr(run, "combo_chosen", None)
            if chosen is not None and norm(field.label):
                chosen[norm(field.label)] = label
            return label
        if closed and not field.multiple and (not live or live == before):
            # The choice opened a sub-list (a category, then its entries): choose there too - the answer or the fact it
            # rests on when an entry says it, else "other", else the first entry.
            deeper = await _popup_options(run)
            texts = [text for _, text in deeper]
            if deeper and label not in texts:
                wanted = [want, it.fact.value if it.fact is not None and it.fact.value else ""]
                inner = next((i for i in (best_option(texts, w) for w in wanted if w) if i >= 0), -1)
                if inner < 0:
                    inner = next((i for i, text in enumerate(texts) if re.search(r"\b(?:autre|other|sonstige)", fold(text))), 0)
                entry, entry_label = deeper[inner]
                try:
                    await entry.click(timeout=2500)
                except Exception:
                    await entry.evaluate(_PRESS_JS)
                await asyncio.sleep(0.4)
                return (await run._combo_value(loc)) or f"{label} > {entry_label}"
        if (closed or field.multiple or live != before) and shows(live, label):
            return live or label
        shape = ""
        with contextlib.suppress(Exception):  # (what the option is and what the control shows, for the local failure record)
            shape = (f" (option : {str(await option.evaluate(_CONTROL_SHAPE_JS))[:80]} ; "
                     f"{'encore affichée' if await _shown(option) else 'liste fermée'} ; "
                     f"contrôle {'inchangé' if live == before else 'modifié'})")
        await run._abandon_combobox(loc)
        with contextlib.suppress(Exception):
            await run.page.keyboard.press("Escape")
        raise engine._Skip("la liste n'a pas retenu la réponse choisie" + shape)

    stats = {"read": 0, "recycled": 0}  # (for the local failure record: how many entries were read, how many slipped away)

    def remember(found):
        stats["read"] = max(stats["read"], len(found))
        # What the open list offers is shown to the planner at the next read (a list that only renders once opened). The
        # fullest list seen stands: what a typed query narrows it to (or its "no result" line) does not replace it.
        labels = list(dict.fromkeys(text for _, text in found))
        key = norm(field.label)
        # (a long list - languages, regions - is kept too: the answer the planner words differently is one of its entries)
        if labels and len(labels) <= 250 and key and not _PLACEHOLDER_LABEL.search(key) and len(labels) > len(run.combo_options.get(key, ())):
            run.combo_options[key] = labels

    opener = None
    if typed:
        with contextlib.suppress(Exception):
            opener = (await loc.evaluate_handle(_OPENER_JS)).as_element()

    async def open_list():
        try:
            await loc.click(timeout=4000)
        except Exception:
            await loc.evaluate("e => e.click()")
        await asyncio.sleep(0.4)
        if opener is not None and not await _popup_options(run):
            try:  # (a box a click leaves closed: its arrow shows the whole list)
                await opener.click(timeout=3000)
            except Exception:
                await opener.evaluate(_PRESS_JS)
            await asyncio.sleep(0.6)

    await mark_shown(run)
    await open_list()
    found = await _popup_options(run)
    for _ in range(3 if typed else 7):  # (a list filled by a request made when it opens: seconds, for one that depends on another)
        if found:
            break
        await asyncio.sleep(0.5)
        found = await _popup_options(run)
    if found and not await found[0][0].evaluate(_WAS_SHOWN_JS):
        remember(found)
    index = pick(found, loose=not typed)  # (a list nothing can be typed in: the entry closest to the answer, now)
    if index >= 0 and (len(found) <= 40 or not typed or opener is not None):
        if done := await choose(found, index):
            return done
    typer = loc if typed else None
    if typer is None:
        # A list opened by a non-text control often brings its own search box and focuses it (it shows a few entries only,
        # the rest come with the search): what is typed goes there.
        with contextlib.suppress(Exception):
            box = (await run.page.evaluate_handle(
                "() => { const a = document.activeElement; return a && a.matches('input:not([type=hidden]), [role=searchbox]') ? a : null; }"
            )).as_element()
            if box is None:
                boxes = await run.page.locator("input[type=search]:visible, [role=searchbox]:visible").element_handles()
                fresh = [handle for handle in boxes if not await handle.evaluate(_WAS_SHOWN_JS)]
                box = fresh[0] if len(fresh) == 1 else None
            typer = box
    if typer is not None:
        words = want.split()
        country = sorted(DIAL_COUNTRY_NAMES.get(code, ()), key=len)[:1] if code else []
        texts = [code, code.lstrip("+"), *[name.title() for name in country]] if code else [
            want, want.split(" (")[0], " ".join(words[:2]), words[0] if words else "", *[name.title() for name in language_names(want)],
            want[:4] if len(want) > 5 else ""]
        for text in dict.fromkeys(text for text in texts if text):
            with contextlib.suppress(Exception):
                await typer.fill("", timeout=3000)
            await typer.type(text, delay=30, timeout=8000)
            await asyncio.sleep(1.0)
            for _ in range(2):
                found = await _popup_options(run)
                for _wait in range(5):  # a list fetched from the server: give it time before reading another list's leftovers
                    if found and not await found[0][0].evaluate(_WAS_SHOWN_JS):
                        break
                    await asyncio.sleep(0.5)
                    found = await _popup_options(run)
                if found and not await found[0][0].evaluate(_WAS_SHOWN_JS):
                    remember(found)
                index = pick(found, loose=True)
                if index < 0 and len(found) == 1 and not code and norm(text) and norm(text) in norm(found[0][1]):
                    index = 0
                if index < 0:
                    break
                if done := await choose(found, index):
                    return done
    if typed and not code:
        # The box's own search found nothing (it looks the words up its own way), yet the whole list had the entry: it is
        # chosen there.
        with contextlib.suppress(Exception):
            await loc.fill("", timeout=3000)
            await asyncio.sleep(0.4)
        found = await _popup_options(run)
        if not found:
            await open_list()
            for _ in range(4):
                if found := await _popup_options(run):
                    break
                await asyncio.sleep(0.4)
        index = pick(found)
        if index >= 0 and (done := await choose(found, index)):
            return done
    if field.required and not code:
        # A required list that offers nothing like the answer: its "other" entry is the honest "none of these".
        with contextlib.suppress(Exception):
            if typed:
                await loc.fill("", timeout=3000)
                await asyncio.sleep(0.4)
            found = await _popup_options(run)  # (emptying the box may have opened the list: a click would close it)
            if not found:
                await open_list()
                for _ in range(4):
                    if found := await _popup_options(run):
                        break
                    await asyncio.sleep(0.4)
            if found and not await found[0][0].evaluate(_WAS_SHOWN_JS):
                remember(found)  # (a list that came late is still what the planner is shown next)
            others = [index for index, (_, text) in enumerate(found) if _OTHER_OPTION.search(norm(text))]
            if len(others) == 1 and (done := await choose(found, others[0])):
                return done
    await run._abandon_combobox(loc)
    with contextlib.suppress(Exception):
        await run.page.keyboard.press("Escape")
    offered = run.combo_options.get(norm(field.label)) or []
    shape = ""
    with contextlib.suppress(Exception):
        shape = str(await loc.evaluate(_CONTROL_SHAPE_JS))[:120]
    shape += f" ; {stats['read']} entrées lues" + (f", {stats['recycled']} choix sur une ligne disparue" if stats["recycled"] else "")
    raise engine._Skip("option introuvable dans la liste" + (" ; options proposées : " + " | ".join(offered[:20]) if offered else "")
                       + f" (contrôle : {shape})")


_CODE_FIELD_NOT = re.compile(r"postal|zip|pays|country|promo|parrain|referral|indicatif|naf|ape|siret|iban|bic")
_CODE_BUTTON = re.compile(r"verif|valid|confirm|continu|suivant|soumettre|submit|next|envoyer|postuler|activ|bestatig|weiter|\bok\b")


async def enter_emailed_code(run: _Run, snap: PageSnapshot, resolve) -> bool:
    """The site e-mailed a one-time code and shows the field for it (account creation on a combined signup + application
    form asks for it AFTER the send click): read the code from the mailbox, type it, click the button that validates it.
    Twice per run at most. False when there is no such field, no code, or nothing to click."""
    from jobhunt.apply import mail  # noqa: PLC0415

    engine = _engine()
    flow = run.email_verification
    if flow is None or run.code_entries >= 2:
        return False
    fields = [field for field in snap.fields if field.kind in ("text", "number", "tel", "password") and engine._is_empty(field)
              and (field.autocomplete == "one-time-code" or mail.CODE_LABEL.search(field.label))
              and not _CODE_FIELD_NOT.search(fold(field.label))]
    # (a code typed one character per box: four to eight boxes named alike but for their rank)
    split = 4 <= len(fields) <= 8 and len({re.sub(r"\d+", "", norm(field.label)) for field in fields}) == 1
    if (len(fields) != 1 and not split) or any(other.kind == "password" and other not in fields for other in snap.fields):
        return False  # (a password + code login box is a login, not a lone code prompt)
    run.code_entries += 1
    verification, code = await flow.poll()
    if code:
        run.diagnostics.email = code
    if not verification or verification.kind != "code" or (split and len(verification.value) != len(fields)):
        return False
    target = await run._el(resolve, fields[0].ref)
    if target is None:
        return False
    await run._before_mutation("fill", target)
    run.secrets.add(verification.value)
    if split:
        boxes = [await run._el(resolve, field.ref) for field in fields]
        if any(box is None for box in boxes):
            return False
        await target.click(timeout=engine.ACTION_TIMEOUT_MS)
        await run.page.keyboard.type(verification.value, delay=90)  # (such boxes move on by themselves as one types)
        await asyncio.sleep(0.5)
        typed = "".join([str(await box.evaluate("e => e.isConnected ? e.value : ''")) for box in boxes])
        if typed and typed != verification.value:  # (boxes that do not move on: one character each)
            for box, char in zip(boxes, verification.value):
                await box.fill(char, timeout=engine.ACTION_TIMEOUT_MS)
    else:
        await target.fill(verification.value, timeout=engine.ACTION_TIMEOUT_MS)
    run.diagnostics.email = "email_code_used"
    if split:
        await asyncio.sleep(1.5)
        if not await _shown(target):
            return True  # (the boxes are gone: the site took the code as soon as it was complete)
    buttons = [field for field in snap.fields if field.kind == "button" and _CODE_BUTTON.search(fold(field.label))]
    scoped = [field for field in buttons if fields[0].form and field.form == fields[0].form] or buttons
    control = await run._el(resolve, scoped[-1].ref) if scoped else None
    try:
        if control is not None:
            await run.autonomous.target(control, button=True)
            await control.click(timeout=engine.ACTION_TIMEOUT_MS)
        else:
            await target.press("Enter", timeout=engine.ACTION_TIMEOUT_MS)
    except Exception:
        return False
    await asyncio.sleep(2.0)
    return True


_FILE_SOURCE = re.compile(r"appareil|device|ordinateur|computer|poste de travail|fichier|file|parcourir|browse|local|disque|upload|televers|importer")


async def _file_inputs(run: _Run) -> list:
    found = []
    for frame in run.page.frames:
        with contextlib.suppress(Exception):
            found += await frame.locator("input[type=file]").element_handles()
    return found


async def _upload_through(run: _Run, opener, path) -> None:
    """Put the file on the site through a styled upload control: the file input it stands for, the native file chooser it
    opens, or the dialog / menu it opens (a file input that appears, or a "from this device" entry). ONE click on the opener."""
    engine = _engine()
    near = (await opener.evaluate_handle(engine._NEAR_FILE_JS)).as_element()
    if near is not None:
        await near.set_input_files(str(path))
        run.uploaded = True
        return
    before = {await handle.evaluate("e => e.outerHTML.slice(0, 200)") for handle in await _file_inputs(run)}
    trace = []  # (for the local failure record: how the click went and what it brought)
    try:
        async with run.page.expect_file_chooser(timeout=4000) as waiting:
            try:
                await opener.click(timeout=3000)
                trace.append("clic fait")
            except Exception:
                trace.append("clic empêché, événements envoyés")
                await opener.evaluate(_PRESS_JS)
        await (await waiting.value).set_files(str(path))
        run.uploaded = True
        return
    except Exception:
        pass
    for attempt in range(10):  # no native chooser: the click opened a dialog / menu of its own
        inputs = await _file_inputs(run)
        fresh = [handle for handle in inputs if await handle.evaluate("e => e.outerHTML.slice(0, 200)") not in before]
        choice = fresh or (inputs if len(inputs) == 1 else [])
        if choice:
            await choice[0].set_input_files(str(path))
            run.uploaded = True
            return
        if attempt == 3:  # a source menu ("Depuis cet appareil", Dropbox, Drive...): the local entry opens the chooser
            for option, text in await _popup_options(run):
                if _FILE_SOURCE.search(fold(text)):
                    with contextlib.suppress(Exception):
                        async with run.page.expect_file_chooser(timeout=3000) as waiting:
                            await option.click(timeout=2000)
                        await (await waiting.value).set_files(str(path))
                        run.uploaded = True
                        return
        if attempt == 6 and not await _popup_options(run) and await blocker(run.page) is None:
            # Nothing came of the click (no chooser, no dialog, no menu): the page was not listening yet, or took the
            # first click for something else. Once more, as the page's own script would.
            trace.append("rien d'ouvert : second clic")
            with contextlib.suppress(Exception):
                async with run.page.expect_file_chooser(timeout=3000) as waiting:
                    await opener.evaluate("e => e.click()")
                await (await waiting.value).set_files(str(path))
                run.uploaded = True
                return
        await asyncio.sleep(0.5)
    with contextlib.suppress(Exception):
        shown = await blocker(run.page)
        trace.append(f"{len(await _file_inputs(run))} champ(s) fichier, "
                     + (f"dialogue « {shown['text'][:50]} » ({' | '.join(shown['buttons'])[:80]})" if shown else "aucun dialogue"))
    raise engine._Skip("aucun champ de fichier après le clic sur le bouton de dépôt" + (f" ({' ; '.join(trace)})" if trace else ""))


async def _search_box(run: _Run, target, code: str) -> str:
    """A calling-code search box (a text input that lists countries once something is typed and Enter is pressed): type the
    country of the code, then pick the one entry that carries the code."""
    engine = _engine()
    names = sorted(DIAL_COUNTRY_NAMES.get(code, ()), key=len)
    await mark_shown(run)
    with contextlib.suppress(Exception):
        if await target.evaluate(_REMOVE_CHOSEN_JS, f"({code})"):  # another country's code sits there: take it out first
            await asyncio.sleep(0.4)
    for text in dict.fromkeys([*(name.title() for name in names[:1]), code, code.lstrip("+")]):
        with contextlib.suppress(Exception):
            await target.fill("", timeout=3000)
        await target.type(text, delay=30, timeout=8000)
        await asyncio.sleep(0.8)
        found = await _popup_options(run)
        if not found:
            with contextlib.suppress(Exception):
                await target.press("Enter", timeout=2000)
            await asyncio.sleep(1.0)
            found = await _popup_options(run)
        hits = [entry for entry in found if re.findall(r"\+\s*[0-9]+", entry[1]) == [code]]
        if not hits and names:
            hits = [entry for entry in found if norm(entry[1]) in names]
        if hits:
            try:
                await hits[0][0].click(timeout=2500)
            except Exception:
                await hits[0][0].evaluate(_PRESS_JS)
            await asyncio.sleep(0.4)
            with contextlib.suppress(Exception):
                await target.evaluate(_REMOVE_CHOSEN_JS, f"({code})")  # (a widget that ADDS instead of replacing)
            return hits[0][1]
    with contextlib.suppress(Exception):
        await target.fill("", timeout=3000)
        await run.page.keyboard.press("Escape")
    raise engine._Skip("indicatif introuvable dans la liste de recherche")


_SEARCH_PROMPT_JS = ("e => !!(e.matches('[data-uxi-widget-type=\"selectinput\"], [data-automation-id=\"searchBox\"]') "
                     "|| e.closest('[data-automation-id=\"multiSelectContainer\"], [data-automation-id=\"multiselectInputContainer\"]'))")
_CHOSEN_JS = r"""e => { for (let p = e.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) {
    const chosen = p.querySelector('[data-automation-id="selectedItemList"], [data-automation-id="selectedItem"]');
    if (chosen) return (chosen.textContent || '').trim(); }
  return ''; }"""
_OTHER_OPTION = re.compile(r"^(?:autres?|others?|sonstiges?|andere)\b|\b(?:not listed|non liste|other school|autre etablissement|autre ecole)\b")
_PLACE_LABEL = re.compile(r"\b(?:adresse|address|anschrift|ville|city|localisation|location|lieu|commune|stadt|ort)\b")
_SMALL_WORDS = frozenset({"ecole", "school", "universite", "university", "institut", "institute", "national", "nationale", "superieure",
                          "superieur", "ingenieur", "ingenieurs", "engineering", "sciences", "science", "pour", "with", "from", "dans"})


def _shared_words(label: str, want: str) -> int:
    mine = {word for word in norm(want).split() if len(word) >= 3}
    return len(mine & set(norm(label).split()))


def search_choice(labels: list[str], want: str, query: str) -> int:
    """The result of a search prompt that means the answer: the option the answer names, the one that resembles it, the ONE
    result of a query, else the single result sharing the most (at least two) words with the answer. -1 when none is clear."""
    index = best_option(labels, want)
    if index < 0:
        index = _closest(labels, want)
    if index < 0 and len(labels) == 1 and norm(query) and norm(query) in norm(labels[0]):
        index = 0
    if index < 0:
        scored = sorted(((_shared_words(label, want), -len(label), position) for position, label in enumerate(labels)), reverse=True)
        if scored and scored[0][0] >= 2 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            index = scored[0][2]
    return index


async def is_search_prompt(target) -> bool:
    with contextlib.suppress(Exception):
        return bool(await target.evaluate(_SEARCH_PROMPT_JS))
    return False


# "How did you hear about us?": the question a site asks to learn where its applicants come from.
SOURCE_QUESTION = re.compile(r"comment (?:nous )?avez[- ]vous (?:connu|entendu|eu connaissance|decouvert)|how did you (?:hear|find|learn)|"
                             r"ou avez[- ]vous (?:vu|trouve|decouvert)|source (?:de (?:la |votre )?candidature|of (?:your )?application)|"
                             r"wie (?:haben sie|sind sie auf)")
# (what such a list calls the employer's own careers site, the way every application made here arrives)
_OWN_SITE_QUERIES = ("Site carrière", "Carrière", "Career", "Site web", "Website", "Internet")


async def _search_pick(run: _Run, target, field: FormField, want: str, *, source: bool = False) -> str:
    """A search prompt (a text box whose typed text is only a query: the answer is the result picked from the list it
    returns, shown beside the box as a chosen item). Type the answer, then its most telling words, and pick the result that
    means it; a required prompt that knows nothing of the answer takes its "other" entry when it has one. A required
    "how did you hear about us" prompt (`source`) that does not list where the ad was seen takes the entry for the
    employer's own careers site first: that is where the application is made."""
    engine = _engine()

    async def results() -> list[tuple[object, str]]:
        found = await _popup_options(run)
        if not found:
            with contextlib.suppress(Exception):
                await target.press("Enter", timeout=2000)
            for _ in range(8):
                await asyncio.sleep(0.5)
                if found := await _popup_options(run):
                    break
        return found

    async def take(entry) -> str:
        option, label = entry
        try:
            await option.click(timeout=2500)
        except Exception:
            await option.evaluate(_PRESS_JS)
        await asyncio.sleep(0.6)
        chosen = " ".join(str(await target.evaluate(_CHOSEN_JS) or "").split())
        if await _shown(option):  # (a list that stays open once the item is taken)
            with contextlib.suppress(Exception):
                await run.page.keyboard.press("Escape")
        return chosen and label

    await mark_shown(run)
    words = sorted({word for word in re.findall(r"[^\W\d_][\w'’-]{2,}", want) if norm(word) not in _SMALL_WORDS and len(norm(word)) >= 4},
                   key=len, reverse=True)
    queries = [want, want.split(" (")[0], want.split(",")[0], *words[:4]]
    for text in dict.fromkeys(query.strip() for query in queries if query.strip()):
        with contextlib.suppress(Exception):
            await target.fill("", timeout=3000)
        await target.type(text, delay=30, timeout=8000)
        await asyncio.sleep(0.8)
        found = await results()
        index = search_choice([label for _, label in found], want, text)
        if index >= 0 and (done := await take(found[index])):
            return done
    if field.required and source:
        for text in _OWN_SITE_QUERIES:
            with contextlib.suppress(Exception):
                await target.fill("", timeout=3000)
            await target.type(text, delay=30, timeout=8000)
            await asyncio.sleep(0.8)
            found = await results()
            sites = [entry for entry in found if norm(text) in norm(entry[1])]
            if sites and (done := await take(min(sites, key=lambda entry: len(entry[1])))):
                return done
    if field.required:
        for text in ("Autre", "Other", "Sonstige"):
            with contextlib.suppress(Exception):
                await target.fill("", timeout=3000)
            await target.type(text, delay=30, timeout=8000)
            await asyncio.sleep(0.8)
            found = await results()
            others = [entry for entry in found if _OTHER_OPTION.search(norm(entry[1]))]
            if others and (done := await take(min(others, key=lambda entry: len(entry[1])))):
                return done
    with contextlib.suppress(Exception):
        await target.fill("", timeout=3000)
        await run.page.keyboard.press("Escape")
    raise engine._Skip("aucun résultat de recherche ne correspond à la réponse")


def short_message(run: _Run) -> str:
    """The note a required "message" box takes when no cover letter is available: what the application is, nothing more."""
    value = lambda key: (fact.value or "").strip() if (fact := run.facts.get(key)) is not None else ""  # noqa: E731
    name = value("full_name") or " ".join(part for part in (value("first_name"), value("last_name")) if part)
    title = " ".join(str(getattr(run, "job_title", "") or "").split())
    if not name:
        return ""
    post = f" pour le poste « {title[:140]} »" if title else ""
    return (f"Bonjour,\n\nJe vous adresse ma candidature{post}. Vous trouverez mon CV en pièce jointe ; je reste à votre "
            f"disposition pour échanger sur mon profil.\n\nCordialement,\n{name}")


def _as_date(text: str) -> date | None:
    """The day a date answer names ("2027-02-01", "01/02/2027", "1er février 2027"), None for anything else."""
    from jobhunt.apply.facts import from_french_date  # noqa: PLC0415

    text = " ".join((text or "").split())
    if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text):
        text = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    try:
        return date.fromisoformat(from_french_date(text) or text)
    except ValueError:
        return None


# One step in the calendar a read-only date box opened: reads the month it shows and marks (data-bp-cal) what to click next -
# the wanted day when its month is shown, else the arrow towards it. {state: 'none' | 'day' | 'move' | 'stuck', shown}.
_CALENDAR_JS = r"""(el, [year, month, day]) => {
  const vis = (n) => { const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const off = /disabled|blank|muted|other|outside|adjacent|\bold\b|\bnew\b|prev|next|header/i;
  const days = (root) => [...root.querySelectorAll('*')].filter((n) => !n.children.length && /^\d{1,2}$/.test(n.textContent.trim())
    && vis(n) && !off.test(String(n.className)) && n.getAttribute('aria-disabled') !== 'true' && !n.disabled);
  const doc = el.getRootNode();
  for (const old of doc.querySelectorAll('[data-bp-cal]')) old.removeAttribute('data-bp-cal');
  const boxes = (scope) => [...scope.querySelectorAll('[class*="calendar" i], [class*="datepicker" i], [class*="flatpickr" i], [role="dialog"], [role="grid"]')]
    .filter((c) => !c.contains(el) && vis(c) && days(c).length >= 20).sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  let root = null;
  for (let up = el.parentElement, n = 0; up && n < 4 && !root; up = up.parentElement, n++) root = boxes(up)[0] || null;
  root = root || boxes(doc)[0] || null;
  if (!root) return {state: 'none', shown: ''};
  const names = ['jan', 'f[eé]v|feb', 'mar', 'avr|apr', 'mai|may', 'juin|jun', 'juil|jul', 'ao[uû]|aug', 'sep', 'oct', 'nov', 'd[eé]c'];
  const said = (root.innerText || root.textContent || '').slice(0, 400);
  const head = said.match(/([A-Za-zÀ-ÿ]{3,})\.?\s+(\d{4})/);
  const shownMonth = head ? names.findIndex((name) => new RegExp('^(?:' + name + ')', 'i').test(head[1])) + 1 : 0;
  if (!head || !shownMonth) return {state: 'stuck', shown: said.slice(0, 40)};
  const gap = (year - Number(head[2])) * 12 + (month - shownMonth);
  const mark = (node) => { node.setAttribute('data-bp-cal', '1'); return true; };
  if (gap === 0) {
    const cell = days(root).find((n) => Number(n.textContent.trim()) === day);
    return {state: cell && mark(cell) ? 'day' : 'stuck', shown: head[0]};
  }
  const arrow = [...root.querySelectorAll('*')].filter(vis).find((n) => (gap > 0 ? /next|suivant|forward/i : /prev|pr[eé]c[eé]dent|back/i)
    .test(String(n.className) + ' ' + (n.getAttribute('aria-label') || '') + ' ' + (n.getAttribute('title') || '')));
  return {state: arrow && mark(arrow) ? 'move' : 'stuck', shown: head[0]};
}"""


async def _calendar_pick(run: _Run, target, wanted: date) -> str:
    """Choose `wanted` in the calendar that a read-only date box opens (month arrows, then the day). Returns what the box
    shows afterwards, "" when no calendar opened or the day could not be reached (the caller then tries to type)."""
    engine = _engine()
    with contextlib.suppress(Exception):
        await target.click(timeout=engine.ACTION_TIMEOUT_MS)
    for _ in range(40):  # (the month arrows: three years either way at most)
        await asyncio.sleep(0.15)
        try:
            step = await target.evaluate(_CALENDAR_JS, [wanted.year, wanted.month, wanted.day])
        except Exception:
            return ""
        if step["state"] not in ("day", "move"):
            break
        marked = (await target.evaluate_handle("e => e.getRootNode().querySelector('[data-bp-cal]')")).as_element()
        if marked is None:
            break
        await marked.click(timeout=engine.ACTION_TIMEOUT_MS)
        if step["state"] == "day":
            await asyncio.sleep(0.2)
            return str(await target.evaluate("e => e.value ?? ''") or "").strip()
    with contextlib.suppress(Exception):
        await run.page.keyboard.press("Escape")
    return ""


async def execute(run: _Run, it: _Item, target, resolve, snap: PageSnapshot) -> str:
    """Carry out one item on the live page. `target` is the exact element of the snapshot (the first radio of a group)."""
    engine = _engine()
    action, field = it.act, it.field
    timeout = engine.ACTION_TIMEOUT_MS
    if action.op == "upload_cv":
        await run._before_mutation(action.op, target)
        if field.kind == "file":
            await target.set_input_files({"name": "CV.pdf", "mimeType": "application/pdf", "buffer": run.uploads.cv_bytes}, timeout=timeout)
            run.uploaded = True
            return "CV.pdf"
        await _upload_through(run, target, run.uploads.cv_pdf)
        return run.uploads.cv_pdf.name
    if action.op == "upload_letter":
        letter = await run._letter_obj()
        if letter is None or letter.pdf_path is None:
            raise engine._Skip("lettre indisponible")
        await run._before_mutation(action.op, target)
        await run._set_files(resolve(field.ref), field, letter.pdf_path)
        return letter.pdf_path.name
    if action.op == "letter_text":
        letter = await run._letter_obj()
        text = letter.text if letter is not None and letter.text else ""
        if not text and field.required and field.kind == "textarea":
            text = short_message(run)  # no letter could be written: a required message box takes a short neutral note
        if not text:
            raise engine._Skip("lettre indisponible")
        await run._before_mutation(action.op, target)
        await target.fill(text, timeout=timeout)
        return text[:60] + "…"
    if field.kind == "checkbox":
        want = action.op == "check"
        await run._before_mutation(action.op, target)
        await _toggle(run, target, want)
        if want and it.used in {"application_privacy", "application_terms", "application_background_check"}:
            run.consent_codes.add(it.used)
        return "true" if want else "false"
    if field.kind == "radio":
        wanted = field.options[it.option_index]
        group = resolve(field.ref)
        live = await group.evaluate_all(engine._RADIOS_JS)
        hits = [index for index, radio in enumerate(live) if norm(radio["label"]) == norm(wanted.label)
                and (not wanted.value or radio["value"] == wanted.value)]
        if not hits:
            hits = [index for index, radio in enumerate(live) if norm(radio["label"]) == norm(wanted.label)]
        if not hits and it.option_index < len(live):
            hits = [it.option_index]
        if not hits:
            raise engine._Skip("option introuvable dans le groupe actuel")
        member = group.nth(hits[0])
        await run._before_mutation(action.op, await member.element_handle(timeout=timeout))
        await _toggle(run, member, True)
        if it.used == "application_background_check":
            run.consent_codes.add(it.used)
        return wanted.label
    if field.kind == "select":
        option = field.options[it.option_index]
        await run._before_mutation(action.op, target)
        try:
            await target.select_option(value=option.value, timeout=timeout) if option.value else await target.select_option(
                label=option.label, timeout=timeout)
        except Exception:
            await target.select_option(label=option.label, timeout=timeout)
        shown = str(await target.evaluate("e => { const o = e.selectedOptions[0]; return o ? (o.label || o.textContent).trim() : ''; }") or "")
        if norm(shown) != norm(option.label):
            raise engine._Skip("valeur lue différente de la réponse choisie")
        return shown
    if field.kind == "combobox":
        await run._before_mutation(action.op, target)
        return await _combobox(run, target, field, it)
    fact = it.fact
    if fact is not None and fact.secret:
        if field.kind != "password":
            raise engine._Skip("mot de passe jamais saisi ici")
        secret = run._secret(it.frame_url)
        if secret is None:
            raise engine._Skip("mot de passe indisponible")
        run.secrets.add(secret)
        await run._before_mutation(action.op, target)
        await target.fill(secret, timeout=timeout)
        return engine.SECRET_LOG
    if it.dial_code and field.kind in ("text", "tel"):
        await run._before_mutation(action.op, target)
        return await _search_box(run, target, it.dial_code)
    text = it.value or ""
    if getattr(it, "forced", ""):
        text = it.forced
    elif fact is not None and fact.exact and fact.variants:
        length, pattern = await target.evaluate("e => [e.getAttribute('maxlength'), e.getAttribute('pattern')]")
        text = engine.pick_variant(fact, field, int(length) if length and length.isdigit() else None, pattern, national=it.national)
    if field.kind == "date":
        from jobhunt.apply.facts import from_french_date  # noqa: PLC0415

        text = from_french_date(text) or text
    if field.kind != "textarea":
        text = text.replace("\r", " ").replace("\n", " ")
    await run._before_mutation(action.op, target)
    if field.kind in ("text", "search") and text and await is_search_prompt(target):
        return await _search_pick(run, target, field, text,
                                  source=it.used == "job_source" or bool(SOURCE_QUESTION.search(fold(field.label))))
    if note := getattr(run.autonomous, "note_value", None):
        note(text)
    if field.kind in ("text", "date") and (wanted := _as_date(text)) and await target.evaluate("e => e.readOnly === true"):
        # A box that cannot be typed in and opens a calendar: the day is chosen in the calendar itself.
        if shown := await _calendar_pick(run, target, wanted):
            return shown
    try:
        await target.fill(text, timeout=timeout)
    except Exception:
        await target.click(timeout=timeout)
        await run.page.keyboard.type(text, delay=25)
    with contextlib.suppress(Exception):
        if not str(await target.evaluate("e => e.value ?? ''") or "").strip():  # a masked input that ignores fill()
            await target.click(timeout=timeout)
            await run.page.keyboard.type(text, delay=25)
    return text
