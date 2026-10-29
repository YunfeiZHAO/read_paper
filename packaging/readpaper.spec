# PyInstaller spec for the Read Paper desktop app.
#
# Build from the repo root:  pyinstaller packaging/readpaper.spec
# PyInstaller is NOT a cross-compiler: build the macOS .app on macOS, the Windows .exe on
# Windows, and the Linux binary on Linux. See the build_*.sh / build_windows.ps1 scripts.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is injected by PyInstaller; the project root is its parent.
ROOT = Path(SPECPATH).resolve().parent

# Ship the server's view/asset files. app.config.BASE_DIR resolves to sys._MEIPASS when frozen,
# so these must land at the bundle root as "templates" and "static".
datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
]

# uvicorn/anthropic load some modules dynamically; pull them in explicitly.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("anthropic")
    + ["app", "app.routes", "app.runners", "app.runners.claude"]
)

block_cipher = None

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # The Codex backend needs an external CLI + account pool that can't be bundled; the
    # desktop app targets the Claude backend only.
    excludes=["sub_pool_client"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ReadPaper",
    console=False,  # GUI app: no terminal window
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="ReadPaper",
)

# On macOS, wrap the COLLECT output in a proper .app bundle.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ReadPaper.app",
        bundle_identifier="com.readpaper.app",
        info_plist={
            "CFBundleName": "Read Paper",
            "CFBundleDisplayName": "Read Paper",
            "NSHighResolutionCapable": True,
        },
    )
