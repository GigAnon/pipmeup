#!/usr/bin/env python3
"""Garbage collector pour un miroir PyPI local (repertoire plat d'artefacts).

Politique de retention :
  - on conserve les N versions les plus recentes de chaque distribution ;
  - pour une version conservee, TOUTES les wheels sont conservees (toutes
    plateformes / tous ABI confondus) ;
  - tout ce qui est reference par un lockfile de protection est immunise ;
  - les pre-releases ne comptent pas dans les N sauf si aucune release stable ;
  - les fichiers non parsables sont signales mais jamais supprimes ;
  - la suppression est en fait une mise en quarantaine dans .trash/<horodatage>/.

Le mode par defaut est un dry-run : rien n'est deplace sans --apply.

Usage typique :
    python mirror_gc.py out --keep-last 2 --protect locks/prod.txt
    python mirror_gc.py out --keep-last 2 --protect locks/prod.txt --apply
    python mirror_gc.py out --purge-trash 30 --apply
    python mirror_gc.py out --manifest out/MANIFEST.sha256
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

try:
    from packaging.utils import (
        InvalidSdistFilename,
        InvalidWheelFilename,
        canonicalize_name,
        parse_sdist_filename,
        parse_wheel_filename,
    )
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover
    sys.exit("packaging est requis : pip install packaging")


ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".zip", ".tar.bz2", ".tar.xz", ".egg")
TRASH_DIRNAME = ".trash"
TRASH_STAMP_FMT = "%Y%m%dT%H%M%S"


# --------------------------------------------------------------------------
# Modele
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str          # nom canonique PEP 503
    version: Version
    kind: str          # 'wheel' | 'sdist'


def _full_suffix(path: Path) -> str:
    """Retourne '.tar.gz' et non '.gz' pour les archives a double extension."""
    for suffix in ARCHIVE_SUFFIXES:
        if path.name.lower().endswith(suffix):
            return suffix
    return path.suffix


def parse_artifact(path: Path) -> Artifact | None:
    """Parse un nom de fichier d'artefact. Retourne None si non reconnu.

    On s'appuie sur packaging plutot que sur une regex maison : les wheels
    avec build tag (foo-1.0-1-py3-none-any.whl) et les versions locales
    (1.0+local.1) cassent les heuristiques naives.
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
        # packaging ne gere pas ces extensions ; fallback prudent.
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


def scan(mirror: Path) -> tuple[list[Artifact], list[Path]]:
    """Retourne (artefacts parses, fichiers non reconnus)."""
    artifacts: list[Artifact] = []
    unknown: list[Path] = []

    for path in sorted(mirror.rglob("*")):
        if not path.is_file():
            continue
        if TRASH_DIRNAME in path.parts:
            continue
        if not path.name.lower().endswith(ARCHIVE_SUFFIXES):
            continue

        parsed = parse_artifact(path)
        if parsed is None:
            unknown.append(path)
        else:
            artifacts.append(parsed)

    return artifacts, unknown


# --------------------------------------------------------------------------
# Protection
# --------------------------------------------------------------------------

_PIN_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:\[[^\]]*\])?\s*=="
    r"\s*(?P<version>[^\s;#\\]+)"
)


def load_protected(paths: list[Path]) -> set[tuple[str, Version]]:
    """Extrait les couples (nom canonique, version) epingles dans des requirements.

    Seules les contraintes '==' sont retenues : une contrainte lache ne
    designe pas un artefact precis et ne peut donc rien immuniser.
    """
    protected: set[tuple[str, Version]] = set()

    for req_path in paths:
        if not req_path.is_file():
            print(f"[!] fichier de protection introuvable : {req_path}", file=sys.stderr)
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


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def select_obsolete(
    artifacts: list[Artifact],
    keep_last: int,
    protected: set[tuple[str, Version]],
) -> tuple[list[Artifact], dict[str, list[Version]]]:
    """Retourne (artefacts a evincer, versions conservees par distribution)."""
    by_name: dict[str, set[Version]] = defaultdict(set)
    for art in artifacts:
        by_name[art.name].add(art.version)

    kept_versions: dict[str, list[Version]] = {}

    for name, versions in by_name.items():
        stable = sorted((v for v in versions if not v.is_prerelease), reverse=True)
        # On ne retombe sur les pre-releases que si aucune version stable
        # n'existe : sinon une alpha recente evincerait la derniere stable.
        pool = stable if stable else sorted(versions, reverse=True)
        keep = set(pool[:keep_last])
        keep |= {v for v in versions if (name, v) in protected}
        kept_versions[name] = sorted(keep, reverse=True)

    obsolete = [
        art for art in artifacts
        if art.version not in kept_versions[art.name]
    ]
    return obsolete, kept_versions


# --------------------------------------------------------------------------
# Quarantaine
# --------------------------------------------------------------------------

