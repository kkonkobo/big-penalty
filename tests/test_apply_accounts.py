"""ATS accounts: backends (keyring / local file), index, export / import and CLI. Never touches the real keyring."""

from __future__ import annotations

import json
import os
import re
import string
import sys
import types

import pytest
from typer.testing import CliRunner

from jobhunt.apply import accounts as A
from jobhunt.apply.errors import ApplyError
from jobhunt.cli import app
from jobhunt.config import get_settings


class MemBackend:
    """In-memory backend (one per simulated machine)."""

    name = "mémoire"

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[tuple] = []

    def get(self, service, account):
        self.calls.append(("get", service, account))
        return self.items.get((service, account))

    def set(self, service, account, password, label):
        self.calls.append(("set", service, account, label))
        self.items[(service, account)] = password

    def delete(self, service, account):
        self.items.pop((service, account), None)


@pytest.fixture(autouse=True)
def never_the_real_keyring(monkeypatch, tmp_path):
    """No test can reach the real OS keyring: no module, and the file backend (pytest's tmp data dir) by default."""
    monkeypatch.setattr(A, "_load_keyring", lambda: None)
    monkeypatch.setenv(A.ENV_BACKEND, "file")
    monkeypatch.setattr(A, "_backend_override", None)


@pytest.fixture
def mem(monkeypatch):
    b = MemBackend()
    monkeypatch.setattr(A, "_backend_override", b)
    return b


@pytest.mark.parametrize("host, expected", [
    ("jobs.lever.co", "big-penalty:jobs.lever.co"), ("Careers.Example.COM", "big-penalty:careers.example.com"),
    ("https://boards.greenhouse.io/exemple/jobs/1", "big-penalty:boards.greenhouse.io"), ("example.fr:443", "big-penalty:example.fr"),
    ("emploi.exemple.co.uk", "big-penalty:emploi.exemple.co.uk"), ("exemple.com", "big-penalty:exemple.com"),
    ("company.myworkdayjobs.com", "big-penalty:company.myworkdayjobs.com"), ("127.0.0.1:8080", "big-penalty:127.0.0.1"),
])
def test_service_name_is_scoped_to_the_full_hostname(host, expected):
    assert A.service_name(host) == expected


@pytest.mark.parametrize("host, expected", [
    ("jobs.example.co.uk:443", "example.co.uk"), ("https://careers.Example.com/x", "example.com"), ("example.com", "example.com"),
    ("127.0.0.1", "127.0.0.1"), ("127.0.0.1:8080", "127.0.0.1"), ("http://[::1]:8000/x", "::1"), ("localhost", "localhost"),
    ("localhost:3000", "localhost"), ("10.0.0.12", "10.0.0.12"),
    ("a.github.io", "a.github.io"), ("www.a.github.io", "a.github.io"), ("x.y.netlify.app", "y.netlify.app"),
    ("jobs.company.asso.fr", "company.asso.fr"), ("careers.company.com.au", "company.com.au"), ("github.io", "github.io"),
    ("Company.COM.", "company.com"), ("user@jobs.company.com:99", "company.com"), ("caf\u00e9.fr", "xn--caf-dma.fr"),
    ("bad host.com", ""), ("", ""), ("2001:db8::1", "2001:db8::1"),
])
def test_registrable_host_only_names_the_browser_profile(host, expected):
    assert A.registrable_host(host) == expected


def test_generated_passwords_differ_and_meet_the_policy():
    pws = {A.generate_password() for _ in range(50)}
    assert len(pws) == 50
    assert all(len(p) == A.PASSWORD_LENGTH and any(c in A.SPECIALS for c in p) and any(c.isdigit() for c in p) for p in pws)


