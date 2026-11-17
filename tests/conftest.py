from __future__ import annotations

import pytest
import socket

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.models import ContractType, Profile

_REAL_CONNECT = socket.socket.connect
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH_OVERRIDE", tmp_path / "test.sqlite3")
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", None)
    monkeypatch.setattr(s, "openai_api_key", None)
    monkeypatch.setattr(s, "firecrawl_api_key", None)
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")

    def offline_connect(sock, address, *args, **kwargs):
        # Loopback stays allowed: on Windows, asyncio builds its event-loop wake-up socket pair by connecting
        # over 127.0.0.1, and the desktop tests talk to a local server. Anything else is real network access.
        if isinstance(address, tuple) and address and address[0] in _LOOPBACK:
            return _REAL_CONNECT(sock, address, *args, **kwargs)
        raise AssertionError("Tests must use offline fixtures or mock transports")

    monkeypatch.setattr(socket.socket, "connect", offline_connect)
    yield


@pytest.fixture
def profile() -> Profile:
    return db.save_profile(Profile(
        name="Léa", headline="Étudiante 2e année ingénieur mécatronique",
        interests=["systèmes embarqués", "programmation bas niveau"], skills=["C", "STM32"],
        contract_types=[ContractType.STAGE, ContractType.ALTERNANCE], locations=["Toulouse"], experience_years=0,
    ))
