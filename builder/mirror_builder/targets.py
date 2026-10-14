"""Target platforms: wheel-tag ranking and synthetic PEP 508 marker environments."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packaging.tags import Tag, compatible_tags, cpython_tags

# manylinux_2_5 / _2_12 / _2_17 have historical aliases, defined only for some
# architectures.
_GLIBC_ALIASES = {5: "manylinux1", 12: "manylinux2010", 17: "manylinux2014"}
_ALIAS_ARCHES = {
    5: {"x86_64", "i686"},
    12: {"x86_64", "i686"},
    17: {"x86_64", "i686", "aarch64", "ppc64le", "s390x", "armv7l"},
}


def linux_platforms(arch: str, max_glibc_minor: int) -> list[str]:
    """Platform tags accepted by a glibc target, newest first.

    pip prefers the newest manylinux supported by the system; we reproduce that
    order so the wheel ranking is faithful.
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
    """A mirror target: platform + interpreter version."""

    identifier: str
    python_version: tuple[int, int]
    python_full_version: str
    platforms: tuple[str, ...]
    marker_env: dict[str, str]
    # Whether a source build is acceptable on this target. True for Linux (a
    # compiler toolchain is assumed); False for Windows (wheels only).
    can_build: bool
    tag_rank: dict[Tag, int] = field(compare=False, repr=False, default_factory=dict)

    @property
    def python_tag(self) -> str:
        return f"cp{self.python_version[0]}{self.python_version[1]}"


def build_marker_env(profile: str, machine: str, full_version: str) -> dict[str, str]:
    """Synthetic PEP 508 marker environment for the target."""
    major_minor = ".".join(full_version.split(".")[:2])

    if profile.startswith("win"):
        system, sys_platform, os_name = "Windows", "win32", "nt"
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
    "win_amd64":     {"machine": "AMD64",   "platforms": ("win_amd64",)},
    "win32":         {"machine": "x86",     "platforms": ("win32",)},
    "win_arm64":     {"machine": "ARM64",   "platforms": ("win_arm64",)},
    "linux_x86_64":  {"machine": "x86_64",  "linux_arch": "x86_64"},
    "linux_aarch64": {"machine": "aarch64", "linux_arch": "aarch64"},
}

# Named presets: '<profile>:<python>[:<glibc minor>]'.
# Debian 12 ships Python 3.11 / glibc 2.36; Debian 13 ships Python 3.13 / glibc 2.41.
PRESETS: dict[str, str] = {
    "debian12": "linux_x86_64:3.11:36",
    "debian13": "linux_x86_64:3.13:41",
}

DEFAULT_TARGETS = ["debian12", "debian13", "win_amd64:3.13"]
DEFAULT_GLIBC_MINOR = 36


def make_target(spec: str, default_glibc_minor: int = DEFAULT_GLIBC_MINOR) -> Target:
    """Build a target from a preset name or '<profile>:<pyversion>[:<glibc minor>]'."""
    preset = spec if spec in PRESETS else None
    spec = PRESETS.get(spec, spec)

    parts_spec = spec.split(":")
    if len(parts_spec) < 2 or len(parts_spec) > 3:
        raise ValueError(
            f"invalid target '{spec}' (expected '<profile>:<pyversion>[:<glibc minor>]' "
            f"or a preset: {', '.join(PRESETS)})"
        )

    profile, raw_version = parts_spec[0], parts_spec[1]
    if profile not in PROFILES:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown profile '{profile}' (known: {known})")

    glibc_minor = default_glibc_minor
    if len(parts_spec) == 3:
        try:
            glibc_minor = int(parts_spec[2])
        except ValueError as exc:
            raise ValueError(f"invalid glibc minor '{parts_spec[2]}' in '{spec}'") from exc

    version_parts = raw_version.split(".")
    if len(version_parts) < 2:
        raise ValueError(f"invalid Python version '{raw_version}' (expected 3.13 or 3.13.2)")
    try:
        major, minor = int(version_parts[0]), int(version_parts[1])
    except ValueError as exc:
        raise ValueError(f"invalid Python version '{raw_version}'") from exc

    # The patch level matters: `python_full_version >= "3.8.1"` excludes 3.8.0.
    # Without precision we assume .0 (conservative).
    full_version = raw_version if len(version_parts) >= 3 else f"{major}.{minor}.0"

    conf = PROFILES[profile]
    if "linux_arch" in conf:
        platforms = tuple(linux_platforms(conf["linux_arch"], glibc_minor))
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
    tag_rank: dict[Tag, int] = {}
    for rank, tag in enumerate(ordered):
        tag_rank.setdefault(tag, rank)

    name = preset or f"{profile}-py{major}.{minor}"
    return Target(
        identifier=name,
        python_version=python_version,
        python_full_version=full_version,
        platforms=platforms,
        marker_env=build_marker_env(profile, conf["machine"], full_version),
        can_build="linux_arch" in conf,
        tag_rank=tag_rank,
    )