def test_create_password_stores_and_reads_back(mem):
    pw = A.create_password("boards.greenhouse.io", "camille@example.test")
    assert len(pw) == A.PASSWORD_LENGTH <= 18 and any(c in string.ascii_uppercase for c in pw) and any(c in string.ascii_lowercase for c in pw)
    assert re.fullmatch(r"[A-Za-z0-9+._-]{16}", pw)
    assert mem.items == {("big-penalty:boards.greenhouse.io", "camille@example.test"): pw}
    assert ("set", "big-penalty:boards.greenhouse.io", "camille@example.test", "Big Penalty \u2014 boards.greenhouse.io") in mem.calls
    assert A.get_password("boards.greenhouse.io", "camille@example.test") == pw
    assert A.get_password("careers.greenhouse.io", "camille@example.test") is None  # another host: another credential


def test_create_password_returns_the_existing_one_and_never_regenerates(mem):
    mem.items[("big-penalty:jobs.lever.co", "a@example.test")] = "existing"
    assert A.create_password("jobs.lever.co", "a@example.test") == "existing"
    assert A.create_password("jobs.lever.co", "a@example.test") == "existing"
    assert not [c for c in mem.calls if c[0] == "set"]
    pw = A.create_password("x.lever.co", "a@example.test")
    assert A.create_password("x.lever.co", "a@example.test") == pw and len([c for c in mem.calls if c[0] == "set"]) == 1


def test_create_password_is_verified_by_reading_it_back(mem):
    mem.set = lambda *a: None  # nothing is actually stored
    with pytest.raises(ApplyError, match="Enregistrement dans le trousseau"):
        A.create_password("lever.co", "a@example.test")
    assert not A._index_path().exists()


@pytest.mark.parametrize("host, email", [
    ("lever.co", 'a"b@example.test'), ("lever.co", "a\\b@example.test"), ("lever.co", "a@example.test\nadd-generic-password -w x"),
    ("lever.co", "a'b@example.test"), ("lever.co", "a$b@example.test"), ("exemple\".co", "a@example.test"), ("lever.co", ""),
])
def test_create_password_rejects_names_that_need_quoting(mem, host, email):
    with pytest.raises(ApplyError, match="Nom de compte"):
        A.create_password(host, email)
    assert mem.calls == [] and not A._index_path().exists()


def test_get_password_rejects_an_invalid_host(mem):
    with pytest.raises(ApplyError, match="Nom de compte"):
        A.get_password("bad host.com", "a@example.test")
    assert mem.calls == []


# ---- file backend ----

def _file_path():
    return get_settings().data_dir / "accounts.json"


def test_file_backend_round_trip_and_layout():
    b = A.FileBackend()
    assert b.get("big-penalty:a.test", "x@example.test") is None
    b.set("big-penalty:a.test", "x@example.test", "pw1", "label")
    b.set("big-penalty:a.test", "y@example.test", "pw2", "label")
    assert b.get("big-penalty:a.test", "x@example.test") == "pw1"
    data = json.loads(_file_path().read_text("utf-8"))
    entry = data["big-penalty:a.test"]["x@example.test"]
    assert entry["password"] == "pw1" and entry["label"] == "label" and entry["updated_at"]
    b.delete("big-penalty:a.test", "x@example.test")
    b.delete("big-penalty:a.test", "unknown@example.test")
    assert b.get("big-penalty:a.test", "x@example.test") is None and b.get("big-penalty:a.test", "y@example.test") == "pw2"
    b.delete("big-penalty:a.test", "y@example.test")
    assert json.loads(_file_path().read_text("utf-8")) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_file_backend_is_mode_0600():
    A.FileBackend().set("big-penalty:a.test", "x@example.test", "pw", "l")
    assert _file_path().stat().st_mode & 0o777 == 0o600


def test_file_backend_write_is_atomic(monkeypatch):
    b = A.FileBackend()
    b.set("big-penalty:a.test", "x@example.test", "old", "l")
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (replaced.append((s, d)), real(s, d))[1])
    b.set("big-penalty:a.test", "x@example.test", "new", "l")
    assert len(replaced) == 1 and replaced[0][1] == _file_path() and str(replaced[0][0]).endswith(".tmp")
    # a failing replace leaves the previous content and no temp file behind
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("nope")))
    with pytest.raises(ApplyError) as e:
        b.set("big-penalty:a.test", "x@example.test", "newer", "l")
    assert "newer" not in str(e.value)
    assert b.get("big-penalty:a.test", "x@example.test") == "new"
    assert [p.name for p in _file_path().parent.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"svc": {"acc": "str"}}'])
