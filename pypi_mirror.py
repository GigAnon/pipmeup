#!/usr/bin/env python3
"""Constitution d'un miroir PyPI local, multi-plateformes, sans conteneur.

Pourquoi ne pas utiliser `pip download --platform` :
    Les options --platform / --python-version / --abi de pip n'influencent que
    la selection des *tags de wheel*. Les marqueurs d'environnement autres que
    python_version (sys_platform, platform_system, platform_machine, os_name,
    platform_python_implementation) restent evalues contre l'interpreteur qui
    execute pip. Consequence : depuis Linux, une dependance conditionnelle
    telle que `pywin32 ; sys_platform == "win32"` est silencieusement omise du
    telechargement destine a Windows.

Ce script resout donc lui-meme le graphe de dependances, une fois par cible,
contre un environnement de marqueurs *synthetique* decrivant la cible :

    1. listing des artefacts via l'API simple JSON de PyPI (PEP 691) ;
    2. metadonnees par wheel via les fichiers .metadata (PEP 658), ce qui
       capture les Requires-Dist propres a chaque wheel -- ils peuvent
       differer d'une plateforme a l'autre pour une meme version ;
    3. evaluation des marqueurs avec packaging.markers contre la cible ;
    4. selection de wheel par compatibilite de tags calculee avec
       packaging.tags pour la cible (et non pour la machine locale) ;
    5. telechargement avec verification sha256, puis emission de lockfiles.

Limite assumee : la resolution est gloutonne (derniere version satisfaisant
les contraintes accumulees), sans backtracking. Pour un miroir c'est le bon
compromis -- sur-inclure est benin, sous-inclure ne l'est pas -- mais les
conflits de contraintes sont signales dans le rapport plutot que resolus.

Usage :
    python pypi_mirror.py -r packages.txt -o out
    python pypi_mirror.py -r packages.txt -o out --target win_amd64:3.13 \
                                                  --target linux_x86_64:3.13
    python pypi_mirror.py -r packages.txt -o out --with-sdists --jobs 8
"""

from __future__ import annotations

import argparse
import email.parser
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from packaging.markers import Marker, UndefinedEnvironmentName
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms
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


INDEX_URL = "https://pypi.org/simple"
USER_AGENT = "pypi-mirror/1.0 (+local mirror builder)"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
CACHE_TTL_SECONDS = 24 * 3600
MAX_RESOLVER_STEPS = 50_000


# ==========================================================================
# Cibles
# ==========================================================================

# manylinux_2_5 / _2_12 / _2_17 ont des alias historiques, uniquement definis
# pour certaines architectures.
_GLIBC_ALIASES = {5: "manylinux1", 12: "manylinux2010", 17: "manylinux2014"}
_ALIAS_ARCHES = {
    5: {"x86_64", "i686"},
    12: {"x86_64", "i686"},
    17: {"x86_64", "i686", "aarch64", "ppc64le", "s390x", "armv7l"},
}


def _linux_platforms(arch: str, max_glibc_minor: int) -> list[str]:
    """Tags plateforme acceptes par une cible glibc, du plus recent au plus ancien.

    pip privilegie le manylinux le plus recent supporte par le systeme ; on
    reproduit cet ordre pour que le classement des wheels soit fidele.
    """
    platforms: list[str] = []
    for minor in range(max_glibc_minor, 4, -1):
        platforms.append(f"manylinux_2_{minor}_{arch}")
        if minor in _GLIBC_ALIASES and arch in _ALIAS_ARCHES[minor]:
            platforms.append(f"{_GLIBC_ALIASES[minor]}_{arch}")
    platforms.append(f"linux_{arch}")
    return platforms


@dataclass(frozen=True)
class Target:
    """Une cible de miroir : plateforme + version d'interpreteur."""

    identifier: str
    python_version: tuple[int, int]
    python_full_version: str
    platforms: tuple[str, ...]
    marker_env: dict[str, str]
    tag_rank: dict[Tag, int] = field(compare=False, repr=False, default_factory=dict)

    @property
    def python_tag(self) -> str:
        return f"cp{self.python_version[0]}{self.python_version[1]}"


def _build_marker_env(profile: str, machine: str, full_version: str) -> dict[str, str]:
    """Environnement de marqueurs synthetique (PEP 508) pour la cible."""
    major_minor = ".".join(full_version.split(".")[:2])

    if profile.startswith("win"):
        system, sys_platform, os_name = "Windows", "win32", "nt"
    elif profile.startswith("macos"):
        system, sys_platform, os_name = "Darwin", "darwin", "posix"
    else:
        system, sys_platform, os_name = "Linux", "linux", "posix"

    return {
        "implementation_name": "cpython",
        "implementation_version": full_version,
        "os_name": os_name,
        "platform_machine": machine,
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": system,
        "platform_version": "",
        "python_full_version": full_version,
        "python_version": major_minor,
        "sys_platform": sys_platform,
    }


