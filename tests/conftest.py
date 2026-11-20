from __future__ import annotations

import pytest
import socket
from threading import Lock

from jobhunt import db
from jobhunt.config import Settings, get_settings
from jobhunt.models import ContractType, Profile

_REAL_CONNECT = socket.socket.connect
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(params=[False, True], ids=["process-scoped", "per-handle"])
def lock_backend(request):
    """Model a file lock that allows or rejects a second handle in this process."""
    mutex, handles = Lock(), set()

    def try_lock(handle, shared=False):
        with mutex:
            if request.param and handles:
                return False
            handles.add(handle)
            return True

    def unlock(handle):
        with mutex:
            handles.remove(handle)

    return try_lock, unlock


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from jobhunt import schedule
    from jobhunt.portal import periodic

    plist_path = schedule.plist_path
    monkeypatch.setattr(schedule, "plist_path", lambda home=None: plist_path(home or tmp_path))

    def unexpected_daily_launch(*args, **kwargs):
        raise AssertionError("Tests must use a fake daily launcher")

    monkeypatch.setattr(schedule, "_launch_daily", unexpected_daily_launch)
    monkeypatch.setattr(periodic, "run_pass", lambda: None)
    monkeypatch.setattr(db, "DB_PATH_OVERRIDE", tmp_path / "test.sqlite3")
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", None)
    monkeypatch.setattr(s, "openai_api_key", None)
    monkeypatch.setattr(s, "firecrawl_api_key", None)
    monkeypatch.setattr(s, "linkedin_detail_cmd", "off")  # never spawn the paid provider from tests
    monkeypatch.setattr(s, "llm_cli_enabled", False)  # never spawn the developer's signed-in Codex / Claude Code CLIs
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")
    # The developer's .env may carry personal auto-apply limits (age, thresholds, caps, switches): tests always start
    # from the documented defaults and change what they need themselves.
    for name, value in (("apply_auto", False), ("apply_daily", False), ("apply_auto_min_score", 60), ("apply_daily_cap", 10),
                        ("apply_prepare_min_score", 60), ("apply_prepare_daily_cap", 15), ("apply_max_age_days", 14)):
        if hasattr(s, name):
            monkeypatch.setattr(s, name, value)
    for name, field in Settings.model_fields.items():  # the owner's JOBHUNT_APPLY_* tuning (big passes) never leaks into tests
        if name.startswith("apply_"):
            monkeypatch.setattr(s, name, field.default)
    monkeypatch.setattr(s, "apply_autonomous", False)

    def offline_connect(sock, address, *args, **kwargs):
        # Loopback stays allowed: on Windows, asyncio builds its event-loop wake-up socket pair by connecting
        # over 127.0.0.1, and the desktop tests talk to a local server. Anything else is real network access.
        if isinstance(address, tuple) and address and address[0] in _LOOPBACK:
            return _REAL_CONNECT(sock, address, *args, **kwargs)
        raise AssertionError("Tests must use offline fixtures or mock transports")

    monkeypatch.setattr(socket.socket, "connect", offline_connect)
    yield


@pytest.fixture(autouse=True)
def web_guard_for_tests(monkeypatch):
    """The test client's Host is "testserver" and it posts without Origin: relax the guard here only (dedicated tests in
    test_web_guard.py turn the strict rules back on)."""
    from jobhunt.web import app as webapp

    monkeypatch.setattr(webapp, "EXTRA_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(webapp, "REQUIRE_ORIGIN_EVIDENCE", False)


@pytest.fixture
def profile() -> Profile:
    return db.save_profile(Profile(
        name="Léa", headline="Étudiante 2e année ingénieur mécatronique",
        interests=["systèmes embarqués", "programmation bas niveau"], skills=["C", "STM32"],
        contract_types=[ContractType.STAGE, ContractType.ALTERNANCE], locations=["Toulouse"], experience_years=0,
    ))
