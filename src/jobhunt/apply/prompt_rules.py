"""Shared classification of candidate prompts; never inspects answer values."""

import re
import unicodedata


def norm_question(label: str) -> str:
    plain = "".join(char for char in unicodedata.normalize("NFKD", label or "") if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", plain.casefold()).strip()


CREDENTIAL_ERROR = "Cette question doit être traitée sur le site ; aucune réponse n’a été enregistrée."


def _plain_field_label(label: str) -> bool:
    """A label naming an ordinary profile datum (city, phone, name...), never a one-time code."""
    text = norm_question(label)
    return bool(re.search(r"\b(?:ville|city|town|telephone|phone|mobile|portable|tel|numero de tel\w*|prenom|first name|"
                          r"nom de famille|last name|surname|family name|email|e mail|adresse|address|pays|country|"
                          r"code postal|postal code|zip|region|state|province|nationalite|nationality)\b", text)
                and not re.search(r"verification|securite|security|usage unique|one time|otp|sms|connexion|validation", text))


def technical_key(label: str, kind: str = "", *, group: str = "", autocomplete: str = "") -> str | None:
    tokens = set(autocomplete.lower().split())
    if tokens & {"current-password", "new-password"}:
        return "credentials"
    if "one-time-code" in tokens and not _plain_field_label(label):
        # Some ATS (Eightfold) put autocomplete="one-time-code" on phone / city inputs only to stop browser autofill:
        # a field that names an ordinary profile datum is not a verification code.
        return "credentials"
    if group and technical_key(group) == "credentials":
        return "credentials"
    label = norm_question(label)
    if kind == "password" or re.search(r"mot de passe|password|passwort|kennwort|contrase[nñ]a|senha|wachtwoord|\bmdp\b|passcode", label):
        return "credentials"
    if re.search(r"\b(?:otp|totp|one time code|one time password|verification code|security code|code de verification|"
                 r"code de securite|code a usage unique|code de connexion|code de validation|einmalcode|bestatigungscode|sicherheitscode)\b", label):
        return "credentials"
    if label in {"confirmation", "confirm", "bestatigung", "wiederholen", "repeat", "confirmer"}:
        return "credentials"
    if re.search(r"compte (?:requis|obligatoire)|creation de compte|connexion (?:impossible|requise)|login|log in|sign in|account (?:is required|required|creation)|"
                 r"anmeldung|anmelden|konto (?:erstellen|erforderlich)", label):
        return "account"
    if label == "cv personale":
        return "cv_upload"
    if label == "photo jpg":
        return "photo_upload"
    if label in {"lettre de motivation", "cover letter", "message"} and kind != "file":
        return "application_material"
    if kind == "file" or re.search(r"depot du fichier|fichier a joindre|file upload|upload failed|datei hochladen", label) \
            or (kind in {"button", "link"} and re.search(r"joindre|televerser|upload|attach|hochladen", label)):
        if re.search(r"\bcv\b|lebenslauf|\bresume\b|curriculum", label):
            return "cv_upload"
        if re.search(r"lettre de motivation|cover letter|anschreiben", label):
            return "letter_upload"
        if re.search(r"photo|foto", label):
            return "photo_upload"
        return "file_upload"
    return None


def requires_site_consent(label: str) -> bool:
    return bool(re.search(r"\b(?:privacy|rgpd|gdpr|dsgvo|datenschutz\w*|confidentialite|consent\w*|einwillig\w*|"
                          r"terms|cgu|conditions generales|bedingungen|agb|personal data|donnees personnelles|"
                          r"data processing|traitement des donnees|background check|i agree|i accept|i authorize|"
                          r"j accepte|j autorise|je consens|je renonce|ich stimme zu)\b", norm_question(label)))


def decision_key(label: str) -> str | None:
    """Only group recognizable decisions, never convert a choice between polarities."""
    label = norm_question(label)
    if re.search(r"\b(?:ia|ai|ki)\b|intelligence artificielle|artificial intelligence|kunstliche intelligenz", label):
        if re.search(r"\b(?:opt out|decline|refus\w*|nicht|widerspr\w*|ablehn\w*|deactiv\w*|desactiv\w*|without|sans|no|not|kein\w*)\b", label):
            return "ai_opt_out"
        if re.search(r"\b(?:opt in|consent\w*|agree|accept\w*|autorise\w*|zustimm\w*|einwillig\w*|yes|enable|allow)\b", label):
            return "ai_opt_in"
        return "ai_review"
    if optional_recruiting(label):
        return "recruiting"
    if requires_site_consent(label):
        return "consent"
    if re.search(r"conflict of interest|conflit d interet|interessenkonflikt", label):
        return "conflict"
    if re.search(r"former employer|previously employed|previously worked (?:for|at|with us)|already worked (?:for|at|with us)|"
                 r"deja travaille (?:chez|pour|avec nous)|ancien employeur|"
                 r"fruher.*(?:gearbeitet|beschaftigt)|ehemalig\w* arbeitgeber", label):
        return "former_employer"
    if re.search(r"newsletter|marketing|werbung|commercial communications|communications commerciales|publicite", label):
        return "marketing"
    if re.search(r"talent pool|talent community|vivier de talent|talentpool|future opportunit|offres futures|kunftige stellen", label):
        return "recruiting"
    if label in {"country", "pays", "land", "country region", "pays region", "land region", "country code", "code pays"}:
        return "ambiguous_country"
    if re.search(r"work authori[sz]ation|authori[sz]ed to work|right to work|sponsor\w*|autorisation de travail|"
                 r"autorise a travailler|permis de travail|arbeitserlaubnis|arbeitsberechtigung", label):
        return "work_authorization_review"
    return None


def optional_recruiting(label: str) -> bool:
    return bool(re.search(r"other job opportunities|receive update messages|(?:j accepte|je souhaite) d? ?etre sollicite|"
                          r"j accepte de recevoir des messages|job alerts?|alertes? (?:emploi|offres?)|"
                          r"talent pool|talent community|vivier|future opportunit|offres futures", norm_question(label)))


def recruiter_ai_polarity(label: str) -> bool | None:
    text = norm_question(label)
    if not re.search(r"\b(?:recruiter|recruteur)\b", text) or not re.search(r"\b(?:assist\w*|aid\w*|help\w*|support\w*)\b", text):
        return None
    choice = decision_key(label)
    return {"ai_opt_in": True, "ai_opt_out": False}.get(choice)


def is_manual_choice(label: str) -> bool:
    return decision_key(label) is not None


# Country-only phone-country lists ("France"): a calling code is tied to a country by name only where that is unambiguous.
DIAL_COUNTRY_NAMES = {"+33": frozenset({"france", "france metropolitaine"}), "+49": frozenset({"germany", "allemagne", "deutschland"}),
                      "+44": frozenset({"united kingdom", "royaume uni", "grossbritannien", "vereinigtes konigreich"})}


def international_calling_code(phone: str) -> str | None:
    """Recognize supported international formats without converting a domestic number into one."""
    if not re.fullmatch(r"(?:\+|00)[1-9][0-9 .-]+", phone.strip()):
        return None
    digits = re.sub(r"[ .-]", "", phone.strip())
    digits = digits[1:] if digits.startswith("+") else digits[2:]
    for code, national in (("33", r"[1-9][0-9]{8}"), ("49", r"[1-9][0-9]{6,11}"),
                           ("44", r"[1-9][0-9]{9}"), ("1", r"[2-9][0-9]{2}[2-9][0-9]{6}")):
        if digits.startswith(code) and re.fullmatch(national, digits[len(code):]):
            return "+" + code
    return None


def french_phone(phone: str) -> str:
    """Return the nine national digits of an unambiguous French-format phone."""
    value = re.sub(r"[ .()-]", "", phone.strip())
    match = re.fullmatch(r"(?:0|\+33|0033)([1-9][0-9]{8})", value)
    return match[1] if match else ""


def device_type(label: str) -> str | None:
    label = norm_question(label)
    if label in {"mobile", "cell", "cell phone", "portable", "mobil", "mobiltelefon", "handy"}:
        return "Mobile"
    if label in {"fixe", "landline", "fixed line", "festnetz", "festnetztelefon"}:
        return "Fixe"
    return None
