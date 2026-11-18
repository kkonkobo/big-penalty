"""Import a CV (PDF or text) into the master profile: text + photo extraction, then ONE LLM call to structure it."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from jobhunt.cv.master import (PENDING_PHOTO, Bullet, Education, Identity, Language, Master, MasterError, Project, SkillGroup,
                               load_master, profile_dir, save_pending)
from jobhunt.dedupe import strip_accents
from jobhunt.models import Profile


# ----------------------------------------------------------------- LLM schema (no ids: they are assigned in code)
class _Skill(BaseModel):
    label: str
    items: list[str] = Field(description="Each item copied verbatim from the CV, one entry per comma/bullet separated item")


class _Bullet(BaseModel):
    label: str = Field(description='Bold prefix before the colon, without the colon ("" if none)')
    text: str = Field(description="Text after the label, verbatim")


class _Project(BaseModel):
    title: str
    dates: str = ""
    context: str = Field(default="", description="The italic line under the title (type of project, school...)")
    bullets: list[_Bullet] = Field(default_factory=list)


class _Education(BaseModel):
    title: str
    school: str = ""
    dates: str = ""
    details: str = ""


class _Language(BaseModel):
    name: str
    level: str = ""


class ImportedMaster(BaseModel):
    name: str
    default_title: str = Field(description='Job/status title of the headline WITHOUT the job-search wording, e.g. "Élève ingénieur"')
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    summary: str = Field(description="The italic introduction paragraph, verbatim")
    skill_groups: list[_Skill] = Field(default_factory=list)
    projects: list[_Project] = Field(default_factory=list)
    education: list[_Education] = Field(default_factory=list)
    languages: list[_Language] = Field(default_factory=list)


IMPORT_SYSTEM = """You convert the raw text of a French CV into a structured master profile.
STRICT RULES:
- Copy the candidate's wording VERBATIM (same words, accents, punctuation, capitalisation). Never paraphrase, translate,
  summarise, correct, reorder facts, or add anything that is not in the text: no new skills, tools, dates, schools,
  employers or metrics.
- The header line gives the name and a headline such as "<title> - Recherche d'alternance en <domain>": put only the
  title part (without the job-search wording) in default_title.
- skill_groups: one group per skill line ("Label : item, item, ..."); split the items on commas, keep each item verbatim
  (do not split inside parentheses; leave out the final full stop of the line).
- projects: title (without the dates), dates (as written, e.g. 2025–2026), context (the italic line below the title), and bullets;
  each bullet = label (bold prefix before the colon) + text (what follows, verbatim, same capitalisation).
