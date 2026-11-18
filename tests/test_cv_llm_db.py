"""Structured-call deadline / sanitized errors / failover setting, and the migration lock + upgrade of a live v2 database."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.cv.verify import VerifyResult, build_items, verify
from jobhunt.llm import FailoverLLM, LLMError

from cv_fakes import FakeProvider, good_draft, sample_master


class Slow(FakeProvider):
    async def structured(self, model_cls, **kw):
        await asyncio.sleep(30)


async def test_structured_has_one_overall_deadline(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_deadline_s", 0.1)
    slow1, slow2 = Slow(), Slow()
    slow2.name = "second"
    with pytest.raises(LLMError) as e:
        await FailoverLLM(providers=[slow1, slow2]).structured(VerifyResult, system_text="s", user_text="u")
    assert e.value.code == "deadline" and "Délai dépassé" in str(e.value)


async def test_structured_errors_are_short_codes_never_model_output():
    bad = FakeProvider(error="quote of the CV: SECRET-CV-TEXT")
    llm = FailoverLLM(providers=[bad])
    with pytest.raises(LLMError) as e:
        await llm.structured(VerifyResult, system_text="s", user_text="u")
    assert "SECRET" not in str(e.value) and "SECRET" not in " ".join(llm.errors) and e.value.code == "unavailable"
    assert str(e.value) == "Appel IA impossible (fake: error)"


async def test_provider_messages_do_not_embed_validation_errors():
    import anthropic
    from jobhunt.llm import AnthropicProvider

    class Client:
        class beta:
            class messages:
                @staticmethod
                async def parse(**kw):
                    raise ValueError("input_value='SECRET-CV-TEXT'")

    p = AnthropicProvider(client=Client(), max_attempts=1)
    with pytest.raises(LLMError) as e:
        await p.structured(VerifyResult, system_text="s", user_text="u")
    assert "SECRET" not in str(e.value) and e.value.code == "invalid_response"


def test_settings_defaults_and_env(monkeypatch):
    s = get_settings()
    assert s.cv_allow_failover is True and s.llm_deadline_s == 180


async def test_failover_allowed_by_default_and_prefer_orders_providers(monkeypatch):
    a, o = FakeProvider({"VerifyResult": VerifyResult(items=[])}), FakeProvider({"VerifyResult": VerifyResult(items=[])})
    a.name, o.name = "anthropic", "openai"
    llm = FailoverLLM(providers=[a, o])
    info, _ = await llm.structured_ex(VerifyResult, system_text="s", user_text="u", prefer="openai")
    assert info["provider"] == "openai" and a.calls == []
    o.error = "down"
    info, _ = await llm.structured_ex(VerifyResult, system_text="s", user_text="u", prefer="openai")
    assert info["provider"] == "anthropic"  # explicit failover
    monkeypatch.setattr(get_settings(), "cv_allow_failover", False)
    o.error = None
    info, _ = await llm.structured_ex(VerifyResult, system_text="s", user_text="u", prefer="openai")
    assert info["provider"] == "anthropic"  # the FIRST configured provider only, `prefer` is ignored
    a.error, before = "down", len(o.calls)
    with pytest.raises(LLMError):
        await llm.structured_ex(VerifyResult, system_text="s", user_text="u", prefer="openai")
    assert len(o.calls) == before  # no failover: the data never reached openai


def test_verifier_items_use_cited_sources_and_full_master_for_summary():
    from jobhunt.cv.tailor import build_tailored
    from jobhunt.dedupe import MergedJob
    from jobhunt.models import ContractType
    m = sample_master()
    cv = build_tailored(good_draft(m), m, MergedJob(title="x", contract_type=ContractType.STAGE))
    cv.projects[0].bullets[1].source_ids = []
    items, preset = build_items(cv, m)
    assert [p.ref for p in preset] == ["projects[0].bullets[1]"] and not preset[0].supported
    by = {i["ref"]: i for i in items}
    assert by["projects[0].bullets[0]"]["sources"] == ["Acquisition : lecture de cinq capteurs infrarouges et filtrage des mesures."]
    assert "Station météo connectée" in by["summary"]["sources"][0]


async def test_verify_with_no_available_provider_does_not_raise():
    m = sample_master()
    from jobhunt.cv.tailor import build_tailored
    from jobhunt.dedupe import MergedJob
    from jobhunt.models import ContractType
    cv = build_tailored(good_draft(m), m, MergedJob(title="x", contract_type=ContractType.STAGE))
    rep = await verify(cv, m, FailoverLLM(providers=[]))
    assert rep.ran is False and rep.error == "unavailable" and rep.flags()[0].code == "verifier_not_run"


# ------------------------------------------------------------------ migrations
def make_v2_db(path):
    conn = sqlite3.connect(path)
    for stmt in db._statements("".join(db.MIGRATIONS[:2])):
        conn.execute(stmt)
    conn.execute("PRAGMA user_version=2")
    conn.execute("INSERT INTO profiles(id, name, data, created_at, updated_at) VALUES (1,'p','{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
    cols = "(profile_id, job_id, job_title, company, status, tailored_json, flags_json, cv_path, created_at, updated_at)"
    rows = [(1, 5, "T", "C", "draft", '{"headline":"h","summary":"s"}', '[{"level":"error","code":"c","location":"l","message":"m"}]', "/old/cv.pdf"),
            (1, 5, "T", "C", "approved", '{"headline":"h2","summary":"s"}', "[]", "/old/cv2.pdf"),  # duplicate active row
            (1, 6, "T6", "C", "withdrawn", '{"headline":"h3","summary":"s"}', "[]", None)]
    for r in rows:
        conn.execute(f"INSERT INTO applications{cols} VALUES (?,?,?,?,?,?,?,?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')", r)
    conn.commit()
    conn.close()


def test_live_v2_database_upgrades_without_data_loss(tmp_path, monkeypatch):
    path = tmp_path / "live.sqlite3"
    make_v2_db(path)
    monkeypatch.setattr(db, "DB_PATH_OVERRIDE", path)
    from jobhunt.cv import store
    apps = store.list_applications()
    assert [a.id for a in apps] == [3, 2, 1]
    a1, a2 = store.get_application(1), store.get_application(2)
    assert a1.current_rev == 1 and a1.tailored.headline == "h" and a1.cv_path == "/old/cv.pdf" and len(a1.errors) == 1
    assert a1.pdf_sha256 is None and a1.verifier is None and a1.approved_rev is None  # never eligible: must be regenerated
    assert {a1.status, a2.status} == {"withdrawn", "approved"}  # duplicate active rows: older one withdrawn
    assert store.get_application(3).status == "withdrawn"
    with db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert c.execute("SELECT COUNT(*) FROM application_revisions").fetchone()[0] == 3
        with pytest.raises(sqlite3.IntegrityError):  # at most one active application per (profile, job)
            c.execute("INSERT INTO applications(profile_id, job_id, status, created_at, updated_at) VALUES (1,6,'draft','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
            c.execute("INSERT INTO applications(profile_id, job_id, status, created_at, updated_at) VALUES (1,6,'draft','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")


def test_concurrent_startups_migrate_once(tmp_path, monkeypatch):
    path = tmp_path / "race.sqlite3"
    make_v2_db(path)
    monkeypatch.setattr(db, "DB_PATH_OVERRIDE", path)
    errors = []

    def start():
        try:
            conn = sqlite3.connect(path, timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            db._migrate(conn)
            conn.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("SELECT COUNT(*) FROM application_revisions").fetchone()[0] == 3  # backfilled once, not 6 times


def test_migration_failure_rolls_back_schema_and_version(tmp_path, monkeypatch):
    path = tmp_path / "bad.sqlite3"
    make_v2_db(path)
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:3] + ["CREATE TABLE ok_t(x);\nINSERT INTO nope VALUES (1);"])
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.OperationalError):
        db._migrate(conn)
    conn.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2  # migration 3 rolled back with the broken 4th
    assert conn.execute("SELECT name FROM sqlite_master WHERE name IN ('application_revisions','ok_t')").fetchall() == []