def test_corrupt_file_raises_and_is_left_untouched(content):
    _file_path().parent.mkdir(parents=True, exist_ok=True)
    _file_path().write_text(content, "utf-8")
    b = A.FileBackend()
    with pytest.raises(ApplyError):
        b.get("s", "a")
    with pytest.raises(ApplyError):
        b.set("s", "a", "pw", "l")
    with pytest.raises(ApplyError):
        A.create_password("lever.co", "a@example.test")
    assert _file_path().read_text("utf-8") == content


def test_corrupt_index_raises_and_is_left_untouched():
    A._index_path().parent.mkdir(parents=True, exist_ok=True)
    A._index_path().write_text("oops", "utf-8")
    with pytest.raises(ApplyError):
        A.create_password("lever.co", "a@example.test")
    assert A._index_path().read_text("utf-8") == "oops"


# ---- backend choice ----

class FakeKeyring:
    """A usable fake `keyring` module."""

    def __init__(self, fail=None, module="fake.backend"):
        self.items: dict[tuple[str, str], str] = {}
        self.fail = fail
        backend_cls = type("Backend", (), {"priority": 5, "__module__": module})
        self._backend = backend_cls()

    def get_keyring(self):
        return self._backend

    def get_password(self, s, a):
        if self.fail:
            raise self.fail
        return self.items.get((s, a))

    def set_password(self, s, a, p):
        if self.fail:
            raise self.fail
        self.items[(s, a)] = p

    def delete_password(self, s, a):
        if self.fail:
            raise self.fail
        self.items.pop((s, a), None)


def test_auto_picks_keyring_when_usable_else_file(monkeypatch):
    monkeypatch.setenv(A.ENV_BACKEND, "auto")
    assert isinstance(A.get_backend(), A.FileBackend) and A.backend_name() == "fichier local data/accounts.json"
    kr = FakeKeyring()
    monkeypatch.setattr(A, "_load_keyring", lambda: kr)
    assert isinstance(A.get_backend(), A.KeyringBackend) and A.backend_name() == "trousseau du système"
    pw = A.create_password("lever.co", "a@example.test")
    assert kr.items == {("big-penalty:lever.co", "a@example.test"): pw} and not _file_path().exists()
    monkeypatch.delenv(A.ENV_BACKEND)  # default is auto
    assert isinstance(A.get_backend(), A.KeyringBackend)


@pytest.mark.parametrize("kr", [FakeKeyring(module="keyring.backends.fail"), FakeKeyring(module="keyring.backends.null")])
def test_auto_ignores_fail_and_null_keyrings(monkeypatch, kr):
    monkeypatch.setenv(A.ENV_BACKEND, "auto")
    monkeypatch.setattr(A, "_load_keyring", lambda: kr)
    assert isinstance(A.get_backend(), A.FileBackend)


def test_auto_ignores_a_zero_priority_keyring(monkeypatch):
    kr = FakeKeyring()
    kr._backend.priority = 0
    monkeypatch.setenv(A.ENV_BACKEND, "auto")
    monkeypatch.setattr(A, "_load_keyring", lambda: kr)
    assert isinstance(A.get_backend(), A.FileBackend)


def test_explicit_choices(monkeypatch):
    monkeypatch.setenv(A.ENV_BACKEND, "keyring")
    with pytest.raises(ApplyError, match="Trousseau indisponible"):  # forced keyring, none usable: no silent fallback
        A.get_backend()
    monkeypatch.setattr(A, "_load_keyring", lambda: FakeKeyring())
    assert isinstance(A.get_backend(), A.KeyringBackend)
    monkeypatch.setenv(A.ENV_BACKEND, "file")
    assert isinstance(A.get_backend(), A.FileBackend)
    monkeypatch.setenv(A.ENV_BACKEND, "bogus")
    with pytest.raises(ApplyError):
        A.get_backend()


