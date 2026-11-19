"""Doubles for the portal sync client tests: an in-memory FAKE SERVER implementing DESIGN §4 behind httpx.MockTransport, and
helpers to build applications, runs and a paired / enabled setup (fictional data only)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from jobhunt.apply import store as apply_store
from jobhunt.apply.types import FilledField, Question, RunResult, StepLog
from jobhunt.cv import service as cvs
from jobhunt.portal import manage, protocol, secrets
from jobhunt.portal.client import PortalClient
from jobhunt.portal.journal import Journal

from apply_cv_helpers import make_app, make_job
from cv_fakes import save_confirmed

BASE = "http://127.0.0.1:8099/app"
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
PAIRING_CODE = "pairing-code-for-tests-0123456789abcdef"
_EVENT = TypeAdapter(protocol.Event)


@dataclass
class FakeProfile:
    device_id: str
    config_rev: int = 0
    config_digest: str = ""
    enabled: bool = True
    handle_sha: str = ""
    passcode: dict | None = None
    epoch: int = 0
    stream_id: str = ""
    events: list[dict] = field(default_factory=list)
    last_seq: int = 0
    revoked_through_seq: int = 0
    acked: int = 0
    snapshot_rev: int = 0
    snapshot_digest: str = ""
    snapshot: protocol.Snapshot | None = None
    results: list[protocol.EventResult] = field(default_factory=list)
    docs: dict[str, bytes] = field(default_factory=dict)
    held: bool = False


class FakePortalServer:
    def __init__(self, pairing_code: str = PAIRING_CODE):
        self.pairing_code = pairing_code
        self.devices: dict[str, str] = {}  # token sha -> device id
        self.profiles: dict[str, FakeProfile] = {}
        self.offline = False
        self.requests: list[tuple[str, str]] = []
        self.snapshot_pushes = 0
        self.doc_uploads: list[str] = []
        self.override: list = []  # callables (request) -> Response | None, consumed once each
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------ test helpers
    def client(self, token: str | None = None) -> PortalClient:
        return PortalClient(BASE, token, transport=self.transport)

    def add_event(self, key: str, kind: str, **fields) -> str:
        p = self.profiles[key]
        p.last_seq += 1
        ev = {"seq": p.last_seq, "event_id": protocol.new_id(), "at": NOW.isoformat(), "kind": kind, **fields}
        _EVENT.validate_python(ev)
        p.events.append(ev)
        return ev["event_id"]

    def edit_answer(self, key: str, path: str, value: str, after: str | None = None, base_ver: str | None = None) -> str:
        ver = base_ver or self.profiles[key].snapshot.answers[path].ver
        return self.add_event(key, "answer", path=path, value=value, base_ver=ver, after=after)

    def only_key(self) -> str:
        return next(iter(self.profiles))

    # ------------------------------------------------------------ HTTP
    def _json(self, status: int, body) -> httpx.Response:
        data = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
        return httpx.Response(status, json=data)

    def _err(self, status: int, code: str, current: int | None = None) -> httpx.Response:
        return self._json(status, {"error": code, **({"current": current} if current is not None else {})})

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        self.requests.append((request.method, request.url.path))
        for fn in list(self.override):
            r = fn(request)
            if r is not None:
                self.override.remove(fn)
                return r
        prefix = "/app" + protocol.API_PREFIX
        path = request.url.path
        if not path.startswith(prefix):
            return self._err(404, "not_found")
        path = path[len(prefix):]
        if path == "/pair" and request.method == "POST":
            return self._pair(request)
        auth = request.headers.get("authorization", "")
        device = self.devices.get(protocol.sha256_hex(auth[7:])) if auth.startswith("Bearer ") else None
        if device is None:
            return self._err(401, "unauthorized")
        if path == "/status":
            return self._json(200, protocol.ServerStatus(protocol=protocol.PROTOCOL_VERSION, version="fake", profiles=[
                protocol.ProfileStatus(profile_key=k, enabled=p.enabled, held=p.held, config_rev=p.config_rev, snapshot_rev=p.snapshot_rev,
                                       last_seq=p.last_seq, acked_seq=p.acked, sessions=0)
                for k, p in self.profiles.items() if p.device_id == device]))
        m = re.fullmatch(r"/profiles/([0-9a-f]{32})(/inbox|/snapshot|/documents/([0-9a-f]{32}))?", path)
        if not m:
            return self._err(404, "not_found")
        key, sub, doc = m.group(1), m.group(2), m.group(3)
        prof = self.profiles.get(key)
        if prof is not None and prof.device_id != device:
            return self._err(404, "not_found")
        if sub is None and request.method == "PUT":
            return self._put_profile(request, key, device)
        if prof is None:
            return self._err(404, "not_found")
        if sub is None and request.method == "DELETE":
            del self.profiles[key]
            return self._json(200, {})
        if sub == "/inbox" and request.method == "GET":
            after = int(request.url.params.get("after", "0"))
            evs = [e for e in prof.events if e["seq"] > after][:protocol.INBOX_PAGE]
            return httpx.Response(200, json={"stream_id": prof.stream_id, "last_seq": prof.last_seq, "acked_seq": prof.acked,
                                             "sessions_epoch": prof.epoch, "revoked_through_seq": prof.revoked_through_seq,
                                             "config_rev": prof.config_rev, "enabled": prof.enabled, "held": prof.held, "events": evs})
        if sub == "/snapshot" and request.method == "PUT":
            return self._put_snapshot(request, prof)
        if doc and request.method == "PUT":
            return self._put_doc(request, prof, doc)
        return self._err(404, "not_found")

    def _pair(self, request) -> httpx.Response:
        try:
            req = protocol.PairRequest.model_validate_json(request.content)
        except ValidationError:
            return self._err(422, "invalid")
        if req.pairing_code != self.pairing_code:
            return self._err(401, "unauthorized")
        if req.token_sha256 in self.devices:
            return self._json(200, protocol.PairResponse(device_id=self.devices[req.token_sha256]))
        if self.devices:
            return self._err(401, "unauthorized")  # max devices reached
        self.devices[req.token_sha256] = protocol.new_id()
        return self._json(200, protocol.PairResponse(device_id=self.devices[req.token_sha256]))

    def _put_profile(self, request, key: str, device: str) -> httpx.Response:
        if len(request.content) > protocol.MAX_JSON_BODY:
            return self._err(413, "too_large")
        try:
            cfg = protocol.ProfileConfig.model_validate_json(request.content)
        except ValidationError:
            return self._err(422, "invalid")
        prof = self.profiles.get(key)
        digest = protocol.body_digest(cfg)
        if prof is not None:
            if cfg.config_rev < prof.config_rev or (cfg.config_rev == prof.config_rev and digest != prof.config_digest):
                return self._err(409, "config_rev", prof.config_rev)
            if cfg.config_rev == prof.config_rev:
                return self._json(200, protocol.ProfileConfigResponse(config_rev=prof.config_rev))
        elif cfg.passcode is None:
            return self._err(422, "invalid")
        if any(k != key and q.handle_sha == cfg.handle_sha256 for k, q in self.profiles.items()):
            return self._err(409, "handle_taken")
        if prof is None:
            prof = self.profiles[key] = FakeProfile(device_id=device, stream_id=cfg.stream_id)
        if cfg.stream_id != prof.stream_id:  # a new stream: pending events dropped, seq restarts, open results become lost
            prof.stream_id, prof.events, prof.last_seq, prof.acked = cfg.stream_id, [], 0, 0
            prof.revoked_through_seq = 0
            prof.results = [r if r.status in protocol.FINAL_STATUSES else r.model_copy(update={"status": "lost"}) for r in prof.results]
        elif cfg.sessions_epoch != prof.epoch:
            prof.revoked_through_seq = prof.last_seq
        prof.config_rev, prof.config_digest, prof.enabled = cfg.config_rev, digest, cfg.enabled
        prof.handle_sha, prof.epoch = cfg.handle_sha256, cfg.sessions_epoch
        if cfg.passcode is not None:
            prof.passcode = cfg.passcode.model_dump()
        return self._json(200, protocol.ProfileConfigResponse(config_rev=prof.config_rev))

    def _put_snapshot(self, request, prof: FakeProfile) -> httpx.Response:
        if len(request.content) > protocol.MAX_SNAPSHOT_BODY:
            return self._err(413, "too_large")
        try:
            push = protocol.SnapshotPush.model_validate_json(request.content)
        except ValidationError:
            return self._err(422, "invalid")
        if push.stream_id != prof.stream_id:
            return self._err(409, "stream", prof.config_rev)
        if (push.sessions_epoch, push.revoked_through_seq) != (prof.epoch, prof.revoked_through_seq):
            return self._err(409, "controls")
        digest = protocol.body_digest(push)
        if push.rev < prof.snapshot_rev or (push.rev == prof.snapshot_rev and digest != prof.snapshot_digest):
            return self._err(409, "rev", prof.snapshot_rev)
        if push.acked_seq < max(prof.acked, prof.revoked_through_seq) or push.acked_seq > prof.last_seq:
            return self._err(409, "ack", prof.acked)
        self.snapshot_pushes += 1
        prof.snapshot_rev, prof.snapshot_digest, prof.acked = push.rev, digest, push.acked_seq
        prof.events = [e for e in prof.events if e["seq"] > push.acked_seq]
        prof.snapshot, prof.results = push.snapshot, push.results
        wanted = {d.doc_id for d in push.snapshot.documents}
        prof.docs = {k: v for k, v in prof.docs.items() if k in wanted}
        missing = [d for d in wanted if d not in prof.docs]
        return self._json(200, protocol.SnapshotPushResponse(rev=push.rev, missing_documents=sorted(missing)))

    def _put_doc(self, request, prof: FakeProfile, doc_id: str) -> httpx.Response:
        ref = next((d for d in (prof.snapshot.documents if prof.snapshot else []) if d.doc_id == doc_id), None)
        body = request.content
        if ref is None or request.headers.get("content-type") != "application/pdf":
            return self._err(422, "invalid")
        if len(body) != ref.size or not body.startswith(b"%PDF-"):
            return self._err(422, "invalid")
        if hashlib.sha256(body).hexdigest() != ref.sha256:
            return self._err(422, "invalid")
        prof.docs[doc_id] = body
        self.doc_uploads.append(doc_id)
        return self._json(200, {})


# ------------------------------------------------------------ local data builders
def new_app(profile, master, tmp_path, n=1, *, approve=False, score=88.0, url=None, age_days=1, flags=None, with_pdf=True):
    """Job + draft application (optionally approved) with a fake one-page PDF; no browser, no LLM."""
    job = make_job(profile, title=f"Stage numéro {n}", company=f"Société {n}", score=score, age_days=age_days,
                   url=url or f"https://careers.exemple.test/jobs/{n}")
    (tmp_path / f"a{n}").mkdir(exist_ok=True)
    app = make_app(profile, job, tmp_path / f"a{n}", master_hash=master.content_hash, flags=flags, with_pdf=with_pdf)
    if with_pdf:
        app = set_pdf(app, f"%PDF-1.4 fake one page, application {n}".encode())  # distinct bytes: distinct document ids
    return job, (cvs.approve(app.id, app.current_rev) if approve else app)


def set_pdf(app, data: bytes):
    """Replace the revision's PDF on disk AND its recorded hash (so the application stays sendable)."""
    import hashlib
    from pathlib import Path

    from jobhunt.cv import store as cvstore
    from jobhunt.db import connect

    Path(app.cv_path).write_bytes(data)
    with connect() as c:
        c.execute("UPDATE application_revisions SET pdf_sha256=? WHERE application_id=? AND rev=?",
                  (hashlib.sha256(data).hexdigest(), app.id, app.current_rev))
    return cvstore.get_application(app.id)


