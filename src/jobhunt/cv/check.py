"""Rule-based fact check of a tailored CV against the master. Pure: no I/O, no LLM.

errors  (block approval until fixed or explicitly overridden; never auto-eligible): skill not in master, bullet without
        valid source ids or citing another project, altered title/dates/school/context/details/language, unknown ids,
        numbers / proper nouns / tools in a bullet absent from the CITED sources, in the summary or a skill-group label
        or the headline absent from the master.
warnings ("à vérifier"): long summary.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from jobhunt.cv.master import Master, norm_text
from jobhunt.cv.tailored import TailoredCV
from jobhunt.dedupe import strip_accents

SUMMARY_MAX_CHARS = 420


class Flag(BaseModel):
    # error = hard fact a recruiter can verify (blocks approval / auto-submit); warning = cosmetic;
    # prep = embellishment beyond the master (allowed, listed in "Préparation entretien" with a hint for the candidate)
    level: Literal["error", "warning", "prep"]
    code: str
    location: str  # "skill_groups[0].items[2]", "projects[1].bullets[0]", "summary"...
    message: str
    term: str | None = None
    hint: str | None = None


def prep_hint(term: str) -> str:
    return f"Tu as écrit « {term} » : prépare une explication concrète (bases, exemple tiré d'un projet)."


# Language-level claims are hard facts: CEFR levels and "bilingue / natif / langue maternelle" must come from the master.
LANG_LEVEL_RE = re.compile(r"\b(?:[ABC][12]|bilingue|natif|native|langue maternelle|courant|courante|fluent|toeic|toefl|ielts)\b", re.I)


# Known tools/languages/protocols/standards. Matched case-insensitively (ambiguous ones, see AMBIGUOUS, only when written
# as in the dictionary below, e.g. "C", "R", "Go", "CAN").
LEXICON = {
    # languages
    "python", "java", "javascript", "typescript", "rust", "c", "c++", "c#", "go", "r", "matlab", "simulink", "scilab",
    "labview", "vhdl", "verilog", "systemverilog", "assembleur", "assembly", "bash", "perl", "kotlin", "swift", "php",
    "sql", "lua", "scala", "julia", "ada", "fortran", "cobol", "arm", "risc-v", "riscv", "labview",
    # mcus / boards / hardware
    "arduino", "esp32", "esp8266", "stm32", "pic", "atmega", "avr", "raspberry", "jetson", "beaglebone", "fpga", "dsp",
    "cortex", "nucleo", "teensy", "zynq", "xilinx", "altera", "aurix", "tricore", "infineon", "nxp", "microchip", "plc",
    # protocols / buses
    "uart", "spi", "i2c", "can", "canfd", "lin", "flexray", "modbus", "profibus", "profinet", "ethercat", "opc", "opcua",
    "mqtt", "ble", "bluetooth", "wifi", "lora", "lorawan", "zigbee", "usb", "rs232", "rs485", "tcp", "udp", "http", "rest",
    "uds", "someip", "doip", "ethernet",
    # software / tooling
    "linux", "ubuntu", "windows", "docker", "kubernetes", "git", "gitlab", "github", "jenkins", "ci/cd", "jira", "confluence",
    "ros", "ros2", "gazebo", "freertos", "zephyr", "rtos", "yocto", "buildroot", "autosar", "vscode", "eclipse", "keil",
    "stm32cubeide", "cubemx", "platformio", "cmake", "make", "gcc", "gdb", "openocd", "jtag", "swd", "tensorflow", "pytorch",
    "opencv", "numpy", "pandas", "scikit-learn", "sklearn", "excel", "powerbi", "sap", "tableau", "node", "react", "angular",
    "django", "flask", "fastapi", "spring", "aws", "azure", "gcp", "terraform", "ansible", "labview",
    # CAD / simulation
    "solidworks", "catia", "autocad", "inventor", "fusion", "creo", "nx", "ansys", "comsol", "abaqus", "kicad", "altium",
    "eagle", "orcad", "proteus", "ltspice", "spice", "cadence", "modelsim", "vivado", "quartus", "freecad", "blender",
    # standards / methods
    "iso", "iec", "misra", "do-178", "do-254", "ecss", "aspice", "cmmi", "scrum", "agile", "safe", "devops", "uml", "sysml",
    "mbse", "fmea", "amdec", "6sigma", "lean", "kanban", "tdd", "ci",
}
AMBIGUOUS = {"c", "r", "go", "can", "lin", "pic", "sap", "ros", "node", "make", "fusion", "nx", "ci", "safe", "rest",
             "opc", "spice", "ada", "arm", "cortex", "eclipse", "swift"}

# Generic vocabulary acronyms that describe what any embedded student did: not worth an "à vérifier".
GENERIC_OK = {"hw", "sw"}

STANDARD_RE = re.compile(r"\b(ISO|IEC|IEEE|DO|EN|ECSS|MISRA|ASPICE|SAE|ARP|NF|RFC)[\s\-]*([0-9][0-9A-Za-z]*(?:[.\-/:][0-9A-Za-z]+)*)")
TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]+(?:\.[A-Za-zÀ-ÿ0-9]+)*[+#]*")


def _n(s: str) -> str:
    return strip_accents(s).lower()


def _key(s: str) -> str:
    return re.sub(r"[\s\-_]+", "", _n(s))


def _items_key(s: str) -> str:
    return re.sub(r"\s+", " ", _n(s)).strip(" .;:")


def _is_technical(tok: str) -> bool:
    if len(tok) == 1 and tok != "C":
        return False
    low = tok.lower()
    if low in GENERIC_OK:
        return False
    if any(ch.isdigit() for ch in tok):
        return True
    if len(tok) >= 2 and tok.isupper():
        return True
    if re.search(r"[a-zà-ÿ][A-Z]", tok):  # SolidWorks, PyTorch, FreeRTOS
        return True
    if low in LEXICON or _n(tok) in LEXICON:
        if low in AMBIGUOUS:
            return tok.isupper() or tok in {"Go", "R", "C"} or tok == "Ada"
        return True
    return False


def technical_terms(text: str) -> dict[str, str]:
    """{normalised term: term as written} for technical-looking terms of a text."""
    out: dict[str, str] = {}
    covered: list[tuple[int, int]] = []  # spans of "ISO 26262"-style standards: their parts are not flagged again
    for m in STANDARD_RE.finditer(text):
        out.setdefault("std:" + _key(m.group(0)), m.group(0))
        covered.append(m.span())
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0)
        if any(a <= m.start() and m.end() <= b for a, b in covered):
            continue
        if tok == "C" and text[m.end():m.end() + 1] in ("'", "’"):  # French elision: "C'est"
            continue
        if _is_technical(tok):
            out.setdefault(_n(tok), tok)
    return out


NUMBER_WORDS = {"zero": "0", "deux": "2", "trois": "3", "quatre": "4", "cinq": "5", "six": "6", "sept": "7", "huit": "8",
                "neuf": "9", "dix": "10", "onze": "11", "douze": "12", "treize": "13", "quatorze": "14", "quinze": "15",
                "seize": "16", "vingt": "20", "trente": "30", "quarante": "40", "cinquante": "50", "soixante": "60",
                "cent": "100", "mille": "1000", "dizaine": "dizaine", "douzaine": "douzaine", "centaine": "centaine",
                "demi": "0.5", "double": "2", "triple": "3"}
NUMBER_RE = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")
SENTENCE_END = ".!?:;\n"


def _stem(w: str) -> str:
    w = _n(w)
    return w[:-1] if len(w) > 3 and w[-1] in "sx" else w


def _num(s: str) -> str:
    return s.replace(",", ".")


def numbers_in(text: str) -> dict[str, str]:
    """{normalised number: as written}: digits and French number words (cinq == 5)."""
    out = {"num:" + _num(m.group(0)): m.group(0) for m in NUMBER_RE.finditer(text)}
    for m in re.finditer(r"[A-Za-zÀ-ÿ]+", text):
        w = _stem(m.group(0))
        w = w if w in NUMBER_WORDS else _n(m.group(0))
        if w in NUMBER_WORDS:
            out.setdefault("num:" + NUMBER_WORDS[w], m.group(0))
    return out


def proper_nouns(text: str) -> dict[str, str]:
    """Capitalised words that are not at the start of a sentence: organisations, places, products."""
    out: dict[str, str] = {}
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0)
        if not tok[0].isupper() or tok.isupper() or len(tok) < 2:
            continue
        before = text[:m.start()].rstrip()
        if not before or before[-1] in SENTENCE_END:
            continue
        out.setdefault("np:" + _stem(tok), tok)
    return out


def claim_terms(text: str, *, proper: bool = True) -> dict[str, str]:
    """Everything a recruiter could check: numbers, tools/acronyms/standards, proper nouns."""
    out = dict(numbers_in(text))
    for k, v in technical_terms(text).items():
        if not any(ch.isdigit() for ch in v) or not v.isdigit():  # bare numbers are handled by numbers_in
            out.setdefault(k, v)
    if proper:
        for k, v in proper_nouns(text).items():
            out.setdefault(k, v)
    return out


class _Haystack:
    def __init__(self, text: str):
        self.tokens = {_n(t) for t in TOKEN_RE.findall(text)}
        self.stems = {_stem(t) for t in TOKEN_RE.findall(text)}
        self.numbers = set(numbers_in(text))
        self.blob = _key(text)

    def has(self, key: str) -> bool:
        if key.startswith("std:"):
            return key[4:] in self.blob
        if key.startswith("num:"):
            return key in self.numbers
        if key.startswith("np:"):
            return key[3:] in self.stems
        return key in self.tokens or _stem(key) in self.stems


def _master_terms(text: str) -> tuple[set[str], str]:
    toks = {_n(t) for t in TOKEN_RE.findall(text)}
    return toks, _key(text)


def _same(a: str, b: str) -> bool:
    return norm_text(a) == norm_text(b)


def check(tailored: TailoredCV, master: Master, *, extra_allowed_text: str = "") -> list[Flag]:
    flags: list[Flag] = []
    add = lambda level, code, loc, msg, term=None: flags.append(Flag(level=level, code=code, location=loc, message=msg, term=term))  # noqa: E731

    # ---- skills: every item must exist in the master
    known = {_items_key(i) for i in master.skill_items()}
    for gi, g in enumerate(tailored.skill_groups):
        for ii, item in enumerate(g.items):
            if _items_key(item) not in known:
                add("prep", "skill_not_in_master", f"skill_groups[{gi}].items[{ii}]",
                    f"Compétence ajoutée pour l'annonce : « {item} »", item)

    # ---- projects: identity of the project + bullets with valid sources
    mp = {p.id: p for p in master.projects}
    bullets = master.bullet_index()
    for pi, p in enumerate(tailored.projects):
        base = mp.get(p.id)
        loc = f"projects[{pi}]"
        if base is None:
            add("error", "project_unknown", loc, f"Projet inconnu du profil maître : « {p.title} »")
        else:
            for field, label in (("title", "titre"), ("dates", "dates"), ("context", "contexte (école/type)")):
                if not _same(getattr(p, field), getattr(base, field)):
                    add("error", "project_altered", f"{loc}.{field}",
                        f"{label.capitalize()} du projet modifié (« {getattr(p, field)} » ≠ « {getattr(base, field)} »)")
        for bi, b in enumerate(p.bullets):
            bloc = f"{loc}.bullets[{bi}]"
            if not b.source_ids:
                add("error", "bullet_no_source", bloc, "Point sans source dans le profil maître")
                continue
            bad = [s for s in b.source_ids if s not in bullets]
            if bad:
                add("error", "bullet_bad_source", bloc, f"Source(s) inconnue(s) : {', '.join(bad)}")
            elif any(bullets[s][0].id != p.id for s in b.source_ids):
                add("error", "bullet_other_project", bloc, "Point dérivé d'un autre projet que celui où il figure")
            if not bad:
                cited = _Haystack("\n".join(f"{bullets[s][1].label} {bullets[s][1].text}" for s in b.source_ids))
                for key, term in {**claim_terms(b.label), **claim_terms(b.text)}.items():
                    if not cited.has(key):
                        add("prep", "term_not_in_sources", bloc,
                            f"« {term} » ajouté (absent des sources de ce point)", term)

    # ---- education / languages: copied verbatim
    me = {e.id: e for e in master.education}
    for ei, e in enumerate(tailored.education):
        base = me.get(e.id)
        if base is None:
            add("error", "education_unknown", f"education[{ei}]", f"Formation inconnue du profil maître : « {e.title} »")
            continue
        for field in ("title", "school", "dates", "details"):
            if not _same(getattr(e, field), getattr(base, field)):
                add("error", "education_altered", f"education[{ei}].{field}",
                    f"Formation modifiée ({field} : « {getattr(e, field)} » ≠ « {getattr(base, field)} »)")
    ml = {l.id: l for l in master.languages}
    for li, l in enumerate(tailored.languages):
        base = ml.get(l.id)
        if base is None:
            add("error", "language_unknown", f"languages[{li}]", f"Langue inconnue du profil maître : « {l.name} »")
        elif not (_same(l.name, base.name) and _same(l.level, base.level)):
            add("error", "language_altered", f"languages[{li}]", f"Langue/niveau modifié (« {l.name} {l.level} » ≠ « {base.name} {base.level} »)")

    # ---- headline / summary / skill labels: terms must be in the master (the stage qualifier is allowed in the headline)
    hay = _Haystack(master.all_text() + "\n" + extra_allowed_text + "\n" + master.identity.default_title)
    seen: set[tuple[str, str]] = set()

    def scan(loc: str, text: str, code: str, *, proper: bool) -> None:
        for key, term in claim_terms(text, proper=proper).items():
            if (loc, key) in seen or hay.has(key):
                continue
            seen.add((loc, key))
            add("prep", code, loc, f"« {term} » ajouté (absent du profil maître)", term)

    scan("headline", tailored.headline, "headline_term_not_in_master", proper=False)
    scan("summary", tailored.summary, "summary_term_not_in_master", proper=True)
    for gi, g in enumerate(tailored.skill_groups):
        scan(f"skill_groups[{gi}].label", g.label, "label_term_not_in_master", proper=False)

    # ---- language levels anywhere in free text: hard fact, must already be in the master
    master_levels = {_n(m.group(0)) for m in LANG_LEVEL_RE.finditer(master.all_text())}
    free = [("headline", tailored.headline), ("summary", tailored.summary)]
    free += [(f"skill_groups[{gi}]", " ".join([g.label, *g.items])) for gi, g in enumerate(tailored.skill_groups)]
    free += [(f"projects[{pi}].bullets[{bi}]", f"{b.label} {b.text}")
             for pi, p in enumerate(tailored.projects) for bi, b in enumerate(p.bullets)]
    for loc, text in free:
        for m in LANG_LEVEL_RE.finditer(text):
            if _n(m.group(0)) not in master_levels:
                add("error", "language_level_claim", loc, f"Niveau de langue non attesté : « {m.group(0)} »", m.group(0))

    for f in flags:
        if f.level == "prep" and f.term and not f.hint:
            f.hint = prep_hint(f.term)

    if len(tailored.summary) > SUMMARY_MAX_CHARS:
        add("warning", "summary_long", "summary", f"Résumé long ({len(tailored.summary)} caractères) : risque de dépasser 3 lignes")
    return flags


def blocking(flags: list[Flag]) -> list[Flag]:
    return [f for f in flags if f.level == "error"]