def test_keyring_exception_gives_a_clean_error_without_the_password(monkeypatch):
    class Boom(Exception):
        pass

    kr = FakeKeyring(fail=Boom("secret-pw-123 locked"))
    monkeypatch.setenv(A.ENV_BACKEND, "keyring")
    monkeypatch.setattr(A, "_load_keyring", lambda: kr)
    for call in (lambda: A.get_password("lever.co", "a@example.test"), lambda: A.create_password("lever.co", "a@example.test"),
                 lambda: A.delete_account("lever.co", "a@example.test")):
        with pytest.raises(ApplyError) as e:
            call()
        assert str(e.value) == "Trousseau indisponible : Boom" and e.value.__cause__ is None and "secret" not in str(e.value)
    assert not _file_path().exists()  # no fallback to the file in the middle of a run


def test_keyring_set_failure_never_leaks_the_generated_password(monkeypatch):
    kr = FakeKeyring()
    seen = []

    def bad_set(s, a, p):
        seen.append(p)
        raise RuntimeError(p)

    kr.set_password = bad_set
    monkeypatch.setenv(A.ENV_BACKEND, "keyring")
    monkeypatch.setattr(A, "_load_keyring", lambda: kr)
    with pytest.raises(ApplyError) as e:
        A.create_password("lever.co", "a@example.test")
    assert seen and seen[0] not in str(e.value) and seen[0] not in repr(e.value.__context__)


# ---- index, list, delete ----

def test_index_is_maintained_and_has_no_password(mem):
    pw = A.create_password("jobs.lever.co", "camille@example.test")
    A.create_password("jobs.lever.co", "camille@example.test")  # no duplicate
    A.create_password("boards.greenhouse.io", "camille@example.test")
    raw = A._index_path().read_text("utf-8")
    index = json.loads(raw)
    assert pw not in raw and len(index) == 2
    assert set(index[0]) == {"service", "account", "host", "label", "created_at", "state", "state_at"}
    assert index[0]["service"] == "big-penalty:jobs.lever.co" and index[0]["host"] == "jobs.lever.co"
    rows = A.list_accounts()
    assert [r["stored"] for r in rows] == [True, True]
    del mem.items[("big-penalty:jobs.lever.co", "camille@example.test")]
    assert [r["stored"] for r in A.list_accounts()] == [False, True]


def test_delete_account(mem):
    A.create_password("jobs.lever.co", "a@example.test")
    A.create_password("lever.co", "a@example.test")
    A.delete_account("jobs.lever.co", "a@example.test")
    assert A.get_password("jobs.lever.co", "a@example.test") is None
    assert [r["host"] for r in A.list_accounts()] == ["lever.co"]
    A.delete_account("jobs.lever.co", "a@example.test")  # idempotent


# ---- export / import between two machines ----

@pytest.fixture
def machines(monkeypatch, tmp_path):
    """Two machines = two backends and two data dirs; `use(n)` switches."""
    backends = [MemBackend(), MemBackend()]
    dirs = [tmp_path / "m1", tmp_path / "m2"]

    def use(n):
        monkeypatch.setattr(A, "_backend_override", backends[n])
        monkeypatch.setattr(get_settings(), "data_dir", dirs[n])

    return backends, use