PROFILES: dict[str, dict[str, Any]] = {
    "win_amd64":    {"machine": "AMD64",  "platforms": ("win_amd64",)},
    "win32":        {"machine": "x86",    "platforms": ("win32",)},
    "win_arm64":    {"machine": "ARM64",  "platforms": ("win_arm64",)},
    "linux_x86_64": {"machine": "x86_64", "linux_arch": "x86_64"},
    "linux_i686":   {"machine": "i686",   "linux_arch": "i686"},
    "linux_aarch64": {"machine": "aarch64", "linux_arch": "aarch64"},
    "macos_arm64":  {"machine": "arm64",  "mac_arch": "arm64"},
    "macos_x86_64": {"machine": "x86_64", "mac_arch": "x86_64"},
}

DEFAULT_TARGETS = ["win_amd64:3.13", "linux_x86_64:3.13"]


def make_target(spec: str, glibc_minor: int, macos_version: tuple[int, int]) -> Target:
    """Construit une cible a partir d'une specification '<profil>:<pyversion>'."""
    if ":" not in spec:
        raise ValueError(f"cible invalide '{spec}' (attendu '<profil>:<pyversion>')")

    profile, _, raw_version = spec.partition(":")
    if profile not in PROFILES:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"profil inconnu '{profile}' (connus : {known})")

    parts = raw_version.split(".")
    if len(parts) < 2:
        raise ValueError(f"version Python invalide '{raw_version}' (attendu 3.13 ou 3.13.2)")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"version Python invalide '{raw_version}'") from exc

    # Le patch compte : un marqueur `python_full_version >= "3.8.1"` exclut
    # 3.8.0. Faute de precision, on suppose .0 (hypothese conservatrice).
    full_version = raw_version if len(parts) >= 3 else f"{major}.{minor}.0"

    conf = PROFILES[profile]
    if "linux_arch" in conf:
        platforms = tuple(_linux_platforms(conf["linux_arch"], glibc_minor))
    elif "mac_arch" in conf:
        platforms = tuple(mac_platforms(macos_version, conf["mac_arch"]))
    else:
        platforms = tuple(conf["platforms"])

    python_version = (major, minor)
    ordered = list(cpython_tags(python_version=python_version, platforms=platforms))
    ordered += list(
        compatible_tags(
            python_version=python_version,
            interpreter=f"cp{major}{minor}",
            platforms=platforms,
        )
    )
    tag_rank = {}
    for rank, tag in enumerate(ordered):
        tag_rank.setdefault(tag, rank)

    return Target(
        identifier=f"{profile}-py{major}.{minor}",
        python_version=python_version,
        python_full_version=full_version,
        platforms=platforms,
        marker_env=_build_marker_env(profile, conf["machine"], full_version),
        tag_rank=tag_rank,
    )


# ==========================================================================
# Client PyPI
# ==========================================================================

@dataclass
class RemoteFile:
    filename: str
    url: str
    sha256: str | None
    requires_python: str | None
    yanked: bool
    has_core_metadata: bool
    version: Version
    kind: str            # 'wheel' | 'sdist'
    tags: frozenset[Tag]


class PackageNotFound(Exception):
    pass


class PyPIClient:
    """Acces a l'index avec cache disque, pour rendre les reruns quasi gratuits."""

    def __init__(self, cache_dir: Path, index_url: str = INDEX_URL,
                 ttl: int = CACHE_TTL_SECONDS, timeout: int = 60) -> None:
        self.cache_dir = cache_dir
        self.index_url = index_url.rstrip("/")
        self.ttl = ttl
        self.timeout = timeout
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._simple_cache: dict[str, list[RemoteFile]] = {}
        self._meta_cache: dict[str, list[str]] = {}

    # -- transport ---------------------------------------------------------

    def _fetch(self, url: str, accept: str | None = None, retries: int = 3) -> bytes:
        headers = {"User-Agent": USER_AGENT}
        if accept:
            headers["Accept"] = accept
        request = urllib.request.Request(url, headers=headers)

        last: Exception | None = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            time.sleep(2 ** attempt)
        raise RuntimeError(f"echec du telechargement de {url} : {last}")

    def _cached(self, url: str, accept: str | None = None) -> bytes:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        path = self.cache_dir / key
        if path.is_file() and (time.time() - path.stat().st_mtime) < self.ttl:
            return path.read_bytes()
        data = self._fetch(url, accept=accept)
        path.write_bytes(data)
        return data

    # -- API ---------------------------------------------------------------

    def files(self, name: str) -> list[RemoteFile]:
        """Artefacts disponibles pour une distribution (API simple, PEP 691)."""
        cname = canonicalize_name(name)
        if cname in self._simple_cache:
            return self._simple_cache[cname]

        url = f"{self.index_url}/{cname}/"
        try:
            payload = json.loads(self._cached(url, accept=SIMPLE_ACCEPT))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise PackageNotFound(cname) from exc
            raise

        result: list[RemoteFile] = []
        for entry in payload.get("files", []):
            filename = entry["filename"]
            parsed = _parse_remote_filename(filename)
            if parsed is None:
                continue
            version, kind, tags = parsed
            result.append(
                RemoteFile(
                    filename=filename,
                    url=entry["url"].split("#", 1)[0],
                    sha256=(entry.get("hashes") or {}).get("sha256"),
                    requires_python=entry.get("requires-python"),
                    yanked=bool(entry.get("yanked")),
                    has_core_metadata=bool(entry.get("core-metadata")
                                           or entry.get("data-dist-info-metadata")),
                    version=version,
                    kind=kind,
                    tags=tags,
                )
            )

        self._simple_cache[cname] = result
        return result

    def requires_dist(self, remote: RemoteFile) -> list[str]:
        """Requires-Dist de l'artefact.

        On privilegie le .metadata PEP 658 attache a *ce* fichier : les
        dependances peuvent differer entre deux wheels d'une meme version.
        Repli sur l'API JSON par version quand il est absent (sdists,
        anciennes publications).
        """
        if remote.filename in self._meta_cache:
            return self._meta_cache[remote.filename]

        deps: list[str] = []
        if remote.has_core_metadata:
            try:
                raw = self._cached(remote.url + ".metadata").decode("utf-8", "replace")
                message = email.parser.Parser().parsestr(raw)
                deps = list(message.get_all("Requires-Dist") or [])
            except (urllib.error.HTTPError, RuntimeError, UnicodeDecodeError):
                deps = []

        if not deps and not remote.has_core_metadata:
            deps = self._requires_dist_via_json(remote)

        self._meta_cache[remote.filename] = deps
        return deps

    def _requires_dist_via_json(self, remote: RemoteFile) -> list[str]:
        try:
            name = canonicalize_name(remote.filename.split("-")[0])
            url = f"https://pypi.org/pypi/{name}/{remote.version}/json"
            payload = json.loads(self._cached(url))
        except (urllib.error.HTTPError, RuntimeError, json.JSONDecodeError, KeyError):
            return []
        return list(payload.get("info", {}).get("requires_dist") or [])


