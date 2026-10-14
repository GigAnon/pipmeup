"""Greedy dependency resolver evaluated against a synthetic target environment.

Two kinds of requirements are followed:

* runtime requirements (`Requires-Dist`), evaluated with the target's markers;
* build requirements (`[build-system].requires`, PEP 518) of every sdist that
  ends up in the mirror. pip builds sdists in an isolated environment, so those
  requirements are resolved in a closure of their own: they may legitimately
  pin different versions than the runtime graph (e.g. setuptools).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable

from packaging.markers import Marker, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from .client import PackageNotFound, PyPIClient, RemoteFile
from .targets import Target

MAX_RESOLVER_STEPS = 50_000
MAX_BUILD_DEPTH = 6


@dataclass
class Closure:
    """Result of one resolution pass (runtime graph or one build environment)."""

    pins: dict[str, Version] = field(default_factory=dict)
    artifacts: dict[str, RemoteFile] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    # Distributions with no usable wheel on a target that cannot build from source.
    unavailable: list[str] = field(default_factory=list)
    # Distributions retained as sdists for lack of a wheel: they need a compiler
    # toolchain at install time.
    source_only: list[str] = field(default_factory=list)
    # Wheels chosen that need a newer glibc than the target's preferred one
    # (no older wheel exists for that version).
    newer_glibc: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    target: Target
    runtime: Closure = field(default_factory=Closure)
    # sdist filename -> build requirements (as written) and their closure
    build_requires: dict[str, list[str]] = field(default_factory=dict)
    build_closures: dict[str, Closure] = field(default_factory=dict)

    # -- convenience views -------------------------------------------------

    @property
    def pins(self) -> dict[str, Version]:
        return self.runtime.pins

    @property
    def artifacts(self) -> dict[str, RemoteFile]:
        return self.runtime.artifacts

    @property
    def source_only(self) -> list[str]:
        return self.runtime.source_only

    @property
    def problems(self) -> list[str]:
        out = list(self.runtime.problems)
        for filename, closure in self.build_closures.items():
            out.extend(f"[build env of {filename}] {p}" for p in closure.problems)
        return out

    @property
    def newer_glibc(self) -> list[str]:
        out = list(self.runtime.newer_glibc)
        for filename, closure in self.build_closures.items():
            out.extend(f"[build env of {filename}] {n}" for n in closure.newer_glibc)
        return out

    @property
    def unavailable(self) -> list[str]:
        out = list(self.runtime.unavailable)
        for filename, closure in self.build_closures.items():
            out.extend(f"[build env of {filename}] {u}" for u in closure.unavailable)
        return out

    def all_artifacts(self) -> dict[str, RemoteFile]:
        """Every artifact to mirror (runtime + build environments), by filename."""
        merged = {a.filename: a for a in self.runtime.artifacts.values()}
        for closure in self.build_closures.values():
            for artifact in closure.artifacts.values():
                merged.setdefault(artifact.filename, artifact)
        return merged

    def all_pins(self) -> set[tuple[str, Version]]:
        pins = set(self.runtime.pins.items())
        for closure in self.build_closures.values():
            pins |= set(closure.pins.items())
        return pins


def marker_ok(marker: Marker | None, env: dict[str, str], extras: Iterable[str]) -> bool:
    """A marker holds if it does without an extra, or for a requested extra."""
    if marker is None:
        return True
    for extra in [""] + sorted(extras):
        try:
            if marker.evaluate({**env, "extra": extra}):
                return True
        except UndefinedEnvironmentName:
            continue
    return False


def requires_python_ok(spec: str | None, full_version: str) -> bool:
    if not spec:
        return True
    try:
        return SpecifierSet(spec).contains(full_version, prereleases=True)
    except InvalidSpecifier:
        return True


def best_wheel(files: list[RemoteFile], target: Target) -> RemoteFile | None:
    """Best wheel for the target, according to the target's tag ranking."""
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
        # Source builds are only acceptable where the target can build.
        self.allow_sdist = allow_sdist and target.can_build
        self._by_version: dict[str, dict[Version, list[RemoteFile]]] = {}

    # -- candidate selection ---------------------------------------------

    def _versions(self, cname: str) -> dict[Version, list[RemoteFile]]:
        if cname not in self._by_version:
            grouped: dict[Version, list[RemoteFile]] = defaultdict(list)
            for remote in self.client.files(cname):
                grouped[remote.version].append(remote)
            self._by_version[cname] = dict(grouped)
        return self._by_version[cname]

    def _pool(self, cname: str, constraint: SpecifierSet) -> list[Version]:
        grouped = self._versions(cname)
        allow_pre = self.allow_prereleases or bool(constraint.prereleases)
        ordered = sorted(grouped, reverse=True)
        stable = [v for v in ordered if not v.is_prerelease]
        return ordered if allow_pre or not stable else stable

    def _usable(self, cname: str, version: Version) -> list[RemoteFile]:
        # requires-python is carried by each file; one compatible artifact suffices.
        return [
            f for f in self._versions(cname)[version]
            if requires_python_ok(f.requires_python, self.target.python_full_version)
        ]

    def _select(self, cname: str, constraint: SpecifierSet) -> tuple[Version, RemoteFile] | None:
        for version in self._pool(cname, constraint):
            if not constraint.contains(version, prereleases=True):
                continue
            usable = self._usable(cname, version)
            if not usable:
                continue
            wheel = best_wheel(usable, self.target)
            if wheel is not None:
                return version, wheel

            # sdist fallback: many old or niche distributions publish no wheel
            # (crcmod, netifaces...). An sdist is platform-neutral, hence valid
            # for any target that can build -- at the price of a compilation
            # at install time.
            if self.allow_sdist:
                sdists = [f for f in usable if f.kind == "sdist" and not f.yanked]
                if sdists:
                    return version, sdists[0]
        return None

    def _sdist_only_candidate(self, cname: str, constraint: SpecifierSet) -> bool:
        """True if a matching version exists but only as an sdist (no usable wheel)."""
        for version in self._pool(cname, constraint):
            if not constraint.contains(version, prereleases=True):
                continue
            usable = self._usable(cname, version)
            if any(f.kind == "sdist" and not f.yanked for f in usable):
                return True
        return False

    # -- one resolution pass ----------------------------------------------

    def _solve(self, roots: list[Requirement]) -> Closure:
        result = Closure()
        constraints: dict[str, SpecifierSet] = defaultdict(SpecifierSet)
        extras: dict[str, set[str]] = defaultdict(set)

        queue: deque[Requirement] = deque(
            r for r in roots if marker_ok(r.marker, self.target.marker_env, r.extras)
        )
        steps = 0

        while queue:
            steps += 1
            if steps > MAX_RESOLVER_STEPS:
                result.problems.append("resolution aborted: too many iterations")
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
                    result.problems.append(f"{cname}: not found on the index")
                    continue
                except RuntimeError as exc:
                    result.problems.append(f"{cname}: {exc}")
                    continue

                if selection is None:
                    try:
                        sdist_only = (not self.allow_sdist
                                      and self._sdist_only_candidate(cname, merged))
                    except PackageNotFound:
                        sdist_only = False
                    if sdist_only:
                        message = (f"{cname}{merged}: no wheel for {self.target.identifier} "
                                   f"(sdist only, source builds not allowed on this target)")
                        if message not in result.unavailable:
                            result.unavailable.append(message)
                    else:
                        result.problems.append(
                            f"{cname}{merged}: no compatible artifact "
                            f"({self.target.identifier})"
                        )
                    continue

                version, artifact = selection
                result.pins[cname] = version
                result.artifacts[cname] = artifact
                if artifact.kind == "sdist" and cname not in result.source_only:
                    result.source_only.append(cname)
                if (artifact.kind == "wheel" and self.target.preferred_tags
                        and not (artifact.tags & self.target.preferred_tags)):
                    result.newer_glibc.append(artifact.filename)
                explore_extras = extras[cname]
            else:
                artifact = result.artifacts[cname]
                explore_extras = new_extras

            for raw in self.client.requires_dist(artifact):
                try:
                    dependency = Requirement(raw)
                except InvalidRequirement:
                    result.problems.append(f"{cname}: unreadable Requires-Dist '{raw}'")
                    continue
                if marker_ok(dependency.marker, self.target.marker_env, explore_extras):
                    queue.append(dependency)

        return result

    # -- build environments -------------------------------------------------

    def _add_build_closures(self, resolution: Resolution, closure: Closure, depth: int) -> None:
        for artifact in list(closure.artifacts.values()):
            if artifact.kind != "sdist" or artifact.filename in resolution.build_closures:
                continue

            filename = artifact.filename
            if depth >= MAX_BUILD_DEPTH:
                closure.problems.append(f"{filename}: build environments nested too deeply")
                continue

            try:
                raw_requires = self.client.build_requires(artifact)
            except (RuntimeError, OSError) as exc:
                closure.problems.append(f"{filename}: cannot read build requirements: {exc}")
                continue

            requirements: list[Requirement] = []
            for raw in raw_requires:
                try:
                    requirements.append(Requirement(raw))
                except InvalidRequirement:
                    closure.problems.append(f"{filename}: unreadable build requirement '{raw}'")

            # Register before solving: a backend whose own sdist needs itself
            # (setuptools, flit-core...) must not recurse forever.
            resolution.build_requires[filename] = raw_requires
            build_closure = self._solve(requirements)
            resolution.build_closures[filename] = build_closure
            self._add_build_closures(resolution, build_closure, depth + 1)

    def resolve(self, roots: list[Requirement]) -> Resolution:
        resolution = Resolution(target=self.target)
        resolution.runtime = self._solve(roots)
        self._add_build_closures(resolution, resolution.runtime, depth=0)
        return resolution
