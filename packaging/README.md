# Desktop app (macOS and Windows)

Big Penalty ships as a normal desktop app: double-click it and the shortlist opens in its own window.
Closing the window stops Big Penalty. No Python, uv or terminal is needed on the user's computer.

## Installing

| Platform | Download | Install |
|---|---|---|
| Windows 10/11 (x64) | `Big-Penalty-windows-x64-setup.exe` | Run it. Installs for the current user (no admin rights), adds Start-menu and optional desktop shortcuts. A newer installer upgrades in place. |
| Windows, portable | `Big-Penalty-windows-x64.zip` | Unzip anywhere, run `Big Penalty.exe`. |
| macOS, Apple Silicon | `Big-Penalty-macos-arm64.zip` | Unzip, drag `Big Penalty.app` to *Applications*. |
| macOS, Intel | `Big-Penalty-macos-intel.zip` | Same. |

The builds are not signed with a paid certificate, so the first launch shows a warning:

- **macOS**: right-click `Big Penalty.app` → *Ouvrir*, then *Ouvrir* again. Only needed once.
- **Windows**: SmartScreen shows *Windows a protégé votre ordinateur*: *Informations complémentaires* → *Exécuter quand même*.

Requirements: **Google Chrome** installed (the job-board searches drive it, as in development). On Windows the window
uses the Edge WebView2 runtime, which Windows 11 and up-to-date Windows 10 already have. If the native window can't
open, Big Penalty opens in the default browser instead, with a small dialog whose button stops it.

## Settings, API keys and data

Everything the app stores lives in one per-user folder, outside the app, so updates and uninstalls keep it:

- macOS: `~/Library/Application Support/Big Penalty`
- Windows: `%LOCALAPPDATA%\Big Penalty`

| File | Contents |
|---|---|
| `.env` | API keys and options. Created on first launch as a commented template: fill in `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` and restart. Without keys, planning and scoring fall back to rules. |
| `jobhunt.sqlite3` | Profiles, jobs, scores. |
| `browser-profiles/` | Per-site browser sessions (cookies, solved CAPTCHAs). |
| `logs/jobhunt.log` | Server log, the first place to look if something fails. |

A real environment variable wins over `.env`, and `JOBHUNT_DATA_DIR` moves the whole folder. The development checkout
(`uv run jobhunt …` with `env.sh`) uses `$BP_ROOT/data` instead, so the app and the checkout have **separate databases**
unless you copy `jobhunt.sqlite3` across (with both closed) or point `JOBHUNT_DATA_DIR` at the same folder.

## Command line

The packaged app is also the CLI: `Big Penalty.app/Contents/MacOS/Big Penalty search 1` on macOS, `big-penalty-cli.exe search 1`
on Windows (`Big Penalty.exe` has no console). Every `jobhunt` command works.

## Building

PyInstaller can't cross-compile, so each OS builds its own app. Locally (macOS shown):

```
uv sync --group package
uv run --group package pyinstaller packaging/big_penalty.spec --noconfirm     # → dist/Big Penalty.app (dist/Big Penalty/ on Windows)
dist/Big Penalty.app/Contents/MacOS/Big Penalty --self-test report.json          # UI pages, browser driver, Chrome; no window
dist/Big Penalty.app/Contents/MacOS/Big Penalty --self-test-window window.json   # opens the native window, checks the UI loads
uv run --group package python packaging/make_icon.py                     # redraw packaging/icon.png (macOS only)
```

`.github/workflows/build.yml` runs the tests, builds Windows x64, macOS Apple Silicon and macOS Intel, runs the
self-tests on each packaged app, builds and test-installs the Windows installer (Inno Setup, `installer.iss`), and
uploads everything as run *Artifacts*. Pushing a tag such as `v0.1.0` publishes them as a GitHub Release. Bump
`version` in `pyproject.toml` first; the app and installer read it from there.

| File | Role |
|---|---|
| `src/jobhunt/desktop.py` | Launcher: data folder, `.env`, local server on 127.0.0.1 (port 8765, or a free one), native window, CLI passthrough, self-tests |
| `packaging/launch.py` | PyInstaller entry point |
| `packaging/big_penalty.spec` | What goes in the bundle (templates, data files, patchright's driver for this OS) |
| `packaging/installer.iss` | Windows installer |
| `packaging/icon.png` | App icon (from `assets/icon.svg`) |