def _parse_remote_filename(filename: str) -> tuple[Version, str, frozenset[Tag]] | None:
    lowered = filename.lower()
    if lowered.endswith(".whl"):
        try:
            _name, version, _build, tags = parse_wheel_filename(filename)
        except (InvalidWheelFilename, InvalidVersion):
            return None
        return version, "wheel", frozenset(tags)
    if lowered.endswith((".tar.gz", ".zip")):
        try:
            _name, version = parse_sdist_filename(filename)
        except (InvalidSdistFilename, InvalidVersion):
            return None
        return version, "sdist", frozenset()
    return None


# ==========================================================================
# Resolution
# ==========================================================================

@dataclass
class Resolution:
    target: Target
    pins: dict[str, Version] = field(default_factory=dict)
    artifacts: dict[str, RemoteFile] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    # Distributions retenues via un sdist faute de wheel : elles exigeront une
    # chaine de compilation sur la cible, ce qui est rarement acceptable en
    # environnement isole. A traiter explicitement.
    source_only: list[str] = field(default_factory=list)


def _marker_ok(marker: Marker | None, env: dict[str, str], extras: Iterable[str]) -> bool:
    """Un marqueur est satisfait s'il l'est hors extra, ou pour un extra demande."""
    if marker is None:
        return True
    candidates = [""] + sorted(extras)
    for extra in candidates:
        try:
            if marker.evaluate({**env, "extra": extra}):
                return True
        except UndefinedEnvironmentName:
            continue
    return False


def _requires_python_ok(spec: str | None, full_version: str) -> bool:
    if not spec:
        return True
    try:
        return SpecifierSet(spec).contains(full_version, prereleases=True)
    except InvalidSpecifier:
        return True


def _best_wheel(files: list[RemoteFile], target: Target) -> RemoteFile | None:
    """Meilleure wheel pour la cible, selon le classement des tags de la cible."""
    best: tuple[int, RemoteFile] | None = None
    for remote in files:
        if remote.kind != "wheel" or remote.yanked:
            continue
        ranks = [target.tag_rank[t] for t in remote.tags if t in target.tag_rank]
        if not ranks:
            continue
        rank = min(ranks)
        if best is None or rank < best[0]:
            best = (rank, remote)
    return best[1] if best else None


