"""Command line: resolve, download and assemble a bundle."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from .archive import create_archive, human_size, parse_size
from .client import PyPIClient, RemoteFile
from .download import download
from .locks import write_lockfiles
from .resolver import Resolution, Resolver
from .targets import (DEFAULT_GLIBC_MINOR, DEFAULT_MAX_GLIBC_MINOR, DEFAULT_TARGETS,
                      PRESETS, PROFILES, make_targets)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "bundle-template"

_LINE_CONTINUATION = re.compile(r"\\\s*\n")


def read_requirements(path: Path) -> tuple[list[Requirement], str]:
    """Parse the root requirements file. Returns (requirements, raw text)."""
    raw_text = path.read_text(encoding="utf-8")
    text = _LINE_CONTINUATION.sub(" ", raw_text)
    requirements: list[Requirement] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            requirements.append(Requirement(line))
        except InvalidRequirement:
            print(f"warning: ignoring line (invalid requirement): {raw!r}", file=sys.stderr)
    return requirements, raw_text


def _builder_revision() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def assemble_bundle(bundle_dir: Path, wanted: dict[str, RemoteFile],
                    resolutions: list[Resolution], roots: list[Requirement],
                    roots_text: str, hashes: dict[str, str]) -> Path:
    """Copy the template, write locks/meta/bundle.json, rebuild index and manifest.

    After this call the directory is the complete delivery bundle and contains
    nothing that depends on the builder.
    """
    # The bundle tool is the single implementation of index/manifest generation,
    # shared with the builder (dependency direction: builder -> bundle template).
    sys.path.insert(0, str(TEMPLATE_DIR))
    import mirrortool  # noqa: PLC0415

    for item in TEMPLATE_DIR.iterdir():
        if item.is_file() and item.name != "__pycache__":
            shutil.copy2(item, bundle_dir / item.name)

    protected = write_lockfiles(bundle_dir, resolutions, hashes, roots, roots_text)

    meta = mirrortool.load_meta(bundle_dir)
    for remote in wanted.values():
        meta[remote.filename] = {"requires_python": remote.requires_python}
    mirrortool.save_meta(bundle_dir, meta)

    projects, files, unknown = mirrortool.reindex(bundle_dir)
    if unknown:
        print(f"warning: {unknown} unrecognised file(s) under packages/", file=sys.stderr)

    info = {
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "builder_revision": _builder_revision(),
        "targets": [
            {
                "id": r.target.identifier,
                "python": r.target.python_full_version,
                "resolved": len(r.pins),
                "source_only": sorted(r.source_only),
                "unavailable": r.unavailable,
                "newer_glibc": r.newer_glibc,
            }
            for r in resolutions
        ],
        "roots": len(roots),
        "distributions": projects,
        "files": files,
    }
    (bundle_dir / "bundle.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return protected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a self-sufficient offline PyPI mirror bundle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Presets: " + ", ".join(f"{k}={v}" for k, v in PRESETS.items())
                + "\nProfiles: " + ", ".join(sorted(PROFILES))),
    )
    parser.add_argument("-r", "--requirements", type=Path,
                        default=REPO_ROOT / "requirements" / "packages.txt",
                        help="root packages file (default: requirements/packages.txt)")
    parser.add_argument("-o", "--output", type=Path, default=REPO_ROOT / "build" / "bundle",
                        help="bundle directory (default: build/bundle)")
    parser.add_argument("--target", action="append", default=[], metavar="TARGET",
                        help="preset or <profile>:<pyver>[,<pyver>...][:<glibc minor>], "
                             "repeatable; several Python versions in one spec build them "
                             f"together (default: {' '.join(DEFAULT_TARGETS)})")
    parser.add_argument("--glibc", type=int, default=DEFAULT_GLIBC_MINOR, metavar="MINOR",
                        help="preferred glibc 2.MINOR for Linux targets: wheels for it are "
                             f"chosen first (default: {DEFAULT_GLIBC_MINOR}, Ubuntu 20.04)")
    parser.add_argument("--glibc-max", type=int, default=DEFAULT_MAX_GLIBC_MINOR, metavar="MINOR",
                        help="newest glibc 2.MINOR accepted when no preferred wheel exists "
                             f"(default: {DEFAULT_MAX_GLIBC_MINOR}, Debian 13); such wheels are listed")
    parser.add_argument("--with-sdists", action="store_true",
                        help="also fetch the sdist of every wheel-resolved distribution")
    parser.add_argument("--pre", action="store_true", help="allow pre-releases")
    parser.add_argument("--wheels-only", action="store_true",
                        help="forbid the sdist fallback on every target")
    parser.add_argument("--jobs", type=int, default=8, metavar="N",
                        help="parallel downloads (default: 8)")
    parser.add_argument("--cache-dir", type=Path, default=REPO_ROOT / ".cache",
                        help="metadata cache, kept outside the bundle (default: .cache)")
    parser.add_argument("--resolve-only", action="store_true",
                        help="resolve and write locks without downloading")

    group = parser.add_argument_group("archiving")
    group.add_argument("--archive", type=Path, default=None, metavar="PATH",
                       help="produce a tar.gz archive (e.g. build/dist/bundle.tar.gz)")
    group.add_argument("--archive-only", action="store_true",
                       help="archive the existing bundle without resolving anything")
    group.add_argument("--split-size", default=None, metavar="SIZE",
                       help="split the archive (4G, 700M, 1.5GiB, 650MB...)")
    group.add_argument("--split-mode", choices=("volumes", "raw"), default="volumes",
                       help="volumes: independent archives (default); raw: binary split")
    return parser


def _archive(bundle_dir: Path, args: argparse.Namespace, split_size: int | None) -> None:
    destination = args.archive or (bundle_dir.parent / "dist" / f"{bundle_dir.name}.tar.gz")
    parts = create_archive(bundle_dir, destination.resolve(), split_size, args.split_mode)
    total = sum(p.stat().st_size for p in parts)
    label = "independent volume(s)" if args.split_mode == "volumes" else "chunk(s)"
    print(f"\n  archive   : {len(parts)} {label}, {human_size(total)} total")
    for part in parts:
        print(f"      {part.name}  ({human_size(part.stat().st_size)})")
    print(f"      SHA256SUMS and RESTORE.txt in {parts[0].parent}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    split_size = None
    if args.split_size:
        try:
            split_size = parse_size(args.split_size)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if split_size <= 0:
            print("error: --split-size must be strictly positive", file=sys.stderr)
            return 2
        if args.archive is None and not args.archive_only:
            print("error: --split-size has no effect without --archive", file=sys.stderr)
            return 2

    bundle_dir: Path = args.output.resolve()

    if args.archive_only:
        if not bundle_dir.is_dir():
            print(f"error: bundle not found: {bundle_dir}", file=sys.stderr)
            return 2
        _archive(bundle_dir, args, split_size)
        return 0

    if not args.requirements.is_file():
        print(f"error: file not found: {args.requirements}", file=sys.stderr)
        return 2

    try:
        targets = [t for spec in (args.target or DEFAULT_TARGETS)
                   for t in make_targets(spec, args.glibc, args.glibc_max)]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    roots, roots_text = read_requirements(args.requirements)
    if not roots:
        print("error: no usable requirement", file=sys.stderr)
        return 2

    packages_dir = bundle_dir / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    client = PyPIClient(args.cache_dir.resolve())

    print(f"=== Bundle {bundle_dir} ===")
    print(f"  roots   : {len(roots)}")
    print(f"  targets : {', '.join(t.identifier for t in targets)}")

    # -- resolution --------------------------------------------------------

    resolutions: list[Resolution] = []
    wanted: dict[str, RemoteFile] = {}

    for target in targets:
        print(f"\n--- Resolving: {target.identifier} ---")
        resolver = Resolver(client, target, allow_prereleases=args.pre,
                            allow_sdist=not args.wheels_only)
        resolution = resolver.resolve(list(roots))
        resolutions.append(resolution)

        for filename, artifact in resolution.all_artifacts().items():
            wanted.setdefault(filename, artifact)
        if args.with_sdists:
            for cname, artifact in resolution.artifacts.items():
                if artifact.kind != "wheel":
                    continue
                version = resolution.pins[cname]
                for remote in client.files(cname):
                    if remote.kind == "sdist" and remote.version == version and not remote.yanked:
                        wanted.setdefault(remote.filename, remote)

        print(f"  resolved distributions : {len(resolution.pins)}")
        if resolution.build_closures:
            build_files = {f for c in resolution.build_closures.values()
                           for f in (a.filename for a in c.artifacts.values())}
            print(f"  sdists needing a build : {len(resolution.build_closures)} "
                  f"({len(build_files)} build-environment file(s))")
        if resolution.source_only:
            print(f"  built from source      : {', '.join(sorted(resolution.source_only))}")
        if resolution.newer_glibc:
            print(f"  wheels needing glibc > 2.{args.glibc} : {len(resolution.newer_glibc)}")
            for item in resolution.newer_glibc[:20]:
                print(f"      ^ {item}")
        if resolution.unavailable:
            print(f"  unavailable (warnings) : {len(resolution.unavailable)}")
            for item in resolution.unavailable[:20]:
                print(f"      ~ {item}")
        if resolution.problems:
            print(f"  problems               : {len(resolution.problems)}")
            for problem in resolution.problems[:15]:
                print(f"      ! {problem}")
            if len(resolution.problems) > 15:
                print(f"      ... and {len(resolution.problems) - 15} more")

    # -- download ----------------------------------------------------------

    hashes = {f.filename: f.sha256 for f in wanted.values() if f.sha256}
    failures: list[str] = []

    if args.resolve_only:
        print(f"\n--- Resolve only: {len(wanted)} artifact(s) selected ---")
    else:
        print(f"\n--- Downloading: {len(wanted)} artifact(s) ---")
        done = present = 0
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(download, remote, packages_dir): remote.filename
                       for remote in wanted.values()}
            for future in as_completed(futures):
                try:
                    filename, status = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures.append(str(exc))
                    continue
                if status == "present":
                    present += 1
                else:
                    done += 1
                    print(f"  + {filename}")
        print(f"\n  new: {done}   already present: {present}   failures: {len(failures)}")
        for failure in failures:
            print(f"      ! {failure}")

    # -- assemble ----------------------------------------------------------

    protected = assemble_bundle(bundle_dir, wanted, resolutions, roots, roots_text, hashes)
    report = {
        "targets": [
            {
                "id": r.target.identifier,
                "python_full_version": r.target.python_full_version,
                "marker_env": r.target.marker_env,
                "resolved": len(r.pins),
                "source_only": sorted(r.source_only),
                "build_requires": r.build_requires,
                "unavailable": r.unavailable,
                "newer_glibc": r.newer_glibc,
                "problems": r.problems,
            }
            for r in resolutions
        ],
        "artifacts": sorted(wanted),
        "download_failures": failures,
    }
    # The report is builder output, kept next to the bundle rather than in it.
    report_path = bundle_dir.parent / f"{bundle_dir.name}-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  locks     : {protected.parent}")
    print(f"  report    : {report_path}")
    print(f"  Cleanup   : python mirrortool.py gc --keep-last 2   (run from inside the bundle)")

    if args.archive is not None:
        _archive(bundle_dir, args, split_size)

    has_problems = failures or any(r.problems for r in resolutions)
    return 1 if has_problems else 0
