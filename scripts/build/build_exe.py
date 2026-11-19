# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""
Builds dist/amv-gn-bridge.exe in an isolated venv, validates it via `selftest`,
generates dist/amv-gn-bridge.exe.sha256, and writes dist/build_report.json.

Usage:
  python scripts/build/build_exe.py [--use-current-python]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
DIST = ROOT / "dist"
VENV = BUILD / "venv"
VENV_PYTHON = VENV / "Scripts" / "python.exe"
SPEC = ROOT / "scripts" / "build" / "amv-gn-bridge.spec"
EXE = DIST / "amv-gn-bridge.exe"
SHA_FILE = DIST / "amv-gn-bridge.exe.sha256"
REPORT = DIST / "build_report.json"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def ensure_venv() -> Path:
    if not VENV_PYTHON.exists():
        log(f"Creating isolated build venv: {VENV}")
        venv.create(VENV, with_pip=True, clear=True)
    log("Installing dependencies & PyInstaller into build venv")
    run(
        VENV_PYTHON, "-m", "pip", "install", "--upgrade", "--quiet", "--disable-pip-version-check",
        "-r", ROOT / "requirements.txt", "pyinstaller>=6.10.0",
    )
    return VENV_PYTHON


def run_selftest() -> dict:
    out = BUILD / "selftest.json"
    out.unlink(missing_ok=True)
    log("Running EXE self-test...")
    proc = subprocess.run([str(EXE), "selftest", "--out", str(out)], timeout=180)
    if proc.returncode != 0 or not out.is_file():
        raise SystemExit(f"[build] Self-test failed (exit={proc.returncode})")
    data = json.loads(out.read_text(encoding="utf-8"))
    if not data.get("ok"):
        raise SystemExit(f"[build] Self-test reported failure: {data}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-current-python", action="store_true", help="Skip isolated venv and use current Python (requires pyinstaller installed)")
    args = parser.parse_args()

    t0 = time.time()
    py = Path(sys.executable) if args.use_current_python else ensure_venv()

    log("Running PyInstaller...")
    run(
        py, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", DIST, "--workpath", BUILD / "pyi",
        SPEC, cwd=ROOT,
    )
    if not EXE.is_file():
        raise SystemExit(f"[build] Expected EXE not found: {EXE}")

    selftest = run_selftest()
    sha256 = hashlib.sha256(EXE.read_bytes()).hexdigest()
    SHA_FILE.write_text(f"{sha256}  {EXE.name}\n", encoding="utf-8")

    size_mb = round(EXE.stat().st_size / 1048576, 2)
    report = {
        "exe": str(EXE),
        "version": selftest.get("version"),
        "size_mb": size_mb,
        "sha256": sha256,
        "selftest": selftest,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Build OK: {EXE} ({size_mb} MB, sha256={sha256[:12]}...) in {report['elapsed_sec']}s")


if __name__ == "__main__":
    main()
