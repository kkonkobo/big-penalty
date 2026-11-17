from __future__ import annotations

import pytest
import socket

from jobhunt import db
from jobhunt.config import get_settings
from jobhunt.models import ContractType, Profile


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH_OVERRIDE", tmp_path / "test.sqlite3")
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", None)
    monkeypatch.setattr(s, "openai_api_key", None)
    monkeypatch.setattr(s, "firecrawl_api_key", None)
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")

    def offline_connect(*args, **kwargs):
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
