"""The known-facts catalogue of a candidate for one job: everything the form filler may answer, and nothing else.

Sources: master CV identity/education/languages, the profile, `data/profiles/<pid>/answers.yaml` (answers the user gave once)
and the job itself. A fact with value None is UNKNOWN: a required field needing it pauses the run (never guessed).
The account password is NEVER in the sheet (the engine reads it from the keychain)."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel

from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import Fact, FactSheet
from jobhunt.config import get_settings
from jobhunt.cv.master import Master
from jobhunt.dedupe import MergedJob
from jobhunt.models import ContractType, Profile

SECRET_PLACEHOLDER = "(mot de passe géré par le système)"
CONSENT_REQUIRED_LABEL = ("Accepter les consentements OBLIGATOIRES : politique de confidentialité / traitement des données de "
                          "candidature / CGU")
CONSENT_OPTIONAL_LABEL = "Consentements FACULTATIFS (newsletter, marketing, vivier de talents, partage à des tiers) : refuser"


# ---------------------------------------------------------------- answers.yaml
def answers_path(profile_id: int):
    return get_settings().profiles_dir / str(profile_id) / "answers.yaml"


def load_answers(profile_id: int) -> dict:
    p = answers_path(profile_id)
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as e:
        raise ApplyError("answers.yaml illisible ou invalide") from e
    if not isinstance(data, dict):
        raise ApplyError("answers.yaml doit être un dictionnaire")
    return data


def _norm_question(q: str) -> str:
    q = "".join(c for c in unicodedata.normalize("NFKD", q or "") if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", q.casefold().replace("’", "'")).strip()


norm_question = _norm_question  # public: how two question labels are compared (accents, case, punctuation ignored)


def _write_answers(profile_id: int, data: dict) -> None:
    """Atomic YAML round trip (temp file + replace); comments of the original file may be lost."""
    p = answers_path(profile_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100, default_flow_style=False), encoding="utf-8")
    os.replace(tmp, p)


def save_custom_answers(profile_id: int, pairs: list[tuple[str, str]]) -> None:
    """Append (or update, same normalized question) `custom` entries; every other key is kept (comments may be lost)."""
    data = load_answers(profile_id)
    custom = [c for c in (data.get("custom") or []) if isinstance(c, dict)]
    for question, answer in pairs:
        question, answer = (question or "").strip(), (answer or "").strip()
        if not question or not answer:
            continue
        entry = next((c for c in custom if _norm_question(str(c.get("question", ""))) == _norm_question(question)), None)
        if entry is not None:
            entry["answer"] = answer
        else:
            custom.append({"question": question, "answer": answer})
    data["custom"] = custom
    _write_answers(profile_id, data)


# ---------------------------------------------------------------- trusted hosts (answers.yaml `trusted_hosts:`)
_HOSTNAME = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")


def valid_host(host: str | None) -> str | None:
    """The lowercase hostname if `host` is a plain DNS name with at least one dot (no scheme, port, path, IP address)."""
    h = (host or "").strip().lower().rstrip(".")
    return h if _HOSTNAME.fullmatch(h) and not re.fullmatch(r"[0-9.]+", h) else None


def trusted_hosts(profile_id: int) -> list[str]:
    """Hosts the user declared trustworthy for application forms (invalid entries are ignored)."""
    raw = load_answers(profile_id).get("trusted_hosts")
    return list(dict.fromkeys(h for h in (valid_host(x) if isinstance(x, str) else None for x in (raw if isinstance(raw, list) else [])) if h))


def add_trusted_host(profile_id: int, host: str) -> str:
    """Add `host` (validated, lowercased) to `trusted_hosts`; returns it. Raises ApplyError for anything but a hostname."""
    h = valid_host(host)
    if h is None:
        raise ApplyError("Nom de site invalide : indiquer uniquement le nom d'hôte (par exemple jobs.exemple.fr)")
    data = load_answers(profile_id)
    current = trusted_hosts(profile_id)
    if h not in current:
        data["trusted_hosts"] = [*current, h]
        _write_answers(profile_id, data)
    return h


# ---------------------------------------------------------------- editable standing answers (the "Réponses aux formulaires" page)
class AnswerField(BaseModel):
    """One editable answer: `path` is the key path in answers.yaml ("stage.available_from" = answers["stage"]["available_from"])."""
    path: str
    label: str
    help: str = ""
    widget: Literal["text", "date", "select", "textarea"] = "text"
    options: tuple[str, ...] = ()       # select: the allowed values (the first, "", means "not answered")
    section: str
    max_len: int = 500
    placeholder: str = ""


S_ID, S_ADMIN, S_AVAIL, S_PAY, S_MOB = ("Identité et coordonnées", "Situation administrative", "Disponibilité", "Rémunération",
                                         "Mobilité et langues")
ANSWER_SECTIONS: tuple[str, ...] = (S_ID, S_ADMIN, S_AVAIL, S_PAY, S_MOB)
_F = AnswerField
ANSWER_FIELDS: tuple[AnswerField, ...] = (
    _F(path="civility", label="Civilité", widget="select", options=("", "M.", "Mme"), section=S_ID),
    _F(path="first_name", label="Prénom", section=S_ID,
       help="À remplir seulement si le découpage automatique du nom est faux."),
    _F(path="last_name", label="Nom de famille", section=S_ID,
       help="À remplir seulement si le découpage automatique du nom est faux."),
    _F(path="birth_date", label="Date de naissance", widget="date", section=S_ID),
    _F(path="birth_place", label="Lieu de naissance", section=S_ID, placeholder="Toulouse"),
    _F(path="nationality", label="Nationalité", section=S_ID, placeholder="Française"),
    _F(path="address", label="Adresse (numéro et rue)", section=S_ID, placeholder="12 rue des Lilas"),
    _F(path="postal_code", label="Code postal", section=S_ID),
    _F(path="city", label="Ville", section=S_ID),
    _F(path="department", label="Département", section=S_ID, placeholder="Haute-Garonne"),
    _F(path="country", label="Pays", section=S_ID, placeholder="France"),
    _F(path="work_authorization", label="Autorisation de travail en France", widget="textarea", max_len=1000, section=S_ADMIN,
       help="Nationalité ou titre de séjour, et ce qui permet de travailler en France."),
    _F(path="needs_visa_sponsorship", label="Besoin d'un visa ou d'un permis de travail", widget="select",
       options=("", "Non", "Oui"), section=S_ADMIN, help="Avez-vous besoin d'un visa ou d'un permis de travail ?"),
    _F(path="driving_licence", label="Permis de conduire", widget="select", options=("", "Oui (permis B)", "Non"), section=S_ADMIN),
    _F(path="vehicle", label="Véhicule personnel", widget="select", options=("", "Oui", "Non"), section=S_ADMIN),
    _F(path="disability", label="Reconnaissance de travailleur handicapé (RQTH)", widget="select",
       options=("", "Non", "Oui (RQTH)", "Ne souhaite pas répondre"), section=S_ADMIN),
    _F(path="criminal_record", label="Casier judiciaire", widget="select",
       options=("", "Casier judiciaire vierge", "Ne souhaite pas répondre"), section=S_ADMIN),
    _F(path="graduation_date", label="Date d'obtention du diplôme (prévue)", widget="date", section=S_AVAIL,
       help="Fin prévue du cursus en cours ; beaucoup de formulaires la demandent."),
    _F(path="internship_agreement", label="L'école fournit une convention de stage", widget="select",
       options=("", "Oui", "Non"), section=S_AVAIL),
    _F(path="stage.available_from", label="Début de stage possible à partir du", widget="date", section=S_AVAIL),
    _F(path="stage.duration", label="Durée du stage", section=S_AVAIL, placeholder="6 mois"),
    _F(path="alternance.start", label="Début d'alternance", widget="date", section=S_AVAIL),
    _F(path="alternance.duration", label="Durée de l'alternance", section=S_AVAIL, placeholder="24 mois"),
    _F(path="alternance.rhythm", label="Rythme d'alternance", section=S_AVAIL, placeholder="3 semaines en entreprise / 1 semaine en cours"),
    _F(path="notice_period", label="Préavis", section=S_AVAIL, placeholder="Aucun (étudiant)"),
    _F(path="salary_expectation.stage", label="Prétentions salariales (stage)", section=S_PAY, placeholder="Gratification légale"),
    _F(path="salary_expectation.alternance", label="Prétentions salariales (alternance)", section=S_PAY,
       placeholder="Selon la grille légale"),
    _F(path="current_salary", label="Rémunération actuelle", section=S_PAY, placeholder="Aucun (étudiant)"),
    _F(path="mobility", label="Mobilité géographique", section=S_MOB, placeholder="Toute la France"),
    _F(path="languages", label="Langues", section=S_MOB, placeholder="Français (natif), anglais (B2)"),
)
ANSWER_BY_PATH: dict[str, AnswerField] = {f.path: f for f in ANSWER_FIELDS}
COUNTRY_SUGGESTION = "France"

_FR_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre")
_FR_MONTH_NUM = {unicodedata.normalize("NFKD", m).encode("ascii", "ignore").decode(): i for i, m in enumerate(_FR_MONTHS, 1)}
_FR_DATE = re.compile(r"\s*(\d{1,2})(?:er)?\s+([a-zéû]+)\s+((?:19|20)\d{2})\s*", re.I)


def to_french_date(iso: str) -> str:
    """"2027-01-04" -> "4 janvier 2027" ("1er janvier 2027" for the first). ValueError for anything that is not an ISO date."""
    d = date.fromisoformat((iso or "").strip())
    return f"{'1er' if d.day == 1 else d.day} {_FR_MONTHS[d.month - 1]} {d.year}"


def from_french_date(text: str | None) -> str | None:
    """"4 janvier 2027" / "1er janvier 2027" -> "2027-01-04"; None when the text is not exactly one complete, valid French date
    (a free text such as "janvier 2027" or "dès que possible")."""
    m = _FR_DATE.fullmatch(text or "")
    if not m:
        return None
    month = _FR_MONTH_NUM.get(unicodedata.normalize("NFKD", m.group(2).lower()).encode("ascii", "ignore").decode())
    try:
        return date(int(m.group(3)), month, int(m.group(1))).isoformat() if month else None
    except ValueError:
        return None


def get_answer(answers: dict, path: str) -> str | None:
    """The stored text at a dotted key path ("stage.available_from"), None when absent."""
    return _dig(answers, *path.split("."))


def _clean_value(f: AnswerField, raw: Any) -> str:
    """The value to store for `f` ("" = remove the key): stripped, capped, validated. ApplyError on a bad select / date."""
    text = str(raw or "").replace("\r\n", "\n").strip()
    if f.widget != "textarea":
        text = " ".join(text.split())
    text = text[:f.max_len].strip()
    if not text:
        return ""
    if f.widget == "select" and text not in f.options:
        raise ApplyError(f"Valeur non autorisée pour « {f.label} »")
    if f.widget == "date":
        iso = text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else from_french_date(text)
        try:
            return to_french_date(iso or "")
        except ValueError:
            raise ApplyError(f"Date invalide pour « {f.label} »") from None
    return text


def _set_path(data: dict, path: str, value: str) -> None:
    keys = path.split(".")
    if not value:
        node = data
        parents = []
        for k in keys[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
            parents.append(node)
        if isinstance(node, dict):
            node.pop(keys[-1], None)
            if len(keys) > 1 and not node:  # an emptied block disappears
                parent = data
                for k in keys[:-2]:
                    parent = parent[k]
                parent.pop(keys[-2], None)
        return
    node = data
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


def save_answers(profile_id: int, values: dict[str, str]) -> None:
    """Store the given answers (`path` -> text) in ONE atomic write. A path absent from `values` is left alone; an empty
    value REMOVES the key (unknown, not empty), except for a date widget whose stored text is not a complete date (legacy
    "janvier 2027"): that text is kept unless a date is picked. Unknown keys, `custom` and `trusted_hosts` are preserved.
    Values are stripped and capped (500 characters, 1000 for textareas); a select value must be one of its options.
    Raises ApplyError (nothing written) for an unknown path, a value outside the options or an invalid date."""
    unknown = [p for p in values if p not in ANSWER_BY_PATH]
    if unknown:
        raise ApplyError("Réponse inconnue : " + ", ".join(unknown))
    cleaned = {p: _clean_value(ANSWER_BY_PATH[p], v) for p, v in values.items()}
    data = load_answers(profile_id)
    for path, value in cleaned.items():
        f = ANSWER_BY_PATH[path]
        if not value and f.widget == "date":
            stored = get_answer(data, path)
            if stored and from_french_date(stored) is None:
                continue
        _set_path(data, path, value)
    _write_answers(profile_id, data)


def custom_id(question: str) -> str:
    """Stable id of a custom answer: the first 16 hex characters of the sha256 of the normalized question (it does not move
    when another entry is added or removed, unlike a list index)."""
    return hashlib.sha256(_norm_question(question).encode("utf-8")).hexdigest()[:16]


def custom_answers(profile_id: int) -> list[dict]:
    """The `custom` entries (id, question, answer) in the order build_facts numbers them (custom_1, custom_2...)."""
    return [{"id": custom_id(str(c.get("question") or "")), "question": str(c.get("question") or ""), "answer": str(c.get("answer") or "")}
            for c in (load_answers(profile_id).get("custom") or []) if isinstance(c, dict)]


def _custom_entry(data: dict, custom_id_: str) -> dict | None:
    raw = data.get("custom") if isinstance(data.get("custom"), list) else []
    return next((c for c in raw if isinstance(c, dict) and custom_id(str(c.get("question") or "")) == custom_id_), None)


def delete_custom_answer(profile_id: int, custom_id_: str, previous: str) -> bool:
    """Remove the custom answer `custom_id_` only if its stored answer is still `previous` (what the page displayed).
    False (nothing written) when it changed or disappeared meanwhile."""
    data = load_answers(profile_id)
    entry = _custom_entry(data, custom_id_)
    if entry is None or str(entry.get("answer") or "") != previous:
        return False
    data["custom"] = [c for c in data["custom"] if c is not entry]
    _write_answers(profile_id, data)
    return True


def update_custom_answer(profile_id: int, custom_id_: str, answer: str, previous: str) -> bool:
    """Replace the answer of `custom_id_` (stripped, 500 characters max) only if the stored answer is still `previous`.
    False (nothing written) when it changed or disappeared meanwhile. An empty answer raises ApplyError (use delete)."""
    answer = " ".join((answer or "").split())[:500]
    if not answer:
        raise ApplyError("Réponse vide : utiliser « Retirer » pour la supprimer")
    data = load_answers(profile_id)
    entry = _custom_entry(data, custom_id_)
    if entry is None or str(entry.get("answer") or "") != previous:
        return False
    entry["answer"] = answer
    _write_answers(profile_id, data)
    return True


def remove_trusted_host(profile_id: int, host: str) -> bool:
    """Remove a host from `trusted_hosts`; True when it was there. Other entries are untouched."""
    h = valid_host(host)
    data = load_answers(profile_id)
    raw = data.get("trusted_hosts") if isinstance(data.get("trusted_hosts"), list) else []
    kept = [x for x in raw if not (isinstance(x, str) and valid_host(x) == h and h)]
    if len(kept) == len(raw):
        return False
    if kept:
        data["trusted_hosts"] = kept
    else:
        data.pop("trusted_hosts", None)
    _write_answers(profile_id, data)
    return True


# ---------------------------------------------------------------- field compatibility (Fact.label_re)
# The engine applies these with re.search + IGNORECASE on the accent-stripped lowercase VISIBLE label of the field (name and
# placeholder only when the label is empty): an exact identity fact may only land in a field that looks like it. The negative
# lookahead at the start rejects third-party contacts (referees, supervisors, sponsors, school, company, emergency contact...)
# for EVERY identity fact; names also reject birth / maiden / usage names (they become unknown questions).
_THIRD_PARTY = ("parrain|referr|r[eé]f[eé]ren|reference|recommand|cooptation|entreprise|company|employeur|employer|urgence|"
                "emergency|manager|tuteur|superviseur|supervisor|responsable|ma[iî]tre|parent|conjoint|contact person|"
                r"personne [aà] contacter|recruteur|recruiter|[eé]cole|school|"
                r"referee|mentor|garant|witness|t[eé]moin|sponsor|guardian|tuteur l[eé]gal|next of kin|(?<![a-z])proche")
_NOT_THE_NAME = "naissance|jeune fille|birth|maiden|usage"
_NOT_A_PERSON_NAME = _THIRD_PARTY + "|" + _NOT_THE_NAME + "|utilisateur|user|login|identifiant|file.?name|fichier|domain|pseudo"
_FULL_NAME = r"nom complet|nom et prenom|prenom et nom|full.?name"


def _label_re(pattern: str, excluded: str = _THIRD_PARTY) -> str:
    return rf"(?s)\A(?!.*(?:{excluded})).*?(?:{pattern})"


LABEL_RE: dict[str, str] = {
    "first_name": _label_re(r"prenom|first.?name|given.?name|forename|vorname", _NOT_A_PERSON_NAME + "|" + _FULL_NAME),
    "last_name": _label_re(r"(?<![a-z])nom(?![a-z])|last.?name|family.?name|surname|nachname",
                           _NOT_A_PERSON_NAME + "|" + _FULL_NAME + r"|votre nom|your name|nom\s+d(?:u|es)\b|nom\s+de\W+(?!famille)"),
    "full_name": _label_re(_FULL_NAME + r"|your name|votre nom|(?<![a-z])name(?![a-z])",
                           _NOT_A_PERSON_NAME + r"|first|last|given|family|surname|forename|nachname|vorname"),
    "email": _label_re("mail|courriel"),
    "phone": _label_re(r"(?<![a-z])tel(?![a-z])|telephone|phone|mobile|portable|(?<![a-z])gsm"),
    "linkedin": _label_re("linkedin"),
    "account_password": _label_re("mot de passe|password|passwort|mdp|kennwort|contrase[nñ]a|senha|wachtwoord|has[lł]o|parola|密码"),
}
LABEL_RE["account_email"] = LABEL_RE["email"]


# ---------------------------------------------------------------- helpers
def _s(v: Any) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _dig(d: Any, *path: str) -> str | None:
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return _s(d)


def phone_variants(phone: str | None) -> list[str]:
    """The same French number written the usual ways (empty when it is not a French number)."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0033"):
        digits = digits[2:]
    if digits.startswith("33") and len(digits) == 11:
        national = "0" + digits[2:]
    elif digits.startswith("0") and len(digits) == 10:
        national = digits
    else:
        return []
    rest = national[1:]
    pairs = lambda s: " ".join(s[i:i + 2] for i in range(0, len(s), 2))  # noqa: E731
    out = [f"+33 {rest[0]} {pairs(rest[1:])}", f"+33{rest}", national, f"0{rest[0]} {pairs(rest[1:])}"]
    return [v for v in dict.fromkeys(out) if v != (phone or "").strip()]