def quarantine(mirror: Path, obsolete: list[Artifact], apply: bool) -> Path | None:
    """Deplace les artefacts obsoletes vers .trash/<horodatage>/.

    On ne supprime jamais directement : sur un miroir alimentant un
    environnement isole, une erreur de parsing coute trop cher.
    """
    if not obsolete:
        return None

    stamp = dt.datetime.now().strftime(TRASH_STAMP_FMT)
    trash_dir = mirror / TRASH_DIRNAME / stamp

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


def purge_trash(mirror: Path, older_than_days: int, apply: bool) -> list[Path]:
    """Supprime definitivement les lots de quarantaine trop anciens."""
    trash_root = mirror / TRASH_DIRNAME
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


# --------------------------------------------------------------------------
# Manifeste
# --------------------------------------------------------------------------

def write_manifest(mirror: Path, artifacts: list[Artifact], out_path: Path) -> None:
    """Ecrit un manifeste au format sha256sum, verifiable avec `sha256sum -c`.

    Sert de controle d'integrite apres transfert vers l'environnement isole
    et de trace de provenance des artefacts.
    """
    lines = []
    for art in sorted(artifacts, key=lambda a: a.path.name):
        digest = hashlib.sha256()
        with art.path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        relative = art.path.relative_to(mirror).as_posix()
        lines.append(f"{digest.hexdigest()}  {relative}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Nettoyage par retention d'un miroir PyPI local.",
    )
    parser.add_argument("mirror", type=Path, help="repertoire du miroir")
    parser.add_argument(
        "--keep-last", type=int, default=2, metavar="N",
        help="nombre de versions recentes conservees par distribution (defaut: 2)",
    )
    parser.add_argument(
        "--protect", type=Path, action="append", default=[], metavar="REQ",
        help="requirements.txt dont les epinglages '==' sont immunises (repetable)",
    )
    parser.add_argument(
        "--purge-trash", type=int, default=None, metavar="JOURS",
        help="supprime definitivement les lots de quarantaine plus anciens",
    )
    parser.add_argument(
        "--manifest", type=Path, default=None, metavar="FICHIER",
        help="ecrit un manifeste sha256 des artefacts conserves",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="applique reellement les changements (defaut: dry-run)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    mirror: Path = args.mirror.resolve()
    if not mirror.is_dir():
        print(f"[!] repertoire introuvable : {mirror}", file=sys.stderr)
        return 2

    if args.keep_last < 1:
        print("[!] --keep-last doit valoir au moins 1", file=sys.stderr)
        return 2

    artifacts, unknown = scan(mirror)
    protected = load_protected(args.protect)
    obsolete, kept_versions = select_obsolete(artifacts, args.keep_last, protected)

    obsolete_set = {art.path for art in obsolete}
    retained = [art for art in artifacts if art.path not in obsolete_set]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Miroir {mirror} [{mode}] ===")
    print(f"  distributions   : {len(kept_versions)}")
    print(f"  artefacts       : {len(artifacts)}")
    print(f"  conserves       : {len(retained)}")
    print(f"  obsoletes       : {len(obsolete)}")
    print(f"  non reconnus    : {len(unknown)}")
    print(f"  epinglages prot.: {len(protected)}")

    if unknown:
        print("\n--- Fichiers non reconnus (jamais supprimes, a inspecter) ---")
        for path in unknown:
            print(f"  ? {path.relative_to(mirror)}")

    if obsolete:
        print("\n--- Artefacts evinces ---")
        by_name: dict[str, list[Artifact]] = defaultdict(list)
        for art in obsolete:
            by_name[art.name].append(art)
        for name in sorted(by_name):
            kept = ", ".join(str(v) for v in kept_versions[name]) or "(aucune)"
            print(f"  {name}  [conserve: {kept}]")
            for art in sorted(by_name[name], key=lambda a: a.path.name):
                print(f"      - {art.path.relative_to(mirror)}")

        trash_dir = quarantine(mirror, obsolete, args.apply)
        verb = "deplaces vers" if args.apply else "seraient deplaces vers"
        print(f"\n  {len(obsolete)} artefact(s) {verb} {trash_dir}")

    if args.purge_trash is not None:
        purged = purge_trash(mirror, args.purge_trash, args.apply)
        verb = "supprime(s)" if args.apply else "seraient supprime(s)"
        print(f"\n  {len(purged)} lot(s) de quarantaine {verb} "
              f"(> {args.purge_trash} jours)")
        for batch in purged:
            print(f"      - {batch.relative_to(mirror)}")

    if args.manifest is not None:
        if args.apply:
            write_manifest(mirror, retained, args.manifest.resolve())
            print(f"\n  manifeste ecrit : {args.manifest}")
        else:
            print(f"\n  manifeste non ecrit (dry-run) : {args.manifest}")

    if not args.apply and (obsolete or args.purge_trash is not None):
        print("\n  Relancer avec --apply pour appliquer.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