def test_export_import_round_trip_between_two_machines(machines, tmp_path):
    (m1, m2), use = machines, machines[1]
    use(0)
    pw1 = A.create_password("jobs.lever.co", "camille@example.test")
    pw2 = A.create_password("boards.greenhouse.io", "camille@example.test")
    out = tmp_path / "export.json"
    assert A.export_accounts(out) == 2
    data = json.loads(out.read_text("utf-8"))
    assert data["version"] == 1 and data["exported_at"] and len(data["accounts"]) == 2
    assert set(data["accounts"][0]) == {"service", "account", "host", "label", "password", "state"}
    if sys.platform != "win32":
        assert out.stat().st_mode & 0o777 == 0o600
    use(1)
    assert A.import_accounts(out) == (2, 0)
    assert A.get_password("jobs.lever.co", "camille@example.test") == pw1
    assert A.get_password("boards.greenhouse.io", "camille@example.test") == pw2
    assert [r["host"] for r in A.list_accounts()] == ["jobs.lever.co", "boards.greenhouse.io"]
    assert A.import_accounts(out) == (2, 0)  # idempotent, no duplicate index entry
    assert len(A.list_accounts()) == 2


def test_import_skips_a_different_password_unless_overwrite(machines, tmp_path):
    _, use = machines
    use(0)
    pw = A.create_password("jobs.lever.co", "a@example.test")
    out = tmp_path / "export.json"
    A.export_accounts(out)
    use(1)
    other = A.create_password("jobs.lever.co", "a@example.test")
    assert other != pw
    assert A.import_accounts(out) == (0, 1)
    assert A.get_password("jobs.lever.co", "a@example.test") == other
    assert A.import_accounts(out, overwrite=True) == (1, 0)
    assert A.get_password("jobs.lever.co", "a@example.test") == pw


def test_export_refuses_to_overwrite_an_existing_file(mem, tmp_path):
    A.create_password("lever.co", "a@example.test")
    out = tmp_path / "export.json"
    out.write_text("precious", "utf-8")
    with pytest.raises(ApplyError, match="existe"):
        A.export_accounts(out)
    assert out.read_text("utf-8") == "precious"
    assert A.export_accounts(out, overwrite=True) == 1


def _good_entry(**kw):
    e = {"service": "big-penalty:lever.co", "account": "a@example.test", "host": "lever.co", "label": "l", "password": "Pw-1"}
    return {**e, **kw}


@pytest.mark.parametrize("payload", [
    "not json", "[]", json.dumps({"version": 2, "accounts": []}), json.dumps({"version": 1}), json.dumps({"version": 1, "accounts": [1]}),
    json.dumps({"version": 1, "accounts": [_good_entry(password="")]}), json.dumps({"version": 1, "accounts": [_good_entry(password=5)]}),
    json.dumps({"version": 1, "accounts": [_good_entry(account='a"b@example.test')]}),
    json.dumps({"version": 1, "accounts": [_good_entry(host="bad host.com")]}),
    json.dumps({"version": 1, "accounts": [_good_entry(service="big-penalty:other.com")]}),
    json.dumps({"version": 1, "accounts": [_good_entry(), _good_entry(account="a\nb@example.test")]}),  # nothing imported at all
])
def test_import_rejects_malformed_files_without_importing_anything(mem, tmp_path, payload):
    f = tmp_path / "bad.json"
    f.write_text(payload, "utf-8")
    with pytest.raises(ApplyError):
        A.import_accounts(f)
    assert mem.items == {} and not A._index_path().exists()


def test_import_missing_file(mem, tmp_path):
    with pytest.raises(ApplyError, match="introuvable"):
        A.import_accounts(tmp_path / "nope.json")


# ---- CLI ----

def test_cli_accounts_lists_masked_emails_and_the_backend_never_a_password(mem):
    pw = A.create_password("jobs.lever.co", "zoe.martin@example.test")
    r = CliRunner().invoke(app, ["apply", "accounts"])
    assert r.exit_code == 0, r.output
    assert "jobs.lever.co" in r.output and "z\u2026@example.test" in r.output and "zoe.martin" not in r.output and pw not in r.output
    assert "oui" in r.output and "Stockage : mémoire" in r.output


