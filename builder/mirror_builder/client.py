"""PyPI client with an on-disk cache, so reruns are nearly free."""

from __future__ import annotations

import email.parser
import hashlib
import json
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from packaging.tags import Tag
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

INDEX_URL = "https://pypi.org/simple"
USER_AGENT = "pymirror-builder/1.0"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
CACHE_TTL_SECONDS = 24 * 3600

# What pip assumes for a source tree without a [build-system] table (legacy
# setup.py projects): setuptools, plus wheel for older setuptools releases.
LEGACY_BUILD_REQUIRES = ["setuptools>=40.8.0", "wheel"]


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


def parse_remote_filename(filename: str) -> tuple[Version, str, frozenset[Tag]] | None:
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


def read_build_requires(archive_path: Path) -> list[str]:
    """`[build-system].requires` of an sdist (PEP 518), with pip's legacy fallback."""
    name = archive_path.name.lower()
    raw: bytes | None = None
    best_depth = 99

    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.namelist():
                    parts = member.split("/")
                    if parts[-1] == "pyproject.toml" and len(parts) - 1 < best_depth and len(parts) <= 2:
                        best_depth = len(parts) - 1
                        raw = archive.read(member)
        else:
            with tarfile.open(archive_path) as archive:
                for member in archive:
                    parts = member.name.split("/")
                    if member.isfile() and parts[-1] == "pyproject.toml" and len(parts) <= 2:
                        handle = archive.extractfile(member)
                        if handle is not None:
                            raw = handle.read()
                            break
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return list(LEGACY_BUILD_REQUIRES)

    if raw is None:
        return list(LEGACY_BUILD_REQUIRES)

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return list(LEGACY_BUILD_REQUIRES)

    system = data.get("build-system")
    if not isinstance(system, dict) or not system.get("requires"):
        return list(LEGACY_BUILD_REQUIRES)
    return [str(item) for item in system["requires"]]


class PyPIClient:
    def __init__(self, cache_dir: Path, index_url: str = INDEX_URL,
                 ttl: int = CACHE_TTL_SECONDS, timeout: int = 60) -> None:
        self.cache_dir = cache_dir
        self.index_url = index_url.rstrip("/")
        self.ttl = ttl
        self.timeout = timeout
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._simple_cache: dict[str, list[RemoteFile]] = {}
        self._meta_cache: dict[str, list[str]] = {}
        self._build_cache: dict[str, list[str]] = {}

    # -- transport ---------------------------------------------------------

    def _fetch(self, url: str, accept: str | None = None, retries: int = 3,
               timeout: int | None = None) -> bytes:
        headers = {"User-Agent": USER_AGENT}
        if accept:
            headers["Accept"] = accept
        request = urllib.request.Request(url, headers=headers)

        last: Exception | None = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            time.sleep(2 ** attempt)
        raise RuntimeError(f"download of {url} failed: {last}")

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
        """Artifacts available for a distribution (simple API, PEP 691)."""
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
            parsed = parse_remote_filename(filename)
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
        """Requires-Dist of the artifact.

        Prefer the PEP 658 .metadata attached to *this* file: dependencies can
        differ between two wheels of the same version. Fall back to the
        per-version JSON API when absent (sdists, older uploads).
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

    def build_requires(self, remote: RemoteFile) -> list[str]:
        """PEP 517/518 build requirements of an sdist.

        Not available from index metadata, so the sdist is downloaded (once,
        kept in the cache) and its pyproject.toml is read.
        """
        if remote.filename in self._build_cache:
            return self._build_cache[remote.filename]

        path = self.cache_dir / f"sdist-{remote.filename}"
        if not path.is_file():
            data = self._fetch(remote.url, timeout=300)
            if remote.sha256 and hashlib.sha256(data).hexdigest() != remote.sha256:
                raise RuntimeError(f"{remote.filename}: sha256 mismatch")
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)

        requires = read_build_requires(path)
        self._build_cache[remote.filename] = requires
        return requires
