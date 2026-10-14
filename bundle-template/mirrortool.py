#!/usr/bin/env python3
"""mirrortool - maintenance tool shipped inside a PyPI mirror bundle.

Self-sufficient: depends only on the Python standard library, plus the
`packaging` library, which is looked up in this order:

    1. a regular `import packaging`;
    2. the copy vendored in pip (`pip._vendor.packaging`);
    3. a `packaging-*.whl` found in the bundle itself (wheels are zip
       importable, so no installation is needed).

Bundle layout (relative to the directory containing this script):

    packages/          flat directory of wheels and sdists
    simple/            PEP 503 static index (generated, safe to delete)
    locks/             per-target lockfiles + protected.txt
    meta.json          per-file Requires-Python recorded at build time
    MANIFEST.sha256    sha256 of every file under packages/
    .trash/            quarantine used by `gc` (created on demand)

Commands:

    python mirrortool.py reindex            rebuild simple/ and MANIFEST.sha256
    python mirrortool.py verify             check files against MANIFEST.sha256
    python mirrortool.py gc [--apply]       retention-based cleanup (dry run by default)
    python mirrortool.py purge-trash DAYS [--apply]

Typical offline use:

    pip install --index-url file://$PWD/simple <package>   # NOT --no-index: it ignores --index-url

Typical cleanup:

    python mirrortool.py gc --keep-last 2 --protect locks/protected.txt
    python mirrortool.py gc --keep-last 2 --protect locks/protected.txt --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.parser
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tarfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent

PACKAGES_DIRNAME = "packages"
SIMPLE_DIRNAME = "simple"
TRASH_DIRNAME = ".trash"
META_FILENAME = "meta.json"
MANIFEST_FILENAME = "MANIFEST.sha256"

ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".zip", ".tar.bz2", ".tar.xz")
TRASH_STAMP_FMT = "%Y%m%dT%H%M%S"


# --------------------------------------------------------------------------
# packaging bootstrap
# --------------------------------------------------------------------------

def _import_packaging(root: Path) -> None:
    """Make `packaging` importable, falling back to pip's copy or a bundled wheel."""
    try:
        import packaging  # noqa: F401
        return
    except ImportError:
        pass

    try:
        from pip._vendor import packaging as vendored  # type: ignore[attr-defined]
        sys.modules.setdefault("packaging", vendored)
        for sub in ("utils", "version", "tags", "markers", "specifiers", "requirements"):
            try:
                module = __import__(f"pip._vendor.packaging.{sub}", fromlist=["_"])
            except ImportError:
                continue
            sys.modules.setdefault(f"packaging.{sub}", module)
        return
    except ImportError:
        pass

    for wheel in sorted((root / PACKAGES_DIRNAME).glob("packaging-*.whl"), reverse=True):
        sys.path.append(str(wheel))
        try:
            import packaging  # noqa: F401
            return
        except ImportError:
            sys.path.remove(str(wheel))

    sys.exit("error: the 'packaging' library is not available "
             "(not installed, not vendored in pip, no packaging-*.whl in the bundle)")


_import_packaging(ROOT)