def make_run(app, tmp_path, *, mode="submit", status="needs_input", questions=(), message="Message du run", filled=()):
    out = tmp_path / f"runs-{app.id}-{mode}-{status}"
    out.mkdir(parents=True, exist_ok=True)
    steps = [StepLog(index=0, url="https://careers.exemple.test/apply", page_kind="form", filled=list(filled))]
    run = apply_store.start_run(app.id, app.current_rev, mode, "user", lambda rid: out)
    return apply_store.finish_run(run.id, RunResult(status=status, message=message, steps=steps, questions=list(questions)))


def make_outstanding(app, job):
    """An attempt that reached the final click and was not confirmed: the application is `failed` with an outstanding run."""
    from jobhunt.apply import policy
    from jobhunt.dedupe import canonical_url

    run = apply_store.start_run(app.id, app.current_rev, "submit", "user", lambda rid: app_dir(rid))
    apply_store.reserve_send(run.id, profile_id=app.profile_id, application_id=app.id, trigger="user",
                             role_key=policy.normalize_role(job.company, job.title), url_key=canonical_url(job.origin_url), daily_cap=10)
    return apply_store.finish_and_settle(run.id, RunResult(status="failed", message="Délai dépassé", submit_clicked=True))


def app_dir(rid):
    from jobhunt.config import get_settings

    d = get_settings().applications_dir / "portal-test-runs" / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------ fixtures (imported by the test modules)
