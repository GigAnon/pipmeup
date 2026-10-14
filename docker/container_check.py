#!/usr/bin/env python3
"""Runs INSIDE the test container: offline install of the bundle, end to end.

Mounts expected:
    /bundle  (read-only)   the bundle under test
    /check   (read-only)   this directory
    /out     (writable)    receives report-<target>.json

Environment:
    TARGET   preset name matching the bundle's locks (debian12 / debian13)
    SKIP     comma-separated distribution names to leave out (e.g. wxpython)

Stages:
    1. mirrortool.py verify          integrity against MANIFEST.sha256
    2. per-root install              `pip install --index-url file:///bundle/simple <root>`
                                     in one venv, one root at a time, so every
                                     failure is attributed to its root
    3. pip check                     consistency of the installed set
    4. lock install                  --require-hashes from locks/requirements-<TARGET>.txt
                                     into a fresh venv (skipped when SKIP is set)
    5. import check                  every top-level module of every installed root

Note: pip's --no-index IGNORES --index-url, so it must not be used here; the
network isolation comes from `docker run --network none`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BUNDLE = Path(os.environ.get("BUNDLE_DIR", "/bundle"))
OUT = Path(os.environ.get("OUT_DIR", "/out"))
TARGET = os.environ.get("TARGET", "")
SKIP = {s.strip().lower() for s in os.environ.get("SKIP", "").split(",") if s.strip()}
INDEX = f"file://{BUNDLE}/simple"
INSTALL_TIMEOUT = int(os.environ.get("INSTALL_TIMEOUT", "10800"))
WORK = Path(os.environ.get("WORK_DIR", "/tmp"))
VENV_ROOTS = WORK / "venv-roots"
VENV_LOCK = WORK / "venv-lock"


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def make_venv(path: Path) -> Path:
    result = run([sys.executable, "-m", "venv", str(path)])
    if result.returncode != 0:
        sys.exit(f"cannot create venv: {tail(result.stderr)}")
    return path / "bin" / "python"


def pip(py: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    return run([str(py), "-m", "pip", "--disable-pip-version-check", "--no-input",
                *args], timeout=timeout)


def main() -> int:
    if not TARGET:
        sys.exit("TARGET is not set")
    roots_file = BUNDLE / "locks" / f"roots-{TARGET}.txt"
    lock_file = BUNDLE / "locks" / f"requirements-{TARGET}.txt"
    if not roots_file.is_file():
        sys.exit(f"{roots_file} not found: does the bundle contain target '{TARGET}'?")

    report: dict = {"target": TARGET, "python": sys.version.split()[0], "skipped": sorted(SKIP)}
    started = time.time()

    # 1. integrity --------------------------------------------------------
    print("== 1/5 integrity (mirrortool verify)")
    verify = run([sys.executable, str(BUNDLE / "mirrortool.py"), "verify"])
    print(tail(verify.stdout + verify.stderr, 5))
    report["verify_ok"] = verify.returncode == 0

    # 2. per-root install -------------------------------------------------
    roots = [r for r in roots_file.read_text().split() if canon(r) not in SKIP]
    print(f"== 2/5 installing {len(roots)} root(s), one at a time")
    py = make_venv(VENV_ROOTS)
    failures: dict[str, str] = {}
    for index, root in enumerate(roots, 1):
        t0 = time.time()
        try:
            result = pip(py, "install", "--index-url", INDEX, root, timeout=INSTALL_TIMEOUT)
            ok = result.returncode == 0
            detail = "" if ok else tail(result.stdout + "\n" + result.stderr, 25)
        except subprocess.TimeoutExpired:
            ok, detail = False, f"timeout after {INSTALL_TIMEOUT}s"
        print(f"   [{index}/{len(roots)}] {'ok  ' if ok else 'FAIL'} {root}  ({time.time() - t0:.0f}s)")
        if not ok:
            failures[root] = detail
    report["install_failures"] = failures

    # 3. pip check --------------------------------------------------------
    print("== 3/5 pip check")
    check = pip(py, "check")
    print(tail(check.stdout + check.stderr, 10))
    report["pip_check_ok"] = check.returncode == 0
    report["pip_check_output"] = (check.stdout + check.stderr).strip()

    # 4. lock install -----------------------------------------------------
    if SKIP:
        print("== 4/5 lock install: skipped (SKIP is set, the lock still lists skipped packages)")
        report["lock_install_ok"] = None
    else:
        print("== 4/5 lock install with --require-hashes")
        lpy = make_venv(VENV_LOCK)
        try:
            result = pip(lpy, "install", "--index-url", INDEX, "--require-hashes",
                         "-r", str(lock_file), timeout=INSTALL_TIMEOUT)
            report["lock_install_ok"] = result.returncode == 0
            if result.returncode != 0:
                report["lock_install_output"] = tail(result.stdout + "\n" + result.stderr, 40)
        except subprocess.TimeoutExpired:
            report["lock_install_ok"] = False
            report["lock_install_output"] = "timeout"
        print("   ok" if report["lock_install_ok"] else "   FAIL")

    # 5. import check -----------------------------------------------------
    print("== 5/5 import check")
    probe = r"""
import importlib.metadata as md, json, sys
name = sys.argv[1]
try:
    dist = md.distribution(name)
except md.PackageNotFoundError:
    print(json.dumps({"mods": None})); raise SystemExit
top = dist.read_text("top_level.txt")
mods = set(top.split()) if top else set()
if not mods:
    for f in dist.files or []:
        p = f.parts
        if not p or p[0].endswith((".dist-info", ".egg-info", "__pycache__")) or p[0].startswith(".."):
            continue
        mods.add(p[0][:-3] if p[0].endswith(".py") and len(p) == 1 else p[0])
print(json.dumps({"mods": sorted(m for m in mods if m.isidentifier())}))
"""
    import_failures: dict[str, str] = {}
    for root in roots:
        if root in failures:
            continue
        found = run([str(py), "-c", probe, root])
        try:
            mods = json.loads(found.stdout)["mods"]
        except (json.JSONDecodeError, KeyError):
            mods = None
        if not mods:
            import_failures[root] = "no importable module found in the installed distribution"
            continue
        errors = []
        for mod in mods:
            try:
                res = run([str(py), "-c", f"import {mod}"], timeout=180)
                if res.returncode != 0:
                    errors.append(f"import {mod}: {tail(res.stderr, 3)}")
            except subprocess.TimeoutExpired:
                errors.append(f"import {mod}: timeout")
        if errors:
            import_failures[root] = " | ".join(errors)
    report["import_failures"] = import_failures
    print(f"   {len(roots) - len(failures) - len(import_failures)} fully importable, "
          f"{len(import_failures)} with import warnings")

    # summary ---------------------------------------------------------------
    report["duration_s"] = round(time.time() - started)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"report-{TARGET}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n== SUMMARY", TARGET)
    print(f"   verify        : {'ok' if report['verify_ok'] else 'FAIL'}")
    print(f"   install       : {len(roots) - len(failures)}/{len(roots)} roots")
    for root, detail in failures.items():
        print(f"      FAIL {root}\n" + "\n".join("         " + l for l in detail.splitlines()[-6:]))
    print(f"   pip check     : {'ok' if report['pip_check_ok'] else 'FAIL'}")
    print(f"   lock install  : {report['lock_install_ok']}")
    for root, detail in import_failures.items():
        print(f"      import warning {root}: {detail[:200]}")

    hard_failure = (not report["verify_ok"] or failures
                    or not report["pip_check_ok"] or report["lock_install_ok"] is False)
    return 1 if hard_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