from packaging.utils import (  # noqa: E402
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version  # noqa: E402


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str          # canonical PEP 503 name
    version: Version
    kind: str          # 'wheel' | 'sdist'


def _full_suffix(path: Path) -> str:
    """'.tar.gz' rather than '.gz' for double extensions."""
    for suffix in ARCHIVE_SUFFIXES:
        if path.name.lower().endswith(suffix):
            return suffix
    return path.suffix


def parse_artifact(path: Path) -> Artifact | None:
    """Parse an artifact filename; None when unrecognised.

    Relies on `packaging` rather than a home-made regex: wheels with build tags
    (foo-1.0-1-py3-none-any.whl) and local versions (1.0+local.1) break naive
    heuristics.
    """
    suffix = _full_suffix(path)

    if suffix == ".whl":
        try:
            name, version, _build, _tags = parse_wheel_filename(path.name)
        except (InvalidWheelFilename, InvalidVersion):
            return None
        return Artifact(path, canonicalize_name(name), version, "wheel")

    if suffix in (".tar.gz", ".zip"):
        try:
            name, version = parse_sdist_filename(path.name)
        except (InvalidSdistFilename, InvalidVersion):
            return None
        return Artifact(path, canonicalize_name(name), version, "sdist")

    if suffix in (".tar.bz2", ".tar.xz"):
        # packaging does not handle these extensions; cautious fallback.
        stem = path.name[: -len(suffix)]
        if "-" not in stem:
            return None
        raw_name, _, raw_version = stem.rpartition("-")
        try:
            version = Version(raw_version)
        except InvalidVersion:
            return None
        return Artifact(path, canonicalize_name(raw_name), version, "sdist")

    return None


def scan(root: Path) -> tuple[list[Artifact], list[Path]]:
    """Return (parsed artifacts, unrecognised files) found under packages/."""
    artifacts: list[Artifact] = []
    unknown: list[Path] = []
    packages = root / PACKAGES_DIRNAME

    if not packages.is_dir():
        return artifacts, unknown

    for path in sorted(packages.iterdir()):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        if not path.name.lower().endswith(ARCHIVE_SUFFIXES):
            continue
        parsed = parse_artifact(path)
        if parsed is None:
            unknown.append(path)
        else:
            artifacts.append(parsed)

    return artifacts, unknown


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Requires-Python metadata
# --------------------------------------------------------------------------

def load_meta(root: Path) -> dict[str, dict[str, Any]]:
    path = root / META_FILENAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_meta(root: Path, meta: dict[str, dict[str, Any]]) -> None:
    (root / META_FILENAME).write_text(
        json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


def _requires_python_from_text(text: str) -> str | None:
    message = email.parser.Parser().parsestr(text, headersonly=True)
    return message.get("Requires-Python") or None


def extract_requires_python(path: Path) -> str | None:
    """Read Requires-Python from the artifact itself (fallback when meta.json lacks it)."""
    name = path.name.lower()
    try:
        if name.endswith(".whl"):
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    parts = member.split("/")
                    if len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "METADATA":
                        return _requires_python_from_text(
                            archive.read(member).decode("utf-8", "replace"))
        elif name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
            with tarfile.open(path) as archive:
                for member in archive:
                    parts = member.name.split("/")
                    if len(parts) == 2 and parts[1] == "PKG-INFO":
                        handle = archive.extractfile(member)
                        if handle is not None:
                            return _requires_python_from_text(
                                handle.read().decode("utf-8", "replace"))
        elif name.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    parts = member.split("/")
                    if len(parts) == 2 and parts[1] == "PKG-INFO":
                        return _requires_python_from_text(
                            archive.read(member).decode("utf-8", "replace"))
    except (OSError, tarfile.TarError, zipfile.BadZipFile, KeyError):
        return None
    return None


# --------------------------------------------------------------------------
# Index and manifest
# --------------------------------------------------------------------------

def build_simple_index(root: Path, artifacts: list[Artifact],
                       hashes: dict[Path, str]) -> tuple[int, int]:
    """Generate a static PEP 503 index in <root>/simple/.

    Usable directly by pip through a file:// URL (or any static HTTP server),
    which is more faithful than --find-links: pip then applies its normal index
    logic (requires-python, version selection, hash checking).
    """
    simple_dir = root / SIMPLE_DIRNAME
    if simple_dir.exists():
        shutil.rmtree(simple_dir)
    simple_dir.mkdir(parents=True)

    meta = load_meta(root)
    grouped: dict[str, list[Artifact]] = defaultdict(list)
    for art in artifacts:
        grouped[art.name].append(art)

    total_files = 0
    for cname, arts in sorted(grouped.items()):
        project_dir = simple_dir / cname
        project_dir.mkdir(parents=True, exist_ok=True)

        rows = []
        for art in sorted(arts, key=lambda a: a.path.name):
            href = os.path.relpath(art.path, project_dir).replace(os.sep, "/")
            requires_python = (meta.get(art.path.name) or {}).get("requires_python")
            if requires_python is None:
                requires_python = extract_requires_python(art.path)
            attributes = ""
            if requires_python:
                # PEP 503: the attribute must be HTML-escaped ('>' in specifiers).
                attributes = (
                    f' data-requires-python="{html.escape(requires_python, quote=True)}"'
                )
            rows.append(
                f'    <a href="{html.escape(href, quote=True)}#sha256={hashes[art.path]}"'
                f'{attributes}>{html.escape(art.path.name)}</a><br/>'
            )
            total_files += 1

        (project_dir / "index.html").write_text(
            "<!DOCTYPE html>\n<html>\n  <head>\n"
            '    <meta name="pypi:repository-version" content="1.0"/>\n'
            f"    <title>Links for {html.escape(cname)}</title>\n"
            f"  </head>\n  <body>\n    <h1>Links for {html.escape(cname)}</h1>\n"
            + "\n".join(rows)
            + "\n  </body>\n</html>\n",
            encoding="utf-8",
        )

    root_rows = "\n".join(
        f'    <a href="{html.escape(name)}/">{html.escape(name)}</a><br/>'
        for name in sorted(grouped)
    )
    (simple_dir / "index.html").write_text(
        "<!DOCTYPE html>\n<html>\n  <head>\n"
        '    <meta name="pypi:repository-version" content="1.0"/>\n'
        "    <title>Simple index</title>\n  </head>\n  <body>\n"
        + root_rows
        + "\n  </body>\n</html>\n",
        encoding="utf-8",
    )
    return len(grouped), total_files


def write_manifest(root: Path, artifacts: Iterable[Artifact],
                   hashes: dict[Path, str], out_path: Path | None = None) -> Path:
    """Write a manifest in `sha256sum` format (paths relative to the bundle root)."""
    lines = []
    for art in sorted(artifacts, key=lambda a: a.path.name):
        digest = hashes.get(art.path) or sha256_of(art.path)
        lines.append(f"{digest}  {art.path.relative_to(root).as_posix()}")
    out = out_path or (root / MANIFEST_FILENAME)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def reindex(root: Path, rebuild_index: bool = True) -> tuple[int, int, int]:
    """Rebuild simple/ and MANIFEST.sha256 from what is on disk.

    Returns (projects, files, unrecognised).
    """
    artifacts, unknown = scan(root)
    hashes = {art.path: sha256_of(art.path) for art in artifacts}

    # Keep meta.json in sync with the files actually present.
    meta = load_meta(root)
    present = {art.path.name for art in artifacts}
    for name in list(meta):
        if name not in present:
            del meta[name]
    for art in artifacts:
        entry = meta.setdefault(art.path.name, {})
        if entry.get("requires_python") is None:
            entry["requires_python"] = extract_requires_python(art.path)
    save_meta(root, meta)

    projects = files = 0
    if rebuild_index:
        projects, files = build_simple_index(root, artifacts, hashes)
    write_manifest(root, artifacts, hashes)
    return projects, files, len(unknown)


def verify(root: Path) -> int:
    """Check packages/ against MANIFEST.sha256. Returns the number of problems."""
    manifest = root / MANIFEST_FILENAME
    if not manifest.is_file():
        print(f"error: {manifest} not found", file=sys.stderr)
        return 1

    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        expected[relative] = digest

    problems = 0
    for relative, digest in sorted(expected.items()):
        path = root / relative
        if not path.is_file():
            print(f"  MISSING   {relative}")
            problems += 1
        elif sha256_of(path) != digest:
            print(f"  CORRUPT   {relative}")
            problems += 1

    artifacts, unknown = scan(root)
    listed = set(expected)
    for art in artifacts:
        relative = art.path.relative_to(root).as_posix()
        if relative not in listed:
            print(f"  UNLISTED  {relative}")
            problems += 1
    for path in unknown:
        print(f"  UNKNOWN   {path.relative_to(root).as_posix()}")

    print(f"{len(expected)} file(s) listed, {problems} problem(s)")
    return problems


# --------------------------------------------------------------------------
# Garbage collection
# --------------------------------------------------------------------------

_PIN_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:\[[^\]]*\])?\s*=="
    r"\s*(?P<version>[^\s;#\\]+)"
)


