#!/usr/bin/env python3
"""One-time setup: create a venv at ~/.readpaper-bridge/venv with pre-installed packages.

The bridge server (server.py) detects this venv automatically and injects it into the
claude subprocess environment, so the LLM's bash tool can `import pypdf` etc. without
running `pip install` on every request.

Run once (or re-run to add more packages):
    python3 bridge/setup_env.py

Add more packages to PACKAGES below, then re-run.
Uses uv if available (fast), falls back to venv + pip.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

VENV_DIR = Path(os.environ.get("READPAPER_BRIDGE_VENV",
                                str(Path.home() / ".readpaper-bridge" / "venv")))

# Packages pre-installed into the venv so the LLM never needs to install them at runtime.
# Add any package name here and re-run this script.
PACKAGES = [
    "pypdf",          # PDF parsing (most common ask)
    "pdfminer.six",   # alternative PDF text extractor
    "Pillow",         # image processing
    "requests",       # HTTP
    "pandas",         # data tables
    "numpy",          # numerics
    "matplotlib",     # plotting (non-interactive backend works headlessly)
    "scipy",          # scientific computing
    "beautifulsoup4", # HTML/XML parsing
    "lxml",           # fast XML/HTML parser (bs4 optional dep)
]


def _run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(args))
    return subprocess.run(list(args), check=True, **kwargs)


def main() -> None:
    uv = shutil.which("uv")

    if VENV_DIR.exists():
        print(f"venv already exists at {VENV_DIR}")
        print("Re-running to sync packages (new packages in PACKAGES will be installed).")
    else:
        print(f"Creating venv at {VENV_DIR} ...")
        VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        if uv:
            _run(uv, "venv", str(VENV_DIR))
        else:
            _run(sys.executable, "-m", "venv", str(VENV_DIR))

    python = str(VENV_DIR / "bin" / "python")
    pip_base = [uv, "pip", "install", "--python", python] if uv else [python, "-m", "pip", "install", "-q"]

    print(f"\nInstalling {len(PACKAGES)} packages into {VENV_DIR} ...")
    _run(*pip_base, *PACKAGES)

    # Write a CLAUDE.md into the work dir so the LLM knows what is already available.
    work_dir = VENV_DIR.parent / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    claude_md = work_dir / "CLAUDE.md"
    pkg_list = "\n".join(f"- `{p}`" for p in PACKAGES)
    claude_md.write_text(
        f"# Pre-installed Python environment\n\n"
        f"A virtual environment is pre-configured at `{VENV_DIR}`.\n"
        f"It is already active (PATH and VIRTUAL_ENV are set by the bridge).\n\n"
        f"The following packages are pre-installed — **do not `pip install` them**:\n\n"
        f"{pkg_list}\n\n"
        f"Use plain `python` or `python3` to run scripts; the venv Python is first on PATH.\n"
        f"You can still `pip install <other-package>` for anything not in the list above.\n",
        encoding="utf-8",
    )
    print(f"\nWrote {claude_md}")
    print(f"\nDone. Restart the bridge server (./bridge/install.sh or launchctl kickstart) to pick up the new venv.")


if __name__ == "__main__":
    main()