def test_cli_export_import_delete(machines, tmp_path):
    _, use = machines
    runner = CliRunner()
    use(0)
    pw = A.create_password("jobs.lever.co", "camille@example.test")
    out = tmp_path / "x.json"
    r = runner.invoke(app, ["apply", "accounts-export", str(out)])
    assert r.exit_code == 0 and "clair" in r.output and pw not in r.output and "1 compte" in r.output
    r = runner.invoke(app, ["apply", "accounts-export", str(out)])
    assert r.exit_code == 1 and "existe" in r.output
    assert runner.invoke(app, ["apply", "accounts-export", str(out), "--overwrite"]).exit_code == 0
    use(1)
    r = runner.invoke(app, ["apply", "accounts-import", str(out)])
    assert r.exit_code == 0 and "1 compte(s) importé(s)" in r.output and pw not in r.output
    r = runner.invoke(app, ["apply", "accounts-delete", "jobs.lever.co", "camille@example.test"], input="n\n")
    assert r.exit_code != 0 and A.get_password("jobs.lever.co", "camille@example.test") == pw
    r = runner.invoke(app, ["apply", "accounts-delete", "jobs.lever.co", "camille@example.test", "--yes"])
    assert r.exit_code == 0 and A.get_password("jobs.lever.co", "camille@example.test") is None and pw not in r.output
    bad = tmp_path / "bad.json"
    bad.write_text("{", "utf-8")
    r = runner.invoke(app, ["apply", "accounts-import", str(bad)])
    assert r.exit_code == 1 and "Erreur" in r.output


def test_cli_reports_a_keyring_error_without_traceback(monkeypatch):
    monkeypatch.setenv(A.ENV_BACKEND, "keyring")
    r = CliRunner().invoke(app, ["apply", "accounts"])
    assert r.exit_code == 1 and "Trousseau indisponible" in r.output


# ---- account state: pending (password created for a signup) / active (a run got past the account pages) ----

def test_a_new_password_is_pending_and_an_existing_credential_is_active(mem):
    A.create_password("jobs.lever.co", "camille@example.test")
    assert A.account_state("jobs.lever.co", "camille@example.test") == "pending"
    mem.items[("big-penalty:old.example.test", "camille@example.test")] = "Old-Pw-1"  # a credential that predates the index
    A.create_password("old.example.test", "camille@example.test")
    assert A.account_state("old.example.test", "camille@example.test") == "active"
    assert {r["host"]: r["state"] for r in A.list_accounts()} == {"jobs.lever.co": "pending", "old.example.test": "active"}


def test_account_known_needs_a_stored_password_and_the_active_state(mem):
    h, e = "jobs.lever.co", "camille@example.test"
    assert A.account_known(h, e) is False  # nothing stored
    A.create_password(h, e)
    assert A.account_known(h, e) is False  # signup not confirmed yet
    assert A.set_state(h, e, "active") is True
    assert A.account_known(h, e) is True
    A.set_state(h, e, "pending")
    assert A.account_known(h, e) is False
    A.set_state(h, e, "active")
    del mem.items[("big-penalty:jobs.lever.co", e)]
    assert A.account_known(h, e) is False  # active in the index but the password is gone
    assert A.account_known("bad host", e) is False  # errors -> False


@pytest.mark.parametrize(("host", "first", "second"), [
    ("wd3.myworkdaysite.com", "/recruiting/acme/Careers", "/recruiting/other/Careers"),
    ("shared.wd3.myworkdayjobs.com", "/Acme", "/Other"),
])
def test_workday_guard_isolates_same_host_accounts_and_ignores_legacy(mem, host, first, second):
    email = "camille@example.test"
    mem.items[(A.service_name(host), email)] = "synthetic-host-only"
    first_url = f"https://{host}{first}/job/Paris/Engineer_R100"
    second_url = f"https://{host}{second}/job/Paris/Engineer_R200"
    accept_host = {host}.__contains__
    first_password, first_known = A.guarded_account_callbacks(email, accept_host, posting_url=first_url)
    second_password, second_known = A.guarded_account_callbacks(email, accept_host, posting_url=second_url)
    assert not first_known(first_url) and not second_known(second_url)
    assert first_password(second_url) == second_password(first_url) == first_password(host) == ""
    assert not any(call[1] == A.service_name(host) for call in mem.calls)
    first_secret = first_password(first_url)
    assert first_secret and first_secret != "synthetic-host-only"
    assert A.set_state(host, email, "active", site=A.workday_site(first_url, posting=True))
    assert first_known(first_url) and not second_known(second_url)
    assert second_password(second_url) not in {"", first_secret, "synthetic-host-only"}
    assert first_password(first_url) == first_secret