@pytest.fixture
def master(profile):
    return save_confirmed(profile.id)


@dataclass
class Portal:
    journal: Journal
    fake: FakePortalServer
    client: PortalClient
    profile_id: int
    key: str
    credentials: manage.Credentials


@pytest.fixture
def server():
    return FakePortalServer()


@pytest.fixture
def journal():
    return Journal()


@pytest.fixture
def paired(journal, server):
    manage.pair(BASE, PAIRING_CODE, "Mac de test", "file", journal=journal, client=server.client(None))
    return server.client(secrets.load_token("http://127.0.0.1:8099", "file"))


@pytest.fixture
def portal(profile, master, journal, server, paired):
    creds = manage.enable(profile.id, journal=journal, client=paired)
    return Portal(journal, server, paired, profile.id, journal.get_profile(profile.id).profile_key, creds)


def make_sent(app, job, *, inferred=(), submitted_at=None):
    """A really submitted application whose sending run is recorded (`applications.sent_run_id`)."""
    from jobhunt.apply import policy
    from jobhunt.dedupe import canonical_url

    run = apply_store.start_run(app.id, app.current_rev, "submit", "user", lambda rid: app_dir(rid))
    apply_store.reserve_send(run.id, profile_id=app.profile_id, application_id=app.id, trigger="user",
                             role_key=policy.normalize_role(job.company, job.title), url_key=canonical_url(job.origin_url), daily_cap=10)
    steps = [StepLog(index=1, url="https://careers.exemple.test/apply", page_kind="form", filled=list(inferred))]
    res = apply_store.finish_and_settle(run.id, RunResult(status="submitted", message="ok", steps=steps, submit_clicked=True),
                                        sent_origin_url=canonical_url(job.origin_url))
    if submitted_at is not None:
        from jobhunt.db import _iso, connect

        with connect() as c:
            c.execute("UPDATE applications SET submitted_at=? WHERE id=?", (_iso(submitted_at), app.id))
    return res
