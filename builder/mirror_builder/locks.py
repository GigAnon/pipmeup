"""Lockfiles written into the bundle's locks/ directory."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from .resolver import Resolution


def write_lockfiles(bundle_dir: Path, resolutions: list[Resolution],
                    hashes: dict[str, str], roots: list[Requirement],
                    roots_text: str) -> Path:
    """Write per-target locks, build-environment locks, root lists and protected.txt.

    Returns the path of protected.txt (union of every pin, runtime and build).
    """
    locks_dir = bundle_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)

    (locks_dir / "roots.txt").write_text(roots_text, encoding="utf-8")

    union: dict[str, set[Version]] = defaultdict(set)

    for resolution in resolutions:
        ident = resolution.target.identifier

        lines = [
            f"# generated for target {ident}",
            f"# platforms: {', '.join(resolution.target.platforms[:4])}...",
            "",
        ]
        for cname in sorted(resolution.pins):
            version = resolution.pins[cname]
            digest = hashes.get(resolution.artifacts[cname].filename)
            if digest:
                lines.append(f"{cname}=={version} \\\n    --hash=sha256:{digest}")
            else:
                lines.append(f"{cname}=={version}")
        (locks_dir / f"requirements-{ident}.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

        # Root distributions actually present on this target (after markers and
        # availability); used by the install test.
        # Written as requirements (name, extras, specifier; markers dropped) so
        # that constraints such as `setuptools<82` are honoured by the test.
        root_specs = sorted({
            f"{r.name}{'[' + ','.join(sorted(r.extras)) + ']' if r.extras else ''}{r.specifier}"
            for r in roots
            if canonicalize_name(r.name) in resolution.pins
        }, key=str.lower)
        (locks_dir / f"roots-{ident}.txt").write_text(
            "\n".join(root_specs) + "\n", encoding="utf-8")

        # Build environments: informational, one section per sdist.
        build_lines = [f"# build environments for target {ident}",
                       "# sdist filename, its PEP 518 requires, and what they resolved to", ""]
        for filename in sorted(resolution.build_closures):
            closure = resolution.build_closures[filename]
            build_lines.append(f"## {filename}")
            build_lines.append(f"# requires: {', '.join(resolution.build_requires[filename]) or '(none)'}")
            for cname in sorted(closure.pins):
                build_lines.append(f"{cname}=={closure.pins[cname]}")
            build_lines.append("")
        (locks_dir / f"build-{ident}.txt").write_text(
            "\n".join(build_lines) + "\n", encoding="utf-8")

        for cname, version in resolution.all_pins():
            union[cname].add(version)

    protected_lines = [
        "# Union of every pin of every target, build environments included.",
        "# Passed to `mirrortool.py gc --protect` to shield these artifacts from retention.",
        "",
    ]
    for cname in sorted(union):
        for version in sorted(union[cname], reverse=True):
            protected_lines.append(f"{cname}=={version}")

    protected = locks_dir / "protected.txt"
    protected.write_text("\n".join(protected_lines) + "\n", encoding="utf-8")
    return protected
