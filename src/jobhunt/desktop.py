"""Desktop launcher used by the packaged app (macOS .app, Windows .exe).

Double-click: starts the web UI on 127.0.0.1 and shows it in a native window (pywebview: WebKit on macOS,
Edge WebView2 on Windows). Closing the window stops everything. If the native window can't start, the UI
opens in the default browser and a small system dialog keeps the app alive until the user clicks it.

With arguments, the packaged binary behaves like the `jobhunt` CLI (`Big Penalty search 1`, `Big Penalty sources`, ...).
`--self-test REPORT.json` checks the bundle without a window; `--self-test-window REPORT.json` opens the native
window, checks that the UI loaded in it, and closes it. The build pipeline runs both.

Data lives in a per-user folder, never inside the app bundle, so updates keep it:
  macOS   ~/Library/Application Support/Big Penalty
  Windows %LOCALAPPDATA%\\Big Penalty
JOBHUNT_DATA_DIR still overrides it. API keys go in `.env` in that folder (a commented template is created).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_NAME = "Big Penalty"
DEFAULT_PORT = 8765

ENV_TEMPLATE = """\
# Big Penalty settings. Fill in the keys you have, then restart Big Penalty.
# Without any LLM key, Big Penalty still works with rule-based planning and scoring.
# This file stays on this computer; never share it.

ANTHROPIC_API_KEY=
OPENAI_API_KEY=
# JOBHUNT_LLM_PROVIDERS=anthropic,openai

# Optional sources
FIRECRAWL_API_KEY=
FRANCE_TRAVAIL_CLIENT_ID=
FRANCE_TRAVAIL_CLIENT_SECRET=
LABONNEALTERNANCE_API_KEY=

# 1 = run job-board browsers hidden (CAPTCHAs then can't be solved by hand)
# JOBHUNT_HEADLESS=0
"""


def default_data_dir(platform: str = sys.platform, home: Path | None = None, environ=os.environ) -> Path:
    home = home or Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if platform == "win32":
        base = environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / APP_NAME
    base = environ.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / "big-penalty"


def parse_env_file(text: str) -> dict[str, str]:
    """Minimal .env parser: KEY=value lines, '#' comments, optional quotes, optional 'export '."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:  # empty values in the template must not mask real environment variables
            values[key] = value
    return values


def prepare_environment(environ=os.environ) -> Path:
    """Pick the data folder, create the .env template, load it. Must run before jobhunt.config is used."""
    data_dir = Path(environ.get("JOBHUNT_DATA_DIR") or default_data_dir(environ=environ))
    data_dir.mkdir(parents=True, exist_ok=True)
    environ["JOBHUNT_DATA_DIR"] = str(data_dir)
    env_file = data_dir / ".env"
    if not env_file.exists():
        env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
        if os.name != "nt":
            env_file.chmod(0o600)
    for key, value in parse_env_file(env_file.read_text(encoding="utf-8")).items():
        environ.setdefault(key, value)  # a real environment variable wins over the file
    return data_dir