class Resolver:
    def __init__(self, client: PyPIClient, target: Target,
                 allow_prereleases: bool = False, allow_sdist: bool = True) -> None:
        self.client = client
        self.target = target
        self.allow_prereleases = allow_prereleases
        self.allow_sdist = allow_sdist
        self._by_version: dict[str, dict[Version, list[RemoteFile]]] = {}

    def _versions(self, cname: str) -> dict[Version, list[RemoteFile]]:
        if cname not in self._by_version:
            grouped: dict[Version, list[RemoteFile]] = defaultdict(list)
            for remote in self.client.files(cname):
                grouped[remote.version].append(remote)
            self._by_version[cname] = dict(grouped)
        return self._by_version[cname]

    def _select(self, cname: str, constraint: SpecifierSet) -> tuple[Version, RemoteFile] | None:
        grouped = self._versions(cname)
        allow_pre = self.allow_prereleases or bool(constraint.prereleases)

        ordered = sorted(grouped, reverse=True)
        stable = [v for v in ordered if not v.is_prerelease]
        pool = ordered if allow_pre or not stable else stable

        for version in pool:
            if not constraint.contains(version, prereleases=True):
                continue
            files = grouped[version]
            # requires-python est porte par chaque fichier ; il suffit qu'un
            # artefact utilisable soit compatible avec la cible.
            usable = [
                f for f in files
                if _requires_python_ok(f.requires_python, self.target.python_full_version)
            ]
            if not usable:
                continue
            wheel = _best_wheel(usable, self.target)
            if wheel is not None:
                return version, wheel

            # Repli sdist : de nombreuses distributions anciennes ou de niche
            # (crcmod, netifaces, ordereddict...) ne publient aucune wheel.
            # Le sdist est neutre vis-a-vis de la plateforme, donc valide pour
            # toute cible -- au prix d'une compilation a l'installation.
            if self.allow_sdist:
                sdists = [f for f in usable if f.kind == "sdist" and not f.yanked]
                if sdists:
                    return version, sdists[0]
        return None

    def resolve(self, roots: list[Requirement]) -> Resolution:
        result = Resolution(target=self.target)
        constraints: dict[str, SpecifierSet] = defaultdict(SpecifierSet)
        extras: dict[str, set[str]] = defaultdict(set)

        queue: deque[Requirement] = deque(roots)
        steps = 0

        while queue:
            steps += 1
            if steps > MAX_RESOLVER_STEPS:
                result.problems.append("resolution interrompue : trop d'iterations")
                break

            requirement = queue.popleft()
            cname = canonicalize_name(requirement.name)

            merged = constraints[cname] & requirement.specifier
            new_extras = set(requirement.extras) - extras[cname]
            constraint_changed = merged != constraints[cname]
            constraints[cname] = merged
            extras[cname] |= set(requirement.extras)

            current = result.pins.get(cname)
            needs_select = (
                current is None
                or (constraint_changed and not merged.contains(current, prereleases=True))
            )

            if not needs_select and not new_extras:
                continue

            if needs_select:
                try:
                    selection = self._select(cname, merged)
                except PackageNotFound:
                    result.problems.append(f"{cname}: introuvable sur l'index")
                    continue
                except RuntimeError as exc:
                    result.problems.append(f"{cname}: {exc}")
                    continue

                if selection is None:
                    result.problems.append(
                        f"{cname}{merged}: aucune wheel compatible "
                        f"({self.target.identifier})"
                    )
                    continue
                version, artifact = selection
                result.pins[cname] = version
                result.artifacts[cname] = artifact
                if artifact.kind == "sdist" and cname not in result.source_only:
                    result.source_only.append(cname)
                explore_extras = extras[cname]
            else:
                artifact = result.artifacts[cname]
                explore_extras = new_extras

            for raw in self.client.requires_dist(artifact):
                try:
                    dependency = Requirement(raw)
                except InvalidRequirement:
                    result.problems.append(f"{cname}: Requires-Dist illisible '{raw}'")
                    continue
                if _marker_ok(dependency.marker, self.target.marker_env, explore_extras):
                    queue.append(dependency)

        return result


# ==========================================================================
# Telechargement
# ==========================================================================

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(remote: RemoteFile, dest_dir: Path, timeout: int = 120) -> tuple[str, str]:
    """Telecharge un artefact avec verification d'integrite. Idempotent."""
    target_path = dest_dir / remote.filename

    if target_path.is_file():
        if remote.sha256 is None or _sha256(target_path) == remote.sha256:
            return remote.filename, "present"
        target_path.unlink()

    temporary = target_path.with_suffix(target_path.suffix + ".part")
    request = urllib.request.Request(remote.url, headers={"User-Agent": USER_AGENT})

    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                with temporary.open("wb") as handle:
                    while chunk := response.read(1 << 20):
                        handle.write(chunk)
            if remote.sha256 and _sha256(temporary) != remote.sha256:
                raise RuntimeError("sha256 non conforme")
            # Renommage final : un artefact visible est un artefact complet
            # et verifie, ce qui rend le miroir sur pour le GC.
            temporary.replace(target_path)
            return remote.filename, "telecharge"
        except Exception as exc:  # noqa: BLE001 - on retente puis on remonte
            last = exc
            temporary.unlink(missing_ok=True)
            time.sleep(2 ** attempt)

    raise RuntimeError(f"{remote.filename}: {last}")


# ==========================================================================
# Entrees / sorties
# ==========================================================================

_LINE_CONTINUATION = re.compile(r"\\\s*\n")


def read_requirements(path: Path) -> list[Requirement]:
    text = _LINE_CONTINUATION.sub(" ", path.read_text(encoding="utf-8"))
    requirements: list[Requirement] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            requirements.append(Requirement(line))
        except InvalidRequirement:
            print(f"[!] ligne ignoree (requirement invalide) : {raw!r}", file=sys.stderr)
    return requirements


