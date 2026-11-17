"""Fake sources + fake LLM shared by pipeline/web tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

from jobhunt.models import ContractType, JobPosting, SearchQuery
from jobhunt.scoring import JobScore
from jobhunt.sources.base import LoginRequired, Source, SourceContext, SourceError


def posting(source, sid, title="Stage ingénieur systèmes embarqués H/F", company="Airbus SAS", loc="Toulouse (31)",
            ct=ContractType.STAGE, **kw):
    d = dict(source=source, source_id=sid, url=f"https://{source}.test/jobs/{sid}", title=title, company=company,
             location=loc, contract_type=ct, description=f"Description {source} " * 5,
             posted_at=datetime.now(UTC) - timedelta(days=2))
    d.update(kw)
    return JobPosting(**d)


class BoardA(Source):
    name: ClassVar[str] = "boarda"
    label: ClassVar[str] = "Board A"
    kind: ClassVar[str] = "http"
    is_aggregator: ClassVar[bool] = True
    calls: ClassVar[list] = []

    async def search(self, query: SearchQuery, ctx: SourceContext):
        BoardA.calls.append(query.keywords)
        return [posting("boarda", "a1"), posting("boarda", "a2", title="Senior firmware lead", ct=ContractType.CDI)]


class BoardB(Source):
    name: ClassVar[str] = "boardb"
    label: ClassVar[str] = "Board B"
    kind: ClassVar[str] = "http"

    async def search(self, query, ctx):
        return [posting("boardb", "b1", title="Stage Ingénieur Systèmes Embarqués (F/H)", company="AIRBUS"),
                posting("boardb", "b2", title="Stage robotique", company="Thales")]


class Broken(Source):
    name: ClassVar[str] = "broken"
    label: ClassVar[str] = "Broken"
    kind: ClassVar[str] = "http"

    async def search(self, query, ctx):
        raise SourceError("boom")


class NeedsLogin(Source):
    name: ClassVar[str] = "needslogin"
    label: ClassVar[str] = "Needs login"
    kind: ClassVar[str] = "browser"
    requires_login: ClassVar[bool] = True

    async def search(self, query, ctx):
        raise LoginRequired("please log in")


class CdiOnly(Source):
    name: ClassVar[str] = "cdionly"
    label: ClassVar[str] = "CDI only"
    kind: ClassVar[str] = "http"
    contract_types: ClassVar = frozenset({ContractType.CDI})

    async def search(self, query, ctx):
        raise AssertionError("must be skipped: contract types cannot match")


class NeedsKey(Source):
    name: ClassVar[str] = "needskey"
    label: ClassVar[str] = "Needs key"
    kind: ClassVar[str] = "api"
    requires_credentials: ClassVar = ("france_travail_client_id",)

    async def search(self, query, ctx):
        raise AssertionError("not configured")


ALL = {c.name: c for c in (BoardA, BoardB, Broken, NeedsLogin, CdiOnly, NeedsKey)}


def patch_registry(monkeypatch, which=None):
    async def offline_origins(postings, ctx):
        return postings

    monkeypatch.setattr("jobhunt.origin.resolve_origins", offline_origins)
    reg = {k: v for k, v in ALL.items() if which is None or k in which}
    monkeypatch.setattr("jobhunt.pipeline.registry", lambda: reg)
    monkeypatch.setattr("jobhunt.web.app.registry", lambda: reg)
    BoardA.calls.clear()
    return reg