def load_protected(paths: list[Path]) -> set[tuple[str, Version]]:
    """(canonical name, version) pairs pinned with '==' in the given requirements files.

    Loose constraints do not designate a precise artifact and protect nothing.
    """
    protected: set[tuple[str, Version]] = set()

    for req_path in paths:
        if not req_path.is_file():
            print(f"warning: protection file not found: {req_path}", file=sys.stderr)
            continue
        for line in req_path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            match = _PIN_RE.match(line)
            if not match:
                continue
            try:
                version = Version(match.group("version"))
            except InvalidVersion:
                continue
            protected.add((canonicalize_name(match.group("name")), version))

    return protected


def select_obsolete(
    artifacts: list[Artifact],
    keep_last: int,
    protected: set[tuple[str, Version]],
) -> tuple[list[Artifact], dict[str, list[Version]]]:
    """Return (artifacts to evict, kept versions per distribution).

    Policy: keep the N newest versions of each distribution, ALL files of a kept
    version (every wheel, every platform, plus sdists), and anything pinned in a
    protection file. Pre-releases only count when no stable release exists.
    """
    by_name: dict[str, set[Version]] = defaultdict(set)
    for art in artifacts:
        by_name[art.name].add(art.version)

    kept_versions: dict[str, list[Version]] = {}

    for name, versions in by_name.items():
        stable = sorted((v for v in versions if not v.is_prerelease), reverse=True)
        # Fall back to pre-releases only when no stable version exists:
        # otherwise a recent alpha would evict the latest stable.
        pool = stable if stable else sorted(versions, reverse=True)
        keep = set(pool[:keep_last])
        keep |= {v for v in versions if (name, v) in protected}
        kept_versions[name] = sorted(keep, reverse=True)

    obsolete = [art for art in artifacts if art.version not in kept_versions[art.name]]
    return obsolete, kept_versions