def linkedin_variants(url: str | None) -> list[str]:
    bare = re.sub(r"^(?:https?://)?(?:www\.)?", "", (url or "").strip()).rstrip("/")
    if not bare:
        return []
    out = [bare, f"www.{bare}", f"https://{bare}", f"https://www.{bare}"]
    return [v for v in dict.fromkeys(out) if v != (url or "").strip()]


def _latest_education(master: Master):
    """Most recent entry by the latest year in its dates ("en cours" = now); ties keep the master order."""
    def key(e):
        years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", e.dates)]
        ongoing = bool(re.search(r"en cours|présent|present|actuel", e.dates, re.I))
        return (9999 if ongoing else max(years, default=0))
    return max(master.education, key=key, default=None)  # max returns the first of equal keys


def _source_label(job: MergedJob) -> str | None:
    from jobhunt.sources import registry

    if not job.sightings:
        return None
    name = job.sightings[0].source
    cls = registry().get(name)
    return getattr(cls, "label", None) or name


# ---------------------------------------------------------------- the sheet
def build_facts(profile: Profile, master: Master | None, answers: dict, job: MergedJob, *, account_email: str | None = None,
                today: date | None = None) -> FactSheet:
    facts: list[Fact] = []

    def add(key: str, label: str, value: Any = None, **kw) -> None:
        facts.append(Fact(key=key, label=label, value=value if kw.get("secret") else _s(value), **kw))  # secret: "" placeholder

    ident = master.identity if master else None
    full_name = _s(ident.name if ident else None) or _s(profile.name)
    parts = (full_name or "").split()
    email = _s(ident.email if ident else None) or _s(profile.email)
    phone = _s(ident.phone if ident else None) or _s(profile.phone)
    linkedin = _s(ident.linkedin if ident else None)
    add("full_name", "Nom complet", full_name, exact=True, label_re=LABEL_RE["full_name"])
    add("first_name", "Prénom", _s(answers.get("first_name")) or (parts[0] if parts else None), exact=True,
        label_re=LABEL_RE["first_name"])
    add("last_name", "Nom de famille", _s(answers.get("last_name")) or (" ".join(parts[1:]) or None), exact=True,
        label_re=LABEL_RE["last_name"])
    add("email", "Adresse e-mail", email, exact=True, label_re=LABEL_RE["email"])
    add("phone", "Téléphone", phone, exact=True, variants=phone_variants(phone), label_re=LABEL_RE["phone"])
    add("linkedin", "Profil LinkedIn", linkedin, exact=True, variants=linkedin_variants(linkedin), label_re=LABEL_RE["linkedin"])
    for key, label in (("civility", "Civilité"), ("gender", "Genre"), ("birth_date", "Date de naissance"),
                       ("nationality", "Nationalité"), ("address", "Adresse postale"), ("postal_code", "Code postal"),
                       ("city", "Ville"), ("country", "Pays"), ("driving_licence", "Permis de conduire"),
                       ("disability", "Reconnaissance de travailleur handicapé (RQTH)"),
                       ("birth_place", "Lieu de naissance"), ("department", "Département de résidence"),
                       ("needs_visa_sponsorship", "Besoin d'un visa ou d'un permis de travail (sponsoring)"),
                       ("vehicle", "Véhicule personnel"), ("criminal_record", "Casier judiciaire (déclaration)"),
                       ("notice_period", "Préavis"), ("current_salary", "Rémunération actuelle"),
                       ("graduation_date", "Date d'obtention du diplôme en cours (prévue) / graduation date"),
                       ("internship_agreement", "L'école fournit une convention de stage (internship agreement)")):
        add(key, label, answers.get(key))
    add("work_authorization", "Autorisation de travail / situation administrative en France", answers.get("work_authorization"))
    add("mobility", "Mobilité géographique", answers.get("mobility"))
    add("languages_summary", "Langues parlées (résumé)", answers.get("languages"))
    for i, lang in enumerate(master.languages if master else [], 1):
        level = re.sub(r"(?i)^niveau\s+", "", lang.level.strip()).rstrip(". ")
        add(f"language_{i}", f"Langue : {lang.name}", f"{lang.name} : {level}" if level else lang.name)
    edu = _latest_education(master) if master else None
    add("school", "École / établissement actuel", edu.school if edu else None)
    add("degree", "Diplôme / formation en cours", edu.title if edu else None)
    add("education_level", "Niveau d'études (formation la plus récente)",
        _s(answers.get("education_level")) or (f"{edu.title}, {edu.school} ({edu.dates})" if edu and edu.dates else
                                               (f"{edu.title}, {edu.school}" if edu else None)))
    add("current_title", "Intitulé actuel", ident.default_title if ident else None)
    add("candidate_profile", "Profil du candidat (CV maître) — sert uniquement à déduire une réponse plausible",
        profile_digest(master))  # not exact: never typed verbatim, no label_re
    add("today", "Date du jour", (today or datetime.now(ZoneInfo("Europe/Paris")).date()).isoformat())

    # contract-dependent answers
    ct = job.contract_type
    kinds = ["stage"] if ct == ContractType.STAGE else ["alternance"] if ct == ContractType.ALTERNANCE else ["stage", "alternance"]
    for kind in kinds:
        pre = "" if len(kinds) == 1 else f"{kind}_"
        tag = "" if len(kinds) == 1 else f" ({kind})"
        block = answers.get(kind)
        add(f"{pre}contract_sought", f"Type de contrat recherché{tag}", "Stage" if kind == "stage" else "Alternance")
        add(f"{pre}available_from", f"Date de début / disponibilité{tag}",
            _dig(block, "available_from") if kind == "stage" else _dig(block, "start"))
        add(f"{pre}duration", f"Durée{tag}", _dig(block, "duration"))
        if kind == "alternance":
            add(f"{pre}rhythm", f"Rythme d'alternance{tag}", _dig(block, "rhythm"))
        add(f"{pre}salary_expectation", f"Prétentions salariales{tag}", _dig(answers, "salary_expectation", kind))

    add("job_source", "Où avez-vous vu l'offre ? (source réelle de l'annonce)", _source_label(job))
    add("job_title", "Intitulé du poste", job.title)
    add("company", "Entreprise", job.company)
    add("consent_required", CONSENT_REQUIRED_LABEL, "oui")
    add("consent_optional", CONSENT_OPTIONAL_LABEL, "non")
    add("account_email", "E-mail du compte candidat", _s(account_email) or email, exact=True,
        label_re=LABEL_RE["account_email"])
    add("account_password", "Mot de passe du compte candidat", "", exact=True, secret=True, label_re=LABEL_RE["account_password"])
    for i, c in enumerate([c for c in (answers.get("custom") or []) if isinstance(c, dict)], 1):
        add(f"custom_{i}", str(c.get("question") or f"Question {i}"), c.get("answer"))
    return FactSheet(facts=facts)


