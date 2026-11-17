# PyInstaller spec. Build from the repo root:  uv run --group package pyinstaller packaging/jobhunt.spec --noconfirm
# One-folder build (fast start-up). macOS: dist/Jobhunt.app. Windows: dist/Jobhunt/Jobhunt.exe (window, no console)
# plus jobhunt-cli.exe (same app, with a console, for `jobhunt-cli search 1` and friends).
# PyInstaller cannot cross-compile: build each OS on that OS (see .github/workflows/build.yml).

import os
import sys
import tomllib

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
ICON = os.path.join(SPECPATH, "icon.png")  # converted to .icns / .ico by Pillow
with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]

hiddenimports = (
    collect_submodules("jobhunt")  # sources are looked up by name at runtime
    + collect_submodules("uvicorn")  # loops / protocols / lifespan are imported from strings
)
datas = (
    collect_data_files("jobhunt")  # web templates, static files, ROME / location tables
    # Browser automation driver (Node + the patchright JS package) for this OS only.
    + collect_data_files("patchright", includes=["driver/**"])
)

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[SRC],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "respx", "PyInstaller", "PIL"],  # PIL: only for the icon at build time
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(exclude_binaries=True, upx=False, icon=ICON, target_arch=os.environ.get("JOBHUNT_TARGET_ARCH") or None)
exes = [EXE(pyz, a.scripts, [], name="Jobhunt", console=False, **common)]
if sys.platform == "win32":
    exes.append(EXE(pyz, a.scripts, [], name="jobhunt-cli", console=True, **common))

coll = COLLECT(*exes, a.binaries, a.datas, name="Jobhunt", upx=False)

# The patchright driver's Node binary must stay executable after collection.
if sys.platform != "win32":
    node = os.path.join(DISTPATH, "Jobhunt", "_internal", "patchright", "driver", "node")
    if os.path.exists(node):
        os.chmod(node, 0o755)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Jobhunt.app",
        icon=ICON,
        bundle_identifier="io.github.kkonkobo.jobhunt",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "Jobhunt",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            # The window shows the local web UI at http://127.0.0.1.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
