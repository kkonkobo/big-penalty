"""apply/letter.py: cover letter only on demand, rule check + verifier (fail closed), one letter per revision."""

from __future__ import annotations

import pytest

from jobhunt import db
from jobhunt.apply import letter
from jobhunt.cv import service as cvs
from jobhunt.cv import store
from jobhunt.cv.render import FitNotes
from jobhunt.cv.tailored import TailoredCV
from jobhunt.llm import LLMError

from apply_cv_helpers import OK_VERIFIER, make_app, make_job
from cv_fakes import fake_llm, save_confirmed

REAL_RENDER = letter._render_pdf
PARAS = [
    "Étudiante en cycle ingénieur en robotique, votre offre de stage m'intéresse car elle associe capteurs, actionneurs et firmware.",
    "J'ai réalisé un robot suiveur de ligne avec cinq capteurs infrarouges et une régulation PWM : ces projets académiques m'ont "
    "appris à intégrer matériel et logiciel en C.",
    "Disponible pour un stage, je serais heureuse d'échanger avec vous sur vos besoins.",
]


def draft(paras=None):
    return lambda call: letter.LetterDraft(paragraphs=list(paras or PARAS))


def verdict(supported=True, claims=()):
    return lambda call: letter.LetterVerdict(supported=supported, claims=list(claims))


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    def fake_render(doc, out):
        assert "Objet : candidature" in doc
        out.write_bytes(b"%PDF-1.4 fake letter")
    monkeypatch.setattr(letter, "_render_pdf", fake_render)


@pytest.fixture
def setup(profile, tmp_path):
    master = save_confirmed(profile.id)
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash)
    return master, job, app


async def test_letter_is_written_beside_the_revision_pdf_and_recorded(setup):
    master, job, app = setup
    llm, prov = fake_llm({"LetterDraft": draft(), "LetterVerdict": verdict()})
    res = await letter.ensure_letter(app, job, master, llm)
    rev_dir = cvs.safe_pdf(app).parent
    assert res is not None and res.pdf_path == (rev_dir / "lettre.pdf").resolve() and (rev_dir / "lettre.txt").is_file()
    assert res.text.startswith(f"Objet : candidature — {job.title}\n\nMadame, Monsieur,\n\n")
    assert res.text.rstrip().endswith("salutations distinguées.\n\nCamille Durand") and "Je vous prie d'agréer" in res.text
    assert store.get_application(app.id).cover_letter_path == str(rev_dir / "lettre.pdf")
    assert [c["cls"] for c in prov.calls] == ["LetterDraft", "LetterVerdict"]
    assert "Camille Durand" in prov.calls[0]["user"] and job.title in prov.calls[0]["user"]


async def test_letter_is_reused_for_the_same_revision(setup):
    master, job, app = setup
    llm, prov = fake_llm({"LetterDraft": draft(), "LetterVerdict": verdict()})
    first = await letter.ensure_letter(app, job, master, llm)
    again = await letter.ensure_letter(store.get_application(app.id), job, master, llm)
    assert again is not None and again.text == first.text and again.pdf_path == first.pdf_path and len(prov.calls) == 2


async def test_a_new_revision_never_reuses_an_old_letter(setup, profile):
    master, job, app = setup
    llm, prov = fake_llm({"LetterDraft": draft(), "LetterVerdict": verdict()})
    first = await letter.ensure_letter(app, job, master, llm)
    tmp = cvs.safe_pdf(app).with_name("new.tmp")
    tmp.write_bytes(b"%PDF-1.4 rev two")
    store.add_revision(profile_id=profile.id, job_id=job.id, job_title=job.title, company=job.company, ats=None,
                       tailored=TailoredCV(headline="h", summary="s2"), flags=[], verifier=OK_VERIFIER, fit_notes=FitNotes(),
                       master_hash=master.content_hash, tmp_pdf=tmp, app_id=app.id, base_rev=1,
                       pdf_target=lambda rev: cvs.revision_pdf_path(master, job.company, job.id, rev))
    app2 = store.get_application(app.id)
    assert app2.current_rev == 2 and app2.cover_letter_path == str(first.pdf_path)  # stale pointer is ignored
    second = await letter.ensure_letter(app2, job, master, llm)
    assert second is not None and second.pdf_path != first.pdf_path and "rev-2" in str(second.pdf_path) and len(prov.calls) == 4