def write_lockfiles(out_dir: Path, resolutions: list[Resolution],
                    hashes: dict[str, str]) -> Path:
    """Un lock par cible, plus un lock d'union servant de liste de protection."""
    locks_dir = out_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)

    union: dict[str, set[Version]] = defaultdict(set)

    for resolution in resolutions:
        lines = [
            f"# genere pour la cible {resolution.target.identifier}",
            f"# plateformes : {', '.join(resolution.target.platforms[:4])}...",
            "",
        ]
        for cname in sorted(resolution.pins):
            version = resolution.pins[cname]
            union[cname].add(version)
            digest = hashes.get(resolution.artifacts[cname].filename)
            if digest:
                lines.append(f"{cname}=={version} \\\n    --hash=sha256:{digest}")
            else:
                lines.append(f"{cname}=={version}")
        path = locks_dir / f"requirements-{resolution.target.identifier}.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    protected_lines = [
        "# Union de toutes les cibles. A passer a mirror_gc.py --protect",
        "# pour immuniser ces artefacts contre la retention.",
        "",
    ]
    for cname in sorted(union):
        for version in sorted(union[cname], reverse=True):
            protected_lines.append(f"{cname}=={version}")

    protected = locks_dir / "protected.txt"
    protected.write_text("\n".join(protected_lines) + "\n", encoding="utf-8")
    return protected


# ==========================================================================
# Index PEP 503 / manifeste
# ==========================================================================

ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".zip", ".tar.bz2")
# Sous-repertoires de service, jamais consideres comme des artefacts.
RESERVED_DIRS = {".cache", ".trash", "simple", "locks", "dist"}
META_SIDECAR = ".artifact-meta.json"


def parse_local_artifact(filename: str) -> tuple[str, Version, str] | None:
    """(nom canonique, version, type) depuis un nom de fichier d'artefact."""
    lowered = filename.lower()
    if lowered.endswith(".whl"):
        try:
            name, version, _build, _tags = parse_wheel_filename(filename)
        except (InvalidWheelFilename, InvalidVersion):
            return None
        return canonicalize_name(name), version, "wheel"
    if lowered.endswith((".tar.gz", ".zip")):
        try:
            name, version = parse_sdist_filename(filename)
        except (InvalidSdistFilename, InvalidVersion):
            return None
        return canonicalize_name(name), version, "sdist"
    return None


def iter_artifacts(out_dir: Path) -> Iterable[Path]:
    """Artefacts du miroir, hors repertoires de service et fichiers partiels."""
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        relative = path.relative_to(out_dir)
        if relative.parts[0] in RESERVED_DIRS:
            continue
        if path.name.lower().endswith(ARCHIVE_SUFFIXES):
            yield path


def load_sidecar(out_dir: Path) -> dict[str, dict[str, Any]]:
    """Metadonnees conservees entre runs (sha256, requires-python par fichier)."""
    path = out_dir / META_SIDECAR
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def update_sidecar(out_dir: Path, files: Iterable[RemoteFile]) -> dict[str, dict[str, Any]]:
    """Fusionne les metadonnees du run courant avec celles des runs precedents.

    On accumule plutot qu'on ecrase : un artefact telecharge lors d'un run
    anterieur doit conserver son requires-python pour l'index, meme s'il n'a
    pas ete revisite cette fois-ci.
    """
    sidecar = load_sidecar(out_dir)
    for remote in files:
        sidecar[remote.filename] = {
            "sha256": remote.sha256,
            "requires_python": remote.requires_python,
        }
    (out_dir / META_SIDECAR).write_text(
        json.dumps(sidecar, indent=1, sort_keys=True), encoding="utf-8"
    )
    return sidecar


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_simple_index(out_dir: Path, sidecar: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """Genere un index statique PEP 503 dans <out>/simple/.

    Consommable directement par pip via une URL file://, ce qui est plus
    fidele que --find-links : pip applique alors sa logique d'index normale
    (requires-python, selection de version, verification de hash).
    """
    simple_dir = out_dir / "simple"
    if simple_dir.exists():
        shutil.rmtree(simple_dir)
    simple_dir.mkdir(parents=True)

    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in iter_artifacts(out_dir):
        parsed = parse_local_artifact(path.name)
        if parsed is None:
            continue
        grouped[parsed[0]].append(path)

    total_files = 0
    for cname, paths in sorted(grouped.items()):
        project_dir = simple_dir / cname
        project_dir.mkdir(parents=True, exist_ok=True)

        rows = []
        for path in sorted(paths, key=lambda p: p.name):
            meta = sidecar.get(path.name, {})
            digest = meta.get("sha256") or _sha256_of(path)
            href = os.path.relpath(path, project_dir).replace(os.sep, "/")
            attributes = ""
            requires_python = meta.get("requires_python")
            if requires_python:
                # PEP 503 : l'attribut doit etre encode HTML (les '>' des
                # specificateurs casseraient le balisage sinon).
                attributes = (
                    f' data-requires-python="{html.escape(requires_python, quote=True)}"'
                )
            rows.append(
                f'    <a href="{html.escape(href, quote=True)}#sha256={digest}"'
                f'{attributes}>{html.escape(path.name)}</a><br/>'
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
        f'    <a href="{name}/">{html.escape(name)}</a><br/>' for name in sorted(grouped)
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


def write_manifest(out_dir: Path, sidecar: dict[str, dict[str, Any]]) -> Path:
    """Manifeste sha256 verifiable par `sha256sum -c`, pour controle apres transfert."""
    lines = []
    for path in iter_artifacts(out_dir):
        digest = (sidecar.get(path.name) or {}).get("sha256") or _sha256_of(path)
        relative = path.relative_to(out_dir).as_posix()
        lines.append(f"{digest}  {relative}")
    manifest = out_dir / "MANIFEST.sha256"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


# ==========================================================================
# Archivage et decoupage
# ==========================================================================

_SIZE_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?)(?P<base>i?B?)\s*$",
                      re.IGNORECASE)
