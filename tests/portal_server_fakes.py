"""Builders for the portal server tests (fictional data only). Used by the API / auth / ops tests and meant for the HTML pages
tests too.

Typical use in a test module::

    from portal_server_fakes import Harness, fast_scrypt   # noqa
    @pytest.fixture
    def h(tmp_path, monkeypatch):
        fast_scrypt(monkeypatch)
        return Harness.create(tmp_path)

    device = h.pair()                       # a paired Mac (bearer token headers in device.headers)
    prof = h.enable(device)                 # an enabled profile with a snapshot pushed (prof.handle / prof.passcode / prof.row_id)
    outcome = h.login(prof)                 # real login -> LoginOutcome; h.session(prof) -> auth.Session
    h.clock.advance(3600)                   # fake clock (portal.clock): no sleeping

`h.client` is a Starlette `TestClient` on https://testserver (Secure cookies work); `h.portal` is `app.state.portal`."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from starlette.testclient import TestClient

from jobhunt.portal import protocol
from jobhunt.portal.protocol import (AnswerSchema, AnswerValue, Awaiting, CustomAnswer, DocumentRef, EmailPause, EventResult,
                                     FieldSpec, PasscodeVerifier, PendingQuestion, SentItem, Snapshot, Unconfirmed)
from jobhunt.portal.server import auth
from jobhunt.portal.server.app import Portal, create_app
from jobhunt.portal.server.config import Config

ORIGIN = "https://testserver"
KEY_HEX = "00" * 32
OLD_KEY_HEX = "ff" * 32
PAIRING_CODE = "pairing-code-for-tests-0123456789-abcdefghij"
T0 = 1_800_000_000.0  # the fake clock's start (epoch seconds)
SYNCED_AT = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)


def fast_scrypt(monkeypatch) -> None:
    """Cheap scrypt for tests that are not about the cost (keep at least one test with the real parameters)."""
    monkeypatch.setattr(protocol, "SCRYPT_N", 2 ** 6)


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {"PORTAL_DATA_KEY": KEY_HEX, "PORTAL_PUBLIC_ORIGIN": ORIGIN, "PORTAL_DB_PATH": str(tmp_path / "data" / "portal.sqlite3"),
           "PORTAL_PAIRING_CODE": PAIRING_CODE, "PORTAL_PAIRING_EXPIRES": "2030-01-01T00:00:00+00:00", "PORTAL_VERSION": "abc123"}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


# ---------------------------------------------------------------- documents and snapshot
def make_pdf(label: str = "a") -> bytes:
    return b"%PDF-1.4\n% fictional CV " + label.encode() + b"\n%%EOF\n"


def doc_id_for(pdf: bytes) -> str:
    return hashlib.sha256(b"doc|" + pdf).hexdigest()[:32]


def doc_ref(pdf: bytes) -> DocumentRef:
    return DocumentRef(doc_id=doc_id_for(pdf), sha256=hashlib.sha256(pdf).hexdigest(), size=len(pdf))


PDF_A, PDF_B = make_pdf("a"), make_pdf("b")
DOC_A, DOC_B = doc_id_for(PDF_A), doc_id_for(PDF_B)
PDFS = {DOC_A: PDF_A, DOC_B: PDF_B}


def ver(n: int) -> str:
    return f"{n:032x}"


GRANT_SEND_1, GRANT_SEND_2, GRANT_EMAIL = "a1" * 16, "a2" * 16, "a3" * 16
GRANT_SENT, GRANT_NOT_SENT = "b1" * 16, "b2" * 16
QID = "c1" * 8
CUSTOM_ID, CUSTOM_MASKED = "d1" * 8, "d2" * 8


def make_schema() -> AnswerSchema:
    return AnswerSchema(sections=["Identité", "Situation", "Divers"], fields=[
        FieldSpec(path="identity.phone", label="Téléphone", section="Identité", max_len=30),
        FieldSpec(path="identity.city", label="Ville", section="Identité", max_len=60),
        FieldSpec(path="situation.start", label="Disponible dès le", widget="date", section="Situation"),
        FieldSpec(path="situation.contract", label="Contrat", widget="select", options=["CDI", "Alternance", "Stage"], section="Situation"),
        FieldSpec(path="misc.notes", label="Remarques", widget="textarea", section="Divers", max_len=200),
        FieldSpec(path="misc.health", label="Situation de handicap", section="Divers", write_only=True),
    ])


def make_snapshot(**overrides) -> Snapshot:
    """A valid snapshot of the fictional job seeker Léa Martin: six answer fields (one write-only), two custom answers (one
    masked), one question, one e-mail pause, one unconfirmed send, two waiting applications, one sent, two documents."""
    data = dict(
        display_name="Léa", synced_at=SYNCED_AT, auto_send_on=True, answer_schema=make_schema(),
        answers={
            "identity.phone": AnswerValue(value="06 00 00 00 00", is_set=True, ver=ver(1)),
            "identity.city": AnswerValue(value="Toulouse", is_set=True, ver=ver(2)),
            "situation.start": AnswerValue(value="2027-09-01", is_set=True, ver=ver(3)),
            "situation.contract": AnswerValue(value="Alternance", is_set=True, ver=ver(4)),
            "misc.notes": AnswerValue(value="", is_set=False, ver=ver(5)),
            "misc.health": AnswerValue(value=None, is_set=True, ver=ver(6)),
        },
        custom=[CustomAnswer(custom_id=CUSTOM_ID, question="Prétentions salariales", answer="Selon grille", ver=ver(7)),
                CustomAnswer(custom_id=CUSTOM_MASKED, question="Avez-vous un handicap ?", answer="", masked=True, ver=ver(8))],
        questions=[PendingQuestion(qid=QID, label="Permis de conduire ?", options=["Oui", "Non"], company="Société Exemple", title="Technicien")],
        email_pauses=[EmailPause(grant=GRANT_EMAIL, company="Atelier Martin", title="Assistant", host="ats.example", note="Lien reçu par e-mail")],
        unconfirmed=[Unconfirmed(grant_sent=GRANT_SENT, grant_not_sent=GRANT_NOT_SENT, company="Maison Dupont", title="Stagiaire", host="jobs.example")],
        awaiting=[Awaiting(grant=GRANT_SEND_1, company="Société Exemple", title="Technicien mécatronique", score=81.0, doc_id=DOC_A),
                  Awaiting(grant=GRANT_SEND_2, company="Usine Fictive", title="Alternant automatisme", score=64.5)],
        sent=[SentItem(ref="e1" * 16, company="Bureau Test", title="Ingénieur", host="ats.example", doc_id=DOC_B)],
        documents=[doc_ref(PDF_A), doc_ref(PDF_B)],
    )
    data.update(overrides)
    return Snapshot(**data)


def make_verifier(passcode: str, salt: bytes | None = None) -> PasscodeVerifier:
    salt = salt or bytes(range(16))
    return PasscodeVerifier(salt=salt.hex(), hash=protocol.passcode_hash(passcode, salt).hex())


# ---------------------------------------------------------------- harness
@dataclass
class Device:
    token: str
    token_sha256: str
    device_id: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class Enabled:
    key: str
    handle: str
    passcode: str
    stream_id: str
    row_id: int
    config_rev: int = 1
    epoch: int = 0
    rev: int = 0

    def config_body(self, **over) -> dict:
        body = {"config_rev": self.config_rev, "enabled": True, "handle_sha256": protocol.handle_digest(self.handle),
                "passcode": make_verifier(self.passcode).model_dump(), "sessions_epoch": self.epoch, "stream_id": self.stream_id}
        body.update(over)
        return body


@dataclass
class Harness:
    client: TestClient
    app: object
    portal: Portal
    config: Config
    clock: FakeClock
    counter: int = field(default=0)
    _device: Device | None = None

    @classmethod
    def create(cls, tmp_path: Path, **env: str) -> Harness:
        config = Config.from_env(make_env(tmp_path, **env))
        clock = FakeClock()
        app = create_app(config, clock=clock)
        client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
        return cls(client, app, app.state.portal, config, clock)

    @property
    def api(self) -> str:
        return self.config.base_path + protocol.API_PREFIX

    def pair(self, name: str = "Mac de test") -> Device:
        """Pair THE device of this harness (the pairing code is single-use: later calls return the same device;
        `add_device` makes another one)."""
        if self._device is not None:
            return self._device
        self.counter += 1
        token = protocol.new_token()
        digest = hashlib.sha256(token.encode()).hexdigest()
        r = self.client.post(f"{self.api}/pair", json={"pairing_code": PAIRING_CODE, "device_name": name, "token_sha256": digest})
        assert r.status_code == 200, r.text
        self._device = Device(token, digest, r.json()["device_id"])
        return self._device

    def add_device(self, name: str = "Autre Mac") -> Device:
        """A second paired device inserted directly (the pairing code is single-use)."""
        token = protocol.new_token()
        digest = hashlib.sha256(token.encode()).hexdigest()
        device_id = protocol.new_id()
        with self.portal.db.transaction() as c:
            c.execute("INSERT INTO devices(id, name, token_sha256, created_at) VALUES (?,?,?,?)", (device_id, name, digest, int(self.clock())))
        return Device(token, digest, device_id)

    def new_profile(self, handle: str | None = None, passcode: str = "abcd-efgh-jkmn") -> Enabled:
        self.counter += 1
        return Enabled(key=protocol.new_id(), handle=handle or f"hand{self.counter:06d}", passcode=passcode, stream_id=protocol.new_id(), row_id=0)

    def put_profile(self, device: Device, prof: Enabled, **over):
        return self.client.put(f"{self.api}/profiles/{prof.key}", headers=device.headers, json=prof.config_body(**over))

    def push_snapshot(self, device: Device, prof: Enabled, snapshot: Snapshot | None = None, *, rev: int | None = None,
                      acked_seq: int = 0, results: list[EventResult] | None = None, upload: bool = True):
        prof.rev = rev if rev is not None else prof.rev + 1
        body = {"stream_id": prof.stream_id, "sessions_epoch": prof.epoch, "revoked_through_seq": 0,
                "rev": prof.rev, "acked_seq": acked_seq,
                "results": [r.model_dump(mode="json") for r in results or []],
                "snapshot": (snapshot or make_snapshot()).model_dump(mode="json")}
        r = self.client.put(f"{self.api}/profiles/{prof.key}/snapshot", headers=device.headers, json=body)
        if upload and r.status_code == 200:
            for doc_id in r.json()["missing_documents"]:
                self.upload(device, prof, doc_id, PDFS[doc_id])
        return r

    def upload(self, device: Device, prof: Enabled, doc_id: str, data: bytes):
        return self.client.put(f"{self.api}/profiles/{prof.key}/documents/{doc_id}", content=data,
                               headers={**device.headers, "Content-Type": "application/pdf"})

    def enable(self, device: Device | None = None, snapshot: Snapshot | None = None, with_snapshot: bool = True, **kw) -> Enabled:
        device = device or self.pair()
        prof = self.new_profile(**kw)
        r = self.put_profile(device, prof)
        assert r.status_code == 200, r.text
        prof.row_id = self.portal.db.conn.execute("SELECT id FROM profiles WHERE profile_key=?", (prof.key,)).fetchone()[0]
        if with_snapshot:
            assert self.push_snapshot(device, prof, snapshot).status_code == 200
        return prof

    def login(self, prof: Enabled, addr: str = "198.51.100.10", device_cookie: str | None = None, code: str | None = None,
              presented: str | None = None) -> auth.LoginOutcome:
        return auth.login(self.portal, prof.handle, code or prof.passcode, addr, device_cookie, self.clock(), presented)

    def session(self, prof: Enabled) -> auth.Session:
        out = self.login(prof)
        assert out.kind == "ok"
        session = auth.resolve_session(self.portal, [out.session_id], self.clock())
        assert session is not None
        return session