def test_workday_guard_rejects_unproven_scope_without_touching_backend(mem):
    host = "wd3.myworkdaysite.com"
    password, known = A.guarded_account_callbacks("a@example.test", {host}.__contains__,
                                                   posting_url=f"https://{host}/job/Engineer_R100")
    assert not known(host)
    assert password(f"https://{host}/recruiting/acme/Careers/job/Engineer_R100") == ""
    assert not mem.calls


def test_workday_guard_never_falls_back_to_plaintext_storage():
    host = "ag.wd3.myworkdayjobs.com"
    posting = f"https://{host}/en-US/Airbus/job/Paris/Engineer_R100"
    password, known = A.guarded_account_callbacks("a@example.test", {host}.__contains__, posting_url=posting)
    assert not known(posting)
    with pytest.raises(A.ApplyError, match="Trousseau requis"):
        password(posting)
    assert not A.FileBackend().path.exists()


def test_entries_without_a_state_are_active_and_set_state_validates(mem):
    h, e = "jobs.lever.co", "camille@example.test"
    A.create_password(h, e)
    index = json.loads(A._index_path().read_text("utf-8"))
    for entry in index:
        entry.pop("state"), entry.pop("state_at")  # an index written by an older version
    A._index_path().write_text(json.dumps(index), "utf-8")
    assert A.account_state(h, e) == "active" and A.list_accounts()[0]["state"] == "active" and A.account_known(h, e) is True
    with pytest.raises(ApplyError):
        A.set_state(h, e, "weird")
    assert A.set_state("unknown.example.test", e, "active") is False


def test_export_and_import_keep_the_state(mem, monkeypatch, tmp_path):
    A.create_password("jobs.lever.co", "camille@example.test")  # pending
    A.create_password("boards.greenhouse.io", "camille@example.test")
    A.set_state("boards.greenhouse.io", "camille@example.test", "active")
    out = tmp_path / "export.json"
    A.export_accounts(out)
    A._index_path().unlink()
    mem.items.clear()
    assert A.import_accounts(out) == (2, 0)
    assert {r["host"]: r["state"] for r in A.list_accounts()} == {"jobs.lever.co": "pending", "boards.greenhouse.io": "active"}
    old = json.loads(out.read_text("utf-8"))
    for a in old["accounts"]:
        a.pop("state")  # an export from before states existed
    out.write_text(json.dumps(old), "utf-8")
    A._index_path().unlink()
    mem.items.clear()
    A.import_accounts(out)
    assert {r["state"] for r in A.list_accounts()} == {"active"}


def test_accounts_command_shows_the_state(mem):
    A.create_password("jobs.lever.co", "camille@example.test")
    A.create_password("boards.greenhouse.io", "camille@example.test")
    A.set_state("boards.greenhouse.io", "camille@example.test", "active")
    r = CliRunner().invoke(app, ["apply", "accounts"])
    assert r.exit_code == 0 and "état" in r.output and "en attente" in r.output and "actif" in r.output


def test_workday_site_accepts_a_long_posting_slug():
    slug = "STAGE-2027-Commande-Controle-de-platine-motorises-et-des-hexapodes-du-service-de-metrologie--h-f-_JR10445537"
    url = f"https://tenant.wd3.myworkdayjobs.com/en-US/Career/job/Toulouse-Area/{slug}/apply/applyManually"
    assert len(slug) > 64
    assert A.workday_site(url) == "Career"
    assert A.workday_site(url.rsplit("/apply", 1)[0], posting=True) == "Career"
    assert A.workday_site("https://tenant.wd3.myworkdayjobs.com/en-US/" + "x" * 65 + "/job/a/b_1") is None
