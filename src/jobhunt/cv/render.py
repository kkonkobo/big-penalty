"""Render a tailored CV to HTML (preview) and to a ONE-page A4 PDF (headless Chromium via Playwright/patchright)."""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path

from pydantic import BaseModel, Field
from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobhunt.config import get_settings
from jobhunt.cv.master import Master
from jobhunt.cv.tailored import TailoredCV

TEMPLATES = Path(__file__).resolve().parent.parent / "web" / "templates"
env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)

FIT_PREFIX = "Mise en page : "
MIN_SCALE = 0.92
SCALE_STEP = 0.02
MAX_FIT_ITERATIONS = 24
A4_HEIGHT_PX = 297 / 25.4 * 96  # CSS px


def photo_data_uri(path: Path | None) -> str | None:
    if path is None or not Path(path).is_file():
        return None
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()


def render_html(tailored: TailoredCV, master: Master, photo: Path | str | None = None) -> str:
    """Full HTML document of the CV. `photo` = file path or a ready data URI."""
    uri = photo if isinstance(photo, str) else photo_data_uri(photo)
    return env.get_template("cv/cv.html").render(cv=tailored, identity=master.identity, photo=uri)


def safe_filename(*parts: str, ext: str = ".pdf") -> str:
    name = " - ".join(p.strip() for p in parts if p and p.strip())
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:150].strip() or "CV") + ext


class FitNotes(BaseModel):
    """What page fitting removed. Stored per revision, SEPARATE from the (untrimmed) tailored content, and carried over
    when the CV is edited. Bullet indexes refer to the untrimmed content."""
    font_scale: float = 1.0
    dropped_bullets: list[tuple[str, int]] = Field(default_factory=list)  # (project id, bullet index)
    dropped_projects: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return self.font_scale >= 1.0 and not self.dropped_bullets and not self.dropped_projects

    def bullet_dropped(self, project_id: str, index: int) -> bool:
        return project_id in self.dropped_projects or (project_id, index) in {tuple(x) for x in self.dropped_bullets}


def apply_fit(tailored: TailoredCV, notes: FitNotes) -> TailoredCV:
    """The content that is actually on the PDF: a trimmed COPY of the untrimmed tailored CV."""
    cv = tailored.model_copy(deep=True)
    drops = {tuple(x) for x in notes.dropped_bullets}
    cv.projects = [p for p in cv.projects if p.id not in notes.dropped_projects]
    for p in cv.projects:
        p.bullets = [b for i, b in enumerate(p.bullets) if (p.id, i) not in drops]
    cv.font_scale = notes.font_scale
    cv.adjustments = [a for a in cv.adjustments if not a.startswith(FIT_PREFIX)] + fit_messages(tailored, notes)
    return cv


def fit_messages(tailored: TailoredCV, notes: FitNotes) -> list[str]:
    out = []
    if notes.font_scale < 1.0:
        out.append(f"{FIT_PREFIX}taille de police réduite à {round(notes.font_scale * 100)} % pour tenir sur une page")
    titles = {p.id: p.title for p in tailored.projects}
    for pid, i in notes.dropped_bullets:
        if pid in titles and i < len(next(p for p in tailored.projects if p.id == pid).bullets):
            b = next(p for p in tailored.projects if p.id == pid).bullets[i]
            out.append(f"{FIT_PREFIX}point retiré (« {b.label or b.text[:40]} », {titles[pid]})")
    for pid in notes.dropped_projects:
        out.append(f"{FIT_PREFIX}projet retiré (« {titles.get(pid, pid)} »)")
    return out


def _next_trim(tailored: TailoredCV, notes: FitNotes) -> bool:
    """Record ONE more reduction in `notes`: font first (down to MIN_SCALE), then the last remaining bullet of the least
    relevant project, then the least relevant project."""
    if notes.font_scale - SCALE_STEP >= MIN_SCALE - 1e-9:
        notes.font_scale = round(notes.font_scale - SCALE_STEP, 2)
        return True
    live = [p for p in tailored.projects if p.id not in notes.dropped_projects]
    for p in reversed(live):
        alive = [i for i in range(len(p.bullets)) if not notes.bullet_dropped(p.id, i)]
        if len(alive) > 1:
            notes.dropped_bullets.append((p.id, alive[-1]))
            return True
    if len(live) > 1:
        notes.dropped_projects.append(live[-1].id)
        return True
    return False


def _launch(pw):
    try:
        return pw.chromium.launch(headless=True)
    except Exception:  # bundled Chromium missing -> installed Chrome
        return pw.chromium.launch(headless=True, channel="chrome")


def _page_count(pdf: Path) -> int:
    import pymupdf

    with pymupdf.open(pdf) as doc:
        return len(doc)


class FitResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    notes: FitNotes
    final: TailoredCV  # exactly what the PDF contains (fact-check THIS)
    pages: int
    fits: bool  # exactly one page verified on the PDF; `out_path` is written only then


def fit_and_render(tailored: TailoredCV, master: Master, out_path: Path, *, photo: Path | None = None,
                   notes: FitNotes | None = None) -> FitResult:
    """Fit the (untrimmed) `tailored` CV on ONE A4 page: persisted trims first, then the font (bounded), then more trims.
    `tailored` is NOT modified. The PDF is written to `out_path` only if the browser's own pagination says exactly one page."""
    from patchright.sync_api import sync_playwright

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    notes = (notes or FitNotes()).model_copy(deep=True)
    notes.font_scale = 1.0  # the font is recomputed; drops persist
    uri = photo_data_uri(photo)
    tmp = out.with_name("part-" + out.name)
    pages = 0
    try:
        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page(viewport={"width": 794, "height": 1123})
                for _ in range(MAX_FIT_ITERATIONS):
                    page.set_content(render_html(apply_fit(tailored, notes), master, uri), wait_until="load")
                    height = page.evaluate("document.getElementById('cv-page').getBoundingClientRect().height")
                    if height <= A4_HEIGHT_PX - 2 or not _next_trim(tailored, notes):
                        break
                for _ in range(MAX_FIT_ITERATIONS):  # the browser's own pagination is the final judge
                    final = apply_fit(tailored, notes)
                    page.set_content(render_html(final, master, uri), wait_until="load")
                    page.pdf(path=str(tmp), format="A4", print_background=True, prefer_css_page_size=True,
                             margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
                    pages = _page_count(tmp)
                    if pages <= 1 or not _next_trim(tailored, notes):
                        break
            finally:
                browser.close()
        final = apply_fit(tailored, notes)
        fits = pages == 1
        if fits:
            tmp.replace(out)
        return FitResult(notes=notes, final=final, pages=pages, fits=fits)
    finally:
        tmp.unlink(missing_ok=True)