def quarantine(root: Path, obsolete: list[Artifact], apply: bool) -> Path | None:
    """Move obsolete artifacts to .trash/<timestamp>/ instead of deleting them."""
    if not obsolete:
        return None

    stamp = dt.datetime.now().strftime(TRASH_STAMP_FMT)
    trash_dir = root / TRASH_DIRNAME / stamp

    if not apply:
        return trash_dir

    trash_dir.mkdir(parents=True, exist_ok=True)
    for art in obsolete:
        target = trash_dir / art.path.name
        counter = 1
        while target.exists():
            target = trash_dir / f"{art.path.stem}.{counter}{art.path.suffix}"
            counter += 1
        shutil.move(str(art.path), str(target))

    return trash_dir


def purge_trash(root: Path, older_than_days: int, apply: bool) -> list[Path]:
    """Permanently delete quarantine batches older than the given age."""
    trash_root = root / TRASH_DIRNAME
    if not trash_root.is_dir():
        return []

    cutoff = dt.datetime.now() - dt.timedelta(days=older_than_days)
    purged: list[Path] = []

    for batch in sorted(trash_root.iterdir()):
        if not batch.is_dir():
            continue
        try:
            stamp = dt.datetime.strptime(batch.name, TRASH_STAMP_FMT)
        except ValueError:
            continue
        if stamp >= cutoff:
            continue
        purged.append(batch)
        if apply:
            shutil.rmtree(batch)

    return purged