- education: title, school, dates and details as written. languages: name and level as written (e.g. "Courant.").
- email / phone / linkedin only if present in the text. Keep the original order of everything."""


def extract_pdf(pdf: Path, out_dir: Path, photo_name: str = "photo.jpg") -> tuple[str, Path | None]:
    """Text of the PDF (all pages) and the largest embedded image of page 1 (the photo) saved as out_dir/<photo_name>."""
    import pymupdf

    out_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf) as doc:
        text = "\n".join(page.get_text("text", sort=True) for page in doc)
        photo: Path | None = None
        if len(doc):
            page = doc[0]
            infos = [i for i in page.get_image_info(xrefs=True) if i.get("xref")]
            if infos:
                best = max(infos, key=lambda i: (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]))
                pix = pymupdf.Pixmap(doc, best["xref"])
                if pix.alpha or pix.colorspace is None or pix.colorspace.n != 3:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                while pix.width > 700:  # keep the PDF light: the photo is shown ~25 mm wide
                    pix.shrink(1)
                for old in out_dir.glob(Path(photo_name).stem + ".*"):
                    old.unlink()
                photo = out_dir / photo_name
                pix.save(photo, jpg_quality=90)
    return text, photo


def _to_master(draft: ImportedMaster, profile: Profile | None) -> Master:
    ident = Identity(name=draft.name.strip(), default_title=draft.default_title.strip(),
                     email=draft.email or None, phone=draft.phone or None, linkedin=draft.linkedin or None)
    if profile:
        ident.email = ident.email or profile.email
        ident.phone = ident.phone or profile.phone
    return Master(
        identity=ident, summary=draft.summary.strip(),
        skill_groups=[SkillGroup(label=g.label.strip(), items=[i.strip().rstrip(".").strip() for i in g.items if i.strip().rstrip(".").strip()])
                      for g in draft.skill_groups],
        projects=[Project(title=p.title.strip(), dates=p.dates.strip(), context=p.context.strip(),
                          bullets=[Bullet(label=b.label.strip(), text=b.text.strip()) for b in p.bullets])
                  for p in draft.projects],
        education=[Education(title=e.title.strip(), school=e.school.strip(), dates=e.dates.strip(), details=e.details.strip())
                   for e in draft.education],
        languages=[Language(name=l.name.strip(), level=l.level.strip()) for l in draft.languages],
    ).ensure_ids()


def _key(s: str) -> str:
    return re.sub(r"[\W_]+", "", strip_accents(s).lower())


def verify_import(master: Master, source_text: str) -> list[str]:
    """Strings of the master that are NOT found verbatim in the source text (ignoring spaces/punctuation/case)."""
    hay = _key(source_text)
    missing: list[str] = []

    def chk(where: str, value: str | None) -> None:
        if value and _key(value) and _key(value) not in hay:
            missing.append(f"{where}: {value[:80]}")

    chk("résumé", master.summary)
    for g in master.skill_groups:
        chk(f"compétences/{g.id}", g.label)
        for i in g.items:
            chk(f"compétences/{g.id}", i)
    for p in master.projects:
        chk(f"{p.id} titre", p.title)
        chk(f"{p.id} dates", p.dates)
        chk(f"{p.id} contexte", p.context)
        for b in p.bullets:
            chk(f"{b.id} libellé", b.label)
            chk(f"{b.id} texte", b.text)
    for e in master.education:
        for f in ("title", "school", "dates", "details"):
            chk(f"{e.id} {f}", getattr(e, f))
    for l in master.languages:
        chk(f"{l.id} langue", l.name)
        chk(f"{l.id} niveau", l.level)
    return missing


def _digits(s: str | None) -> str:
    return re.sub(r"\D+", "", s or "")


def _norm_url(s: str) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", s.strip().lower()).rstrip("/")


def identity_disputes(master: Master, source_text: str, profile: Profile | None) -> list[dict[str, str]]:
    """Identity fields (name, email, phone, linkedin) that are not in the source text, or that disagree with the Profile.
    Each: {"field", "value", "reason"}."""
    ident, out = master.identity, []
    hay, hay_digits, hay_key = source_text.lower(), _digits(source_text), _key(source_text)

    def dispute(field: str, value: str, reason: str) -> None:
        out.append({"field": f"identity.{field}", "value": value, "reason": reason})

    if ident.name and _key(ident.name) not in hay_key:
        dispute("name", ident.name, "absent du CV source")
    if ident.email and ident.email.strip().lower() not in hay:
        dispute("email", ident.email, "absent du CV source")
    if ident.phone and (not _digits(ident.phone) or _digits(ident.phone) not in hay_digits):
        dispute("phone", ident.phone, "absent du CV source")
    if ident.linkedin and _norm_url(ident.linkedin) not in re.sub(r"https?://(www\.)?", "", hay):
        dispute("linkedin", ident.linkedin, "absent du CV source")
    if profile:
        if profile.email and ident.email and profile.email.strip().lower() != ident.email.strip().lower():
            dispute("email", ident.email, "différent de l'e-mail du profil")
        if profile.phone and ident.phone and _digits(profile.phone) != _digits(ident.phone):
            dispute("phone", ident.phone, "différent du téléphone du profil")
        pk, nk = _key(profile.name or ""), _key(ident.name or "")
        if pk and nk and pk not in nk and nk not in pk:
            dispute("name", ident.name, "différent du nom du profil")
    return out


async def import_text(text: str, profile: Profile | None, llm) -> Master:
    draft = await llm.structured(ImportedMaster, system_text=IMPORT_SYSTEM, user_text="CV text:\n\n" + text[:30000],
                                 max_tokens=8000)
    return _to_master(draft, profile)


class ImportResult(BaseModel):
    master: Master
    unverified: list[str] = Field(default_factory=list)  # strings not found verbatim in the source
    disputed: list[dict[str, str]] = Field(default_factory=list)  # identity fields not in the source / differing from the Profile
    path: Path  # master.pending.yaml


async def import_cv(profile_id: int, profile: Profile | None, llm, *, pdf: Path | None = None,
                    text_file: Path | None = None) -> ImportResult:
    """Extract (+photo), structure with one LLM call, STAGE as master.pending.yaml + import report. The active master is
    untouched until the user confirms the pending one (UI)."""
    if (pdf is None) == (text_file is None):
        raise ValueError("fournir exactement --pdf ou --text")
    out_dir = profile_dir(profile_id)
    photo = None
    if pdf is not None:
        text, photo = extract_pdf(pdf, out_dir, PENDING_PHOTO)
    else:
        text = text_file.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("aucun texte extrait du CV")
    master = await import_text(text, profile, llm)
    try:  # re-import: keep the per-candidate setting the user may have edited by hand
        previous = load_master(profile_id)
    except MasterError:
        previous = None
    if previous and previous.identity.stage_qualifier:
        master.identity.stage_qualifier = previous.identity.stage_qualifier
    if photo is not None:
        master.identity.photo = "photo.jpg"  # becomes photo.jpg when the import is confirmed
    elif (existing := next(iter(sorted(out_dir.glob("photo.*"))), None)) is not None:
        master.identity.photo = existing.name
    unverified = verify_import(master, text)
    disputed = identity_disputes(master, text, profile)
    report = {"unmatched": unverified, "disputed": disputed, "source": pdf.name if pdf else "texte"}
    path = save_pending(profile_id, master, report)
    return ImportResult(master=master, unverified=unverified, disputed=disputed, path=path)
