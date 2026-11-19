"""The ASGI app: routing under the base path, Host allow-list, headers, database setup, import isolation, a real server."""

from __future__ import annotations

import ast
import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import threading
import urllib.request
from pathlib import Path

import pytest
from portal_server_fakes import Harness, fast_scrypt, make_env
from fastapi import Request
from starlette.testclient import TestClient

from jobhunt.portal.server import app as appmod
from jobhunt.portal.server import pages
from jobhunt.portal.server import db as dbmod
from jobhunt.portal.server.app import SECURITY_HEADERS, create_app
from jobhunt.portal.server.config import Config

SERVER_DIR = Path(appmod.__file__).parent


@pytest.fixture
def h(tmp_path, monkeypatch):
    fast_scrypt(monkeypatch)
    harness = Harness.create(tmp_path)
    yield harness
    harness.portal.db.close()


def test_health_and_version(h):
    r = h.client.get("/app/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    v = h.client.get("/app/version.json")
    assert v.json() == {"sha": "abc123"}
    for k, val in SECURITY_HEADERS.items():
        assert r.headers[k] == val
    assert r.headers["cache-control"] == "no-store"
    assert h.client.get("/openapi.json").status_code == 404 and h.client.get("/docs").status_code == 404 and h.client.get("/redoc").status_code == 404


def test_base_path_redirect_and_outside_paths(h):
    r = h.client.get("/app", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/app/"
    for path in ("/", "/application", "/APP/healthz", "/appx/healthz", "/app/../etc", "/static/x", "/app/nothing"):
        r = h.client.get(path, follow_redirects=False)
        assert r.status_code == 404, path
        for k, val in SECURITY_HEADERS.items():
            assert r.headers[k] == val
    assert "<html" in h.client.get("/app/nothing").text and h.client.get("/app/nothing").headers["content-type"].startswith("text/html")


def test_custom_base_path(tmp_path):
    harness = Harness.create(tmp_path, PORTAL_BASE_PATH="/portail")
    assert harness.client.get("/portail/healthz").json() == {"ok": True}
    assert harness.client.get("/app/healthz").status_code == 404
    assert harness.client.get("/portail", follow_redirects=False).headers["location"] == "/portail/"
    dev = harness.pair()
    assert harness.client.get("/portail/api/v3/status", headers=dev.headers).status_code == 200
    assert harness.client.get("/portail/api/v2/status", headers=dev.headers).status_code == 404
    assert harness.client.get("/portail/api/v1/status", headers=dev.headers).status_code == 404
    harness.portal.db.close()


def test_host_allow_list(h):
    for host in ("evil.example", "testserver:8443", "", "testserver.evil.example"):
        r = h.client.get("/app/version.json", headers={"Host": host})
        assert r.status_code == 421, host
        for k, val in SECURITY_HEADERS.items():
            assert r.headers[k] == val
    assert h.client.get("/app/healthz", headers={"Host": "localhost:8080"}).status_code == 200  # container health check
    assert h.client.post("/app/healthz", headers={"Host": "localhost:8080"}).status_code == 421
    assert h.client.get("/app/version.json", headers={"Host": "TESTSERVER"}).status_code == 200


def test_default_port_host_is_accepted(tmp_path):
    c = Config.from_env(make_env(tmp_path, PORTAL_PUBLIC_ORIGIN="https://portal.example:443"))
    assert c.allowed_hosts == {"portal.example:443", "portal.example"}


def test_body_deadline_is_total_not_per_chunk(h, monkeypatch):
    monkeypatch.setattr(appmod, "BODY_DEADLINE_SECONDS", 0.02)
    messages = []

    async def downstream(scope, receive, send):
        assert (await receive())["more_body"]
        assert (await receive())["type"] == "http.disconnect"

    async def exercise():
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"{}", "more_body": True}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        middleware = appmod.PortalASGI(downstream, h.portal)
        await middleware({"type": "http", "method": "POST", "path": f"{h.api}/pair",
                          "headers": [(b"host", b"testserver")]}, receive, send)

    asyncio.run(exercise())
    assert messages[0]["status"] == 408


def test_startup_hold_kills_sessions_and_known_devices(h):
    from dataclasses import replace

    profile = h.enable()
    db = h.portal.db
    assert h.login(profile).kind == "ok"
    for table in ("sessions", "known_devices"):
        assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    create_app(replace(h.config, hold_all_on_start=True), db=db, clock=h.clock)
    assert db.conn.execute("SELECT held FROM profiles").fetchone()[0] == 1
    for table in ("sessions", "known_devices"):
        assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_static_paths_keep_their_own_cache_control_and_no_page_headers_overridden(h):
    @h.app.get("/app/static/x.css")
    async def css():
        from fastapi.responses import Response
        return Response("a{}", media_type="text/css")

    @h.app.get("/app/pdf")
    async def pdf():
        from fastapi.responses import Response
        return Response(b"%PDF-", media_type="application/pdf", headers={"Content-Security-Policy": "sandbox", "Cache-Control": "private, no-store"})

    static = h.client.get("/app/static/x.css")
    assert "cache-control" not in static.headers and static.headers["x-content-type-options"] == "nosniff"
    doc = h.client.get("/app/pdf")
    assert doc.headers["content-security-policy"] == "sandbox" and doc.headers["cache-control"] == "private, no-store"
    assert doc.headers["x-frame-options"] == "DENY"


def test_pages_hook_gets_the_portal(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(pages, "install", lambda app, portal: seen.setdefault("portal", portal))

    @pages.router.get("/hello")
    async def hello(request: Request):
        return {"base": pages.portal_of(request).config.base_path}

    try:
        app = create_app(Config.from_env(make_env(tmp_path)))
        assert TestClient(app, base_url="https://testserver").get("/app/hello").json() == {"base": "/app"}
        assert seen["portal"] is app.state.portal
    finally:
        pages.router.routes[:] = [r for r in pages.router.routes if getattr(r, "path", "") != "/hello"]


def test_purge_runs_at_startup_and_at_most_hourly(tmp_path, monkeypatch):
    calls = []
    real = dbmod.purge_expired
    monkeypatch.setattr(dbmod, "purge_expired", lambda db, now: (calls.append(now), real(db, now))[1])
    harness = Harness.create(tmp_path)
    assert len(calls) == 1
    harness.client.get("/app/healthz")
    harness.client.get("/app/healthz")
    assert len(calls) == 1
    harness.clock.advance(3601)
    harness.client.get("/app/healthz")
    harness.client.get("/app/healthz")
    assert len(calls) == 2
    harness.portal.db.close()


# ---------------------------------------------------------------- database
def test_database_setup(tmp_path):
    path = tmp_path / "nested" / "dir" / "p.sqlite3"
    db = dbmod.Database(path)
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    c = db.conn
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 2 and c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(dbmod.MIGRATIONS)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"devices", "pairings", "profiles", "snapshots", "events", "documents", "sessions", "known_devices", "throttle"} <= tables
    again = dbmod.Database(path)  # reopening migrates nothing and keeps data
    assert again.conn.execute("PRAGMA user_version").fetchone()[0] == len(dbmod.MIGRATIONS)
    db.close()
    again.close()


def test_transaction_rolls_back_and_connections_are_per_thread(tmp_path):
    db = dbmod.Database(tmp_path / "t.sqlite3")
    with pytest.raises(RuntimeError), db.transaction() as c:
        c.execute("INSERT INTO throttle VALUES ('x', 1, 1)")
        raise RuntimeError
    assert db.conn.execute("SELECT COUNT(*) FROM throttle").fetchone()[0] == 0
    conns = []
    t = threading.Thread(target=lambda: conns.append(db.conn))
    t.start()
    t.join()
    assert conns[0] is not db.conn
    db.close()


def test_newer_schema_refused(tmp_path):
    path = tmp_path / "n.sqlite3"
    dbmod.Database(path).close()
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(RuntimeError):
        dbmod.Database(path)


# ---------------------------------------------------------------- import isolation
ISOLATION = textwrap.dedent("""
    import importlib.abc, sys
    BLOCKED = {"yaml", "httpx", "patchright", "anthropic", "openai", "fitz", "keyring", "typer"}
    ALLOWED = {"jobhunt", "jobhunt.portal", "jobhunt.portal.protocol"}

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            top = name.split(".")[0]
            if top in BLOCKED:
                raise ImportError("blocked: " + name)
            if top == "jobhunt" and name not in ALLOWED and not (name == "jobhunt.portal.server" or name.startswith("jobhunt.portal.server.")):
                raise ImportError("blocked: " + name)
            return None

    sys.meta_path.insert(0, Finder())
    import jobhunt.portal.server.app, jobhunt.portal.server.admin, jobhunt.portal.server.pages, jobhunt.portal.server.__main__
    leaked = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED or (m.startswith("jobhunt.") and not m.startswith("jobhunt.portal")))
    assert not leaked, leaked
    assert "uvicorn" not in sys.modules
    print("isolated")
""")


def test_server_imports_in_isolation():
    result = subprocess.run([sys.executable, "-c", ISOLATION], capture_output=True, text=True, timeout=60, cwd=str(SERVER_DIR))
    assert result.returncode == 0 and "isolated" in result.stdout, result.stderr


def test_server_sources_import_only_allowed_packages():
    allowed = {"fastapi", "starlette", "jinja2", "pydantic", "cryptography", "uvicorn", "jobhunt"}
    for path in SERVER_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                top = name.split(".")[0]
                assert top in sys.stdlib_module_names or top in allowed, (path.name, name)
                if top == "jobhunt":
                    assert name == "jobhunt.portal" or name.startswith("jobhunt.portal.protocol") or name.startswith("jobhunt.portal.server"), (path.name, name)


# ---------------------------------------------------------------- a real server process
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_server_process_and_bad_config(tmp_path):
    port = _free_port()
    env = {**os.environ, **make_env(tmp_path, PORTAL_PUBLIC_ORIGIN=f"http://127.0.0.1:{port}", PORTAL_ALLOW_INSECURE_LOCAL="1"), "PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "-m", "jobhunt.portal.server"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        body = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/app/healthz", timeout=2) as resp:
                    body, headers = resp.read(), resp.headers
                break
            except OSError:
                import time
                time.sleep(0.1)
        assert body == b'{"ok":true}'
        assert "server" not in {k.lower() for k in headers} and headers["X-Frame-Options"] == "DENY"
    finally:
        proc.terminate()
        proc.wait(10)
    bad = subprocess.run([sys.executable, "-m", "jobhunt.portal.server"], env={**env, "PORTAL_DATA_KEY": "SECRETVALUE"}, capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2 and "PORTAL_DATA_KEY" in bad.stderr and "SECRETVALUE" not in bad.stderr + bad.stdout