_SIZE_FACTORS = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}


def parse_size(text: str) -> int:
    """'4G' / '700MiB' -> octets. K/M/G/T = 1024^n ; KB/MB/GB = 1000^n."""
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"taille invalide '{text}' (attendu 4G, 700M, 1.5GiB, 650MB...)")
    unit = match.group("unit").upper()
    base = 1000 if match.group("base").lower() == "b" else 1024
    return int(float(match.group("value")) * base ** _SIZE_FACTORS[unit])


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if value < 1024 or unit == "Tio":
            return f"{value:.1f} {unit}" if unit != "o" else f"{int(value)} o"
        value /= 1024
    return f"{value:.1f} Tio"


def _archive_members(out_dir: Path) -> list[tuple[Path, str]]:
    """(chemin absolu, nom dans l'archive), ordonne pour la reproductibilite.

    Le cache et la quarantaine sont exclus : ce sont des donnees de travail
    locales, sans valeur de l'autre cote de la coupure reseau.
    """
    members: list[tuple[Path, str]] = []
    prefix = out_dir.name
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        relative = path.relative_to(out_dir)
        if relative.parts[0] in {".cache", ".trash", "dist"}:
            continue
        members.append((path, f"{prefix}/{relative.as_posix()}"))
    return members


def _normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Metadonnees deterministes : deux runs identiques donnent le meme octet."""
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    info.mode = 0o644 if not info.isdir() else 0o755
    return info


def create_volumes(members: list[tuple[Path, str]], dest_prefix: Path,
                   limit: int | None) -> list[Path]:
    """Archives tar.gz independantes, chacune sous la limite si possible.

    Chaque volume est extractible seul : un support illisible ne coute que
    les paquets qu'il portait, au lieu de rendre l'ensemble inexploitable.
    """
    parts: list[Path] = []
    index = 0
    oversized: list[str] = []

    def open_volume() -> tuple[Any, Any, Any, Path]:
        nonlocal index
        index += 1
        path = (dest_prefix.parent /
                (f"{dest_prefix.name}.part{index:03d}.tar.gz" if limit
                 else f"{dest_prefix.name}.tar.gz"))
        raw = path.open("wb")
        gz = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0)
        return raw, gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT), path

    raw, gz, tar, path = open_volume()
    written = 0

    for source, arcname in members:
        size = source.stat().st_size
        if limit and written > 0 and (written + size) > limit:
            tar.close(); gz.close(); raw.close()
            parts.append(path)
            raw, gz, tar, path = open_volume()
            written = 0

        tar.add(source, arcname=arcname, filter=_normalise)
        # Flush reel pour connaitre la taille compressee courante : les wheels
        # etant deja des zip, le cout en taux de compression est negligeable.
        gz.flush()
        raw.flush()
        written = raw.tell()

        if limit and written > limit and size > limit:
            oversized.append(arcname)

    tar.close(); gz.close(); raw.close()
    parts.append(path)

    for name in oversized:
        print(f"  [!] {name} depasse a lui seul la limite de volume", file=sys.stderr)
    return parts


def create_raw_split(members: list[tuple[Path, str]], dest_prefix: Path,
                     limit: int | None) -> list[Path]:
    """Archive unique, puis decoupage binaire en morceaux de taille fixe."""
    whole = dest_prefix.parent / f"{dest_prefix.name}.tar.gz"
    with whole.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for source, arcname in members:
                    tar.add(source, arcname=arcname, filter=_normalise)

    if not limit or whole.stat().st_size <= limit:
        return [whole]

    parts: list[Path] = []
    with whole.open("rb") as handle:
        index = 0
        while True:
            chunk = handle.read(limit)
            if not chunk:
                break
            part = dest_prefix.parent / f"{whole.name}.{index:03d}"
            part.write_bytes(chunk)
            parts.append(part)
            index += 1
    whole.unlink()
    return parts


def create_archive(out_dir: Path, dest: Path, split_size: int | None,
                   mode: str) -> list[Path]:
    """Produit l'archive (ou les morceaux) et les fichiers de restitution."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefix = dest.parent / dest.name.removesuffix(".tar.gz").removesuffix(".tgz")

    members = _archive_members(out_dir)
    if mode == "volumes":
        parts = create_volumes(members, prefix, split_size)
    else:
        parts = create_raw_split(members, prefix, split_size)

    sums = dest.parent / "SHA256SUMS"
    sums.write_text(
        "\n".join(f"{_sha256_of(p)}  {p.name}" for p in parts) + "\n",
        encoding="utf-8",
    )

    if mode == "volumes":
        restitution = (
            "Volumes independants : chacun s'extrait seul, dans ce meme repertoire.\n"
            "L'ordre n'a pas d'importance ; les extraire tous reconstitue le miroir.\n\n"
            "  sha256sum -c SHA256SUMS\n"
            f"  for v in {prefix.name}.part*.tar.gz; do tar xzf \"$v\"; done\n"
        )
    else:
        restitution = (
            "Decoupage binaire : TOUS les morceaux sont necessaires, dans l'ordre.\n\n"
            "  sha256sum -c SHA256SUMS\n"
            f"  cat {prefix.name}.tar.gz.* > {prefix.name}.tar.gz\n"
            f"  tar xzf {prefix.name}.tar.gz\n"
        )

    # Les chemins du manifeste sont relatifs a la racine du miroir : la
    # verification doit imperativement s'executer depuis ce repertoire.
    (dest.parent / "RESTORE.txt").write_text(
        restitution
        + "\nPuis, cote isole :\n"
        f"  (cd {out_dir.name} && sha256sum -c MANIFEST.sha256)\n"
        f"  pip install --no-index \\\n"
        f"      --index-url file://$(pwd)/{out_dir.name}/simple <paquet>\n",
        encoding="utf-8",
    )
    return parts