def cmd_gc(root: Path, args: argparse.Namespace) -> int:
    if args.keep_last < 1:
        print("error: --keep-last must be at least 1", file=sys.stderr)
        return 2

    artifacts, unknown = scan(root)
    protected_files = [p if p.is_absolute() else Path.cwd() / p for p in args.protect]
    if not args.protect:
        default = root / "locks" / "protected.txt"
        if default.is_file():
            protected_files = [default]
    protected = load_protected(protected_files)
    obsolete, kept_versions = select_obsolete(artifacts, args.keep_last, protected)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Bundle {root} [{mode}] ===")
    print(f"  distributions     : {len(kept_versions)}")
    print(f"  artifacts         : {len(artifacts)}")
    print(f"  kept              : {len(artifacts) - len(obsolete)}")
    print(f"  obsolete          : {len(obsolete)}")
    print(f"  unrecognised      : {len(unknown)}")
    print(f"  protected pins    : {len(protected)}")

    if unknown:
        print("\n--- Unrecognised files (never deleted, please inspect) ---")
        for path in unknown:
            print(f"  ? {path.relative_to(root)}")

    if obsolete:
        print("\n--- Evicted artifacts ---")
        by_name: dict[str, list[Artifact]] = defaultdict(list)
        for art in obsolete:
            by_name[art.name].append(art)
        for name in sorted(by_name):
            kept = ", ".join(str(v) for v in kept_versions[name]) or "(none)"
            print(f"  {name}  [kept: {kept}]")
            for art in sorted(by_name[name], key=lambda a: a.path.name):
                print(f"      - {art.path.relative_to(root)}")

        trash_dir = quarantine(root, obsolete, args.apply)
        verb = "moved to" if args.apply else "would be moved to"
        print(f"\n  {len(obsolete)} artifact(s) {verb} {trash_dir}")

    if args.apply and obsolete:
        projects, files, _ = reindex(root)
        print(f"\n  reindexed: {projects} distribution(s), {files} file(s); manifest rewritten")
    elif not args.apply and obsolete:
        print("\n  Re-run with --apply to apply (index and manifest are rebuilt automatically).")

    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintenance tool for a PyPI mirror bundle.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("reindex", help="rebuild simple/ and MANIFEST.sha256 from disk")
    sub.add_parser("verify", help="check packages/ against MANIFEST.sha256")

    gc = sub.add_parser("gc", help="retention-based cleanup (dry run unless --apply)")
    gc.add_argument("--keep-last", type=int, default=2, metavar="N",
                    help="recent versions kept per distribution (default: 2)")
    gc.add_argument("--protect", type=Path, action="append", default=[], metavar="REQ",
                    help="requirements file whose '==' pins are immune (repeatable; "
                         "default: locks/protected.txt)")
    gc.add_argument("--apply", action="store_true",
                    help="actually move files (default: dry run)")

    purge = sub.add_parser("purge-trash", help="delete old quarantine batches")
    purge.add_argument("days", type=int, help="delete batches older than DAYS")
    purge.add_argument("--apply", action="store_true",
                       help="actually delete (default: dry run)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "reindex":
        projects, files, unknown = reindex(ROOT)
        print(f"index: {projects} distribution(s), {files} file(s)")
        print(f"manifest: {ROOT / MANIFEST_FILENAME}")
        if unknown:
            print(f"warning: {unknown} unrecognised file(s) under packages/", file=sys.stderr)
        return 0

    if args.command == "verify":
        return 1 if verify(ROOT) else 0

    if args.command == "gc":
        return cmd_gc(ROOT, args)

    if args.command == "purge-trash":
        purged = purge_trash(ROOT, args.days, args.apply)
        verb = "deleted" if args.apply else "would be deleted"
        print(f"{len(purged)} quarantine batch(es) {verb} (older than {args.days} days)")
        for batch in purged:
            print(f"  - {batch.relative_to(ROOT)}")
        if not args.apply and purged:
            print("Re-run with --apply to delete.")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