def redirect_output(data_dir: Path) -> None:
    """A windowed app has no console (sys.stdout is None on Windows), which crashes uvicorn's logging."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    stream = open(log_dir / "jobhunt.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives until exit
    sys.stdout = sys.stderr = stream


def find_port(preferred: int = DEFAULT_PORT) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise RuntimeError("no free local port")


def start_server(port: int):
    import uvicorn

    from jobhunt.web.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="jobhunt-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the web server did not start; see logs/jobhunt.log")
        time.sleep(0.05)
    return server, thread


def stop_server(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)


def wait_with_system_dialog(url: str) -> None:
    """Fallback when no native window: browser + a blocking dialog that acts as the 'Quit' button."""
    import webbrowser

    webbrowser.open(url)
    message = f"Big Penalty est ouvert dans votre navigateur :\n{url}\n\nCliquez sur Quitter pour arrêter Big Penalty."
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message.replace("Quitter", "OK"), APP_NAME, 0x40)
    elif sys.platform == "darwin":
        script = f'display dialog {json.dumps(message)} buttons {{"Quitter"}} default button 1 with title "{APP_NAME}"'
        subprocess.run(["osascript", "-e", script], check=False)
    else:
        print(message.replace("Cliquez sur Quitter", "Ctrl+C"), flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


def run_window(url: str) -> None:
    try:
        import webview
    except Exception:  # noqa: BLE001 - any import problem means: use the browser instead
        wait_with_system_dialog(url)
        return
    try:
        webview.create_window(APP_NAME, url, width=1360, height=900, min_size=(900, 600))
        webview.start()
    except Exception:  # noqa: BLE001 - e.g. WebView2 runtime missing on an old Windows 10
        import logging

        logging.getLogger("jobhunt.desktop").exception("native window failed; falling back to the browser")
        wait_with_system_dialog(url)


def self_test(report_path: Path) -> int:
    """Start the server, fetch the main pages, start the bundled browser driver. No window."""
    import asyncio
    import urllib.request

    frozen = bool(getattr(sys, "frozen", False))
    report: dict[str, object] = {
        "data_dir": os.environ.get("JOBHUNT_DATA_DIR"), "frozen": frozen, "utf8_mode": sys.flags.utf8_mode,
    }
    ok = not frozen or sys.flags.utf8_mode == 1  # the bundle must run in UTF-8 mode (see big_penalty.spec)
    server = thread = None
    try:
        port = find_port(0)
        server, thread = start_server(port)
        pages = {}
        for path in ("/", "/static/style.css", "/sources"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=60) as resp:
                pages[path] = resp.status
        report["pages"] = pages
        ok &= all(status == 200 for status in pages.values())
    except Exception as e:  # noqa: BLE001
        report["server_error"] = repr(e)
        ok = False
    finally:
        if server is not None:
            stop_server(server, thread)

    async def driver_check() -> dict[str, object]:
        from patchright.async_api import async_playwright

        result: dict[str, object] = {}
        async with async_playwright() as pw:
            result["driver"] = "ok"
            try:  # informational: needs Google Chrome installed on the machine
                browser = await pw.chromium.launch(channel="chrome", headless=True)
                page = await browser.new_page()
                await page.set_content("<title>jobhunt</title>")
                result["chrome"] = await page.title() == "jobhunt"
                await browser.close()
            except Exception as e:  # noqa: BLE001
                result["chrome"] = f"unavailable: {e.__class__.__name__}"
        return result

    try:
        report["browser"] = asyncio.run(driver_check())
    except Exception as e:  # noqa: BLE001
        report["browser"] = {"driver_error": repr(e)}
        ok = False

    try:
        import webview  # noqa: F401

        report["webview"] = "importable"
    except Exception as e:  # noqa: BLE001
        report["webview"] = f"import failed: {e!r}"
        ok = False

    report["ok"] = ok
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if ok else 1


def self_test_window(report_path: Path, timeout_s: float = 90) -> int:
    """Open the real native window on the local UI, read the page title through the web view, close."""
    import webview

    report: dict[str, object] = {"ok": False}
    server, thread = start_server(find_port(0))
    url = f"http://127.0.0.1:{server.config.port}/"
    window = webview.create_window(APP_NAME, url, width=1000, height=700)

    def probe() -> None:
        try:
            if not window.events.loaded.wait(timeout_s):
                report["error"] = "page did not load in time"
                return
            title = window.evaluate_js("document.title")
            report["title"] = title
            report["ok"] = bool(title)
        except Exception as e:  # noqa: BLE001
            report["error"] = repr(e)
        finally:
            window.destroy()

    try:
        webview.start(probe)
    finally:
        stop_server(server, thread)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    argv = sys.argv[1:] if argv is None else argv
    # macOS passes -psn_0_12345 to apps started from Finder on old systems.
    argv = [a for a in argv if not a.startswith("-psn_")]
    data_dir = prepare_environment()
    frozen = bool(getattr(sys, "frozen", False))

    if argv[:1] in (["--self-test"], ["--self-test-window"]):
        report = Path(argv[1] if len(argv) > 1 else "selftest.json").resolve()
        if frozen and sys.stdout is None:
            redirect_output(data_dir)
        return (self_test if argv[0] == "--self-test" else self_test_window)(report)

    if argv:  # behave like the jobhunt CLI
        from jobhunt.cli import app

        app(args=argv, prog_name="jobhunt")
        return 0

    if frozen or sys.stdout is None:
        redirect_output(data_dir)
    port = find_port()
    server, thread = start_server(port)
    try:
        run_window(f"http://127.0.0.1:{port}/")
    finally:
        stop_server(server, thread)
    return 0


if __name__ == "__main__":
    sys.exit(main())