# ==========================================================================
# CLI
# ==========================================================================

def finalise(out_dir: Path, args: argparse.Namespace, split_size: int | None,
             resolutions: list[Resolution] | None = None,
             protected: Path | None = None) -> int:
    """Index PEP 503, manifeste, archivage. Utilisable sans reseau."""
    sidecar = load_sidecar(out_dir)

    if not args.no_index:
        projects, files = build_simple_index(out_dir, sidecar)
        print(f"\n  index     : {out_dir / 'simple'} "
              f"({projects} distribution(s), {files} fichier(s))")

    manifest = write_manifest(out_dir, sidecar)
    print(f"  manifeste : {manifest}")

    if args.archive is not None or args.archive_only:
        destination = args.archive or (out_dir.parent / "dist" / f"{out_dir.name}.tar.gz")
        parts = create_archive(out_dir, destination.resolve(), split_size, args.split_mode)
        total = sum(p.stat().st_size for p in parts)
        label = "volume(s) independant(s)" if args.split_mode == "volumes" else "morceau(x)"
        print(f"\n  archive   : {len(parts)} {label}, {human_size(total)} au total")
        for part in parts:
            print(f"      {part.name}  ({human_size(part.stat().st_size)})")
        print(f"      SHA256SUMS et RESTORE.txt dans {parts[0].parent}")

    if protected is not None:
        print(f"\n  Nettoyage : python mirror_gc.py {out_dir} "
              f"--keep-last 2 --protect {protected}")
        print(f"  Reindexer : python {Path(sys.argv[0]).name} -o {out_dir} --index-only")
    print(f"  Install   : pip install --no-index "
          f"--index-url file://{out_dir / 'simple'} <paquet>")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Miroir PyPI local multi-plateformes, sans conteneur.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Profils disponibles : " + ", ".join(sorted(PROFILES)),
    )
    parser.add_argument("-r", "--requirements", type=Path, default=None,
                        help="fichier listant les paquets racines "
                             "(inutile avec --index-only / --archive-only)")
    parser.add_argument("-o", "--output", type=Path, default=Path("out"),
                        help="repertoire du miroir (defaut: out)")
    parser.add_argument("--target", action="append", default=[], metavar="PROFIL:PYVER",
                        help=f"cible, repetable (defaut: {' '.join(DEFAULT_TARGETS)})")
    parser.add_argument("--glibc", type=int, default=34, metavar="MINOR",
                        help="glibc 2.MINOR max pour les cibles Linux (defaut: 34)")
    parser.add_argument("--macos", default="14.0", metavar="VER",
                        help="version macOS max pour les cibles macOS (defaut: 14.0)")
    parser.add_argument("--with-sdists", action="store_true",
                        help="recupere aussi les sdists (archivage des sources)")
    parser.add_argument("--pre", action="store_true",
                        help="autorise les pre-releases")
    parser.add_argument("--wheels-only", action="store_true",
                        help="refuse le repli sdist : signale les distributions "
                             "sans wheel au lieu de les retenir")
    parser.add_argument("--jobs", type=int, default=8, metavar="N",
                        help="telechargements paralleles (defaut: 8)")
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="cache des metadonnees (defaut: <output>/.cache)")
    parser.add_argument("--resolve-only", action="store_true",
                        help="resout et ecrit les locks sans rien telecharger")

    group = parser.add_argument_group("index et archivage")
    group.add_argument("--no-index", action="store_true",
                       help="ne pas generer l'index PEP 503 dans <output>/simple")
    group.add_argument("--index-only", action="store_true",
                       help="regenere index et manifeste depuis le disque, "
                            "sans reseau (a lancer apres mirror_gc.py)")
    group.add_argument("--archive", type=Path, default=None, metavar="CHEMIN",
                       help="produit une archive tar.gz (ex: dist/miroir.tar.gz)")
    group.add_argument("--archive-only", action="store_true",
                       help="archive le miroir existant sans rien resoudre")
    group.add_argument("--split-size", default=None, metavar="TAILLE",
                       help="decoupe l'archive (4G, 700M, 1.5GiB, 650MB...)")
    group.add_argument("--split-mode", choices=("volumes", "raw"), default="volumes",
                       help="volumes: archives independantes (defaut) ; "
                            "raw: decoupage binaire d'une archive unique")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    offline = args.index_only or args.archive_only

    if not offline:
        if args.requirements is None:
            print("[!] -r/--requirements est requis (ou --index-only / --archive-only)",
                  file=sys.stderr)
            return 2
        if not args.requirements.is_file():
            print(f"[!] fichier introuvable : {args.requirements}", file=sys.stderr)
            return 2

    split_size = None
    if args.split_size:
        try:
            split_size = parse_size(args.split_size)
        except ValueError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            return 2
        if split_size <= 0:
            print("[!] --split-size doit etre strictement positif", file=sys.stderr)
            return 2

    if args.split_size and args.archive is None and not args.archive_only:
        print("[!] --split-size est sans effet sans --archive", file=sys.stderr)
        return 2

    if offline:
        out_dir = args.output.resolve()
        if not out_dir.is_dir():
            print(f"[!] miroir introuvable : {out_dir}", file=sys.stderr)
            return 2
        return finalise(out_dir, args, split_size, resolutions=None)

    macos_parts = args.macos.split(".")
    try:
        macos_version = (int(macos_parts[0]), int(macos_parts[1]) if len(macos_parts) > 1 else 0)
    except ValueError:
        print(f"[!] version macOS invalide : {args.macos}", file=sys.stderr)
        return 2

    try:
        targets = [
            make_target(spec, args.glibc, macos_version)
            for spec in (args.target or DEFAULT_TARGETS)
        ]
    except ValueError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2

    roots = read_requirements(args.requirements)
    if not roots:
        print("[!] aucun requirement exploitable", file=sys.stderr)
        return 2

    out_dir: Path = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = (args.cache_dir or (out_dir / ".cache")).resolve()
    client = PyPIClient(cache_dir)

    print(f"=== Miroir {out_dir} ===")
    print(f"  racines : {len(roots)}")
    print(f"  cibles  : {', '.join(t.identifier for t in targets)}")

    # -- resolution --------------------------------------------------------

    resolutions: list[Resolution] = []
    wanted: dict[str, RemoteFile] = {}

    for target in targets:
        print(f"\n--- Resolution : {target.identifier} ---")
        resolver = Resolver(client, target, allow_prereleases=args.pre,
                            allow_sdist=not args.wheels_only)
        resolution = resolver.resolve(list(roots))
        resolutions.append(resolution)

        for cname, artifact in resolution.artifacts.items():
            wanted.setdefault(artifact.filename, artifact)
            if args.with_sdists and artifact.kind == "wheel":
                version = resolution.pins[cname]
                for remote in client.files(cname):
                    if remote.kind == "sdist" and remote.version == version and not remote.yanked:
                        wanted.setdefault(remote.filename, remote)

        print(f"  distributions resolues : {len(resolution.pins)}")
        if resolution.source_only:
            print(f"  sdist faute de wheel   : {len(resolution.source_only)} "
                  f"-> compilation requise a l'installation")
            print(f"      {', '.join(sorted(resolution.source_only))}")
        if resolution.problems:
            print(f"  problemes : {len(resolution.problems)}")
            for problem in resolution.problems[:15]:
                print(f"      ! {problem}")
            if len(resolution.problems) > 15:
                print(f"      ... et {len(resolution.problems) - 15} autre(s)")

    # -- telechargement ----------------------------------------------------

    sidecar = update_sidecar(out_dir, wanted.values())
    hashes = {f.filename: f.sha256 for f in wanted.values() if f.sha256}
    failures: list[str] = []

    if args.resolve_only:
        print(f"\n--- Resolution seule : {len(wanted)} artefact(s) retenus ---")
    else:
        print(f"\n--- Telechargement : {len(wanted)} artefact(s) ---")
        done = present = 0
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(download, remote, out_dir): remote.filename
                for remote in wanted.values()
            }
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
        print(f"\n  nouveaux : {done}   deja presents : {present}   echecs : {len(failures)}")
        for failure in failures:
            print(f"      ! {failure}")

    # -- sorties -----------------------------------------------------------

    protected = write_lockfiles(out_dir, resolutions, hashes)
    report = {
        "targets": [
            {
                "id": r.target.identifier,
                "python_full_version": r.target.python_full_version,
                "marker_env": r.target.marker_env,
                "resolved": len(r.pins),
                "source_only": sorted(r.source_only),
                "problems": r.problems,
            }
            for r in resolutions
        ],
        "artifacts": sorted(wanted),
        "download_failures": failures,
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n  locks     : {protected.parent}")
    print(f"  rapport   : {out_dir / 'report.json'}")

    finalise(out_dir, args, split_size, resolutions=resolutions, protected=protected)

    has_problems = failures or any(r.problems for r in resolutions)
    return 1 if has_problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