@pytest.mark.parametrize("bad", [
    verdict(False, ["a travaillé chez Exemple SAS"]), verdict(True, ["école inventée"]), verdict(False)])
async def test_verifier_rejection_returns_none(setup, bad):
    master, job, app = setup
    llm, _ = fake_llm({"LetterDraft": draft(), "LetterVerdict": bad})
    assert await letter.ensure_letter(app, job, master, llm) is None
    rev_dir = cvs.safe_pdf(app).parent
    assert not (rev_dir / "lettre.pdf").exists() and not (rev_dir / "lettre.txt").exists()
    assert store.get_application(app.id).cover_letter_path is None


async def test_llm_errors_fail_closed(setup):
    master, job, app = setup

    def boom(call):
        raise LLMError("openai: timeout")
    for outputs in ({"LetterDraft": boom}, {"LetterDraft": draft(), "LetterVerdict": boom}):
        llm, _ = fake_llm(outputs)
        assert await letter.ensure_letter(app, job, master, llm) is None


@pytest.mark.parametrize("paras,why", [
    ([*PARAS[:2], "Je maîtrise l'anglais au niveau C2 et j'ai 3 ans d'expérience."], "level+number"),
    ([*PARAS[:2], "Mon projet a réduit le temps de réponse de 40 %."], "number"),
    (PARAS[:2], "too few paragraphs"),
    ([*PARAS, "mot " * 230], "too long"),
])
async def test_rule_check_rejects_before_calling_the_verifier(setup, paras, why):
    master, job, app = setup
    llm, prov = fake_llm({"LetterDraft": draft(paras), "LetterVerdict": verdict()})
    assert await letter.ensure_letter(app, job, master, llm) is None, why
    assert [c["cls"] for c in prov.calls] == ["LetterDraft"]


async def test_language_level_present_in_the_master_is_allowed(setup):
    master, job, app = setup
    paras = [*PARAS[:2], "Mon niveau B2 en anglais me permet de travailler avec vos équipes internationales."]
    llm, _ = fake_llm({"LetterDraft": draft(paras), "LetterVerdict": verdict()})
    assert await letter.ensure_letter(app, job, master, llm) is not None


async def test_no_pdf_no_letter(profile, tmp_path):
    master = save_confirmed(profile.id)
    job = make_job(profile)
    app = make_app(profile, job, tmp_path, master_hash=master.content_hash, with_pdf=False)
    llm, prov = fake_llm({"LetterDraft": draft(), "LetterVerdict": verdict()})
    assert await letter.ensure_letter(app, job, master, llm) is None and not prov.calls


def test_letter_html_is_escaped_and_complete(setup):
    master, job, _ = setup
    html = letter.letter_html(master, job, ["Bonjour <script>x</script> & merci"])
    assert "<script>" not in html and "&lt;script&gt;" in html and "&amp;" in html
    assert "camille@example.test" in html and "Objet : candidature — " in html


def test_real_pdf_rendering_is_one_page(setup, tmp_path):
    # the real renderer (the autouse fixture stubs `letter._render_pdf`); skipped when no browser is installed
    master, job, _ = setup
    out = tmp_path / "x" / "lettre.pdf"
    try:
        REAL_RENDER(letter.letter_html(master, job, PARAS), out)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no browser: {type(e).__name__}")
    import pymupdf
    with pymupdf.open(out) as doc:
        assert len(doc) == 1 and "salutations distinguées" in doc[0].get_text()