PROFILE_DIGEST_MAX = 2500


def profile_digest(master: Master | None, limit: int = PROFILE_DIGEST_MAX) -> str | None:
    """Compact plain-text digest of the master CV (title, summary, education, skills, languages, projects), capped."""
    if master is None:
        return None
    ident, lines = master.identity, []
    if ident.default_title:
        lines.append(f"Intitulé : {ident.default_title}")
    if master.summary.strip():
        lines.append(f"Résumé : {' '.join(master.summary.split())}")
    for e in master.education:
        lines.append("Formation : " + ", ".join(x for x in (e.title, e.school, e.dates) if x) + (f" ({e.details})" if e.details else ""))
    for g in master.skill_groups:
        lines.append(f"Compétences ({g.label}) : " + ", ".join(g.items))
    if master.languages:
        lines.append("Langues : " + ", ".join(f"{lang.name} {lang.level}".strip() for lang in master.languages))
    for p in master.projects:
        lines.append("Projet : " + " — ".join(x for x in (p.title, p.dates, p.context) if x))
    text = "\n".join(" ".join(l.split()) for l in lines)
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text or None


def facts_for_llm(sheet: FactSheet) -> list[dict]:
    """What the planner LLM may see. Secret values never leave the process."""
    return [{"key": f.key, "label": f.label, "value": SECRET_PLACEHOLDER if f.secret else f.value,
             "known": f.value not in (None, "") or f.secret} for f in sheet.facts]
