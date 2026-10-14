"""Reproducible archiving and splitting of a bundle, for transfer media."""

from __future__ import annotations

import gzip
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

from .download import sha256_file

_SIZE_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?)(?P<base>i?B?)\s*$",
                      re.IGNORECASE)
_SIZE_FACTORS = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}

EXCLUDED_TOP_LEVEL = {".trash", ".cache"}


def parse_size(text: str) -> int:
    """'4G' / '700MiB' -> bytes. K/M/G/T = 1024^n; KB/MB/GB = 1000^n."""
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"invalid size '{text}' (expected 4G, 700M, 1.5GiB, 650MB...)")
    unit = match.group("unit").upper()
    base = 1000 if match.group("base").lower() == "b" else 1024
    return int(float(match.group("value")) * base ** _SIZE_FACTORS[unit])


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def archive_members(bundle_dir: Path) -> list[tuple[Path, str]]:
    """(absolute path, archive name), sorted for reproducibility.

    The quarantine is excluded: it is local working data, worthless on the other
    side of the air gap.
    """
    members: list[tuple[Path, str]] = []
    prefix = bundle_dir.name
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        relative = path.relative_to(bundle_dir)
        if relative.parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        members.append((path, f"{prefix}/{relative.as_posix()}"))
    return members


def _normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Deterministic metadata: two identical runs give identical bytes."""
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    info.mode = 0o755 if (info.isdir() or info.name.endswith(".py")) else 0o644
    return info


def create_volumes(members: list[tuple[Path, str]], dest_prefix: Path,
                   limit: int | None) -> list[Path]:
    """Independent tar.gz volumes, each under the limit when possible.

    Every volume extracts on its own: an unreadable medium only costs the
    packages it carried instead of ruining the whole set.
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
        # Real flush to learn the current compressed size: wheels are already
        # zip files, so the compression-ratio cost is negligible.
        gz.flush()
        raw.flush()
        written = raw.tell()

        if limit and written > limit and size > limit:
            oversized.append(arcname)

    tar.close(); gz.close(); raw.close()
    parts.append(path)

    for name in oversized:
        print(f"  [!] {name} alone exceeds the volume limit", file=sys.stderr)
    return parts


def create_raw_split(members: list[tuple[Path, str]], dest_prefix: Path,
                     limit: int | None) -> list[Path]:
    """A single archive, then a binary split into fixed-size chunks."""
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


def create_archive(bundle_dir: Path, dest: Path, split_size: int | None,
                   mode: str) -> list[Path]:
    """Produce the archive (or chunks) plus SHA256SUMS and RESTORE.txt."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefix = dest.parent / dest.name.removesuffix(".tar.gz").removesuffix(".tgz")

    members = archive_members(bundle_dir)
    if mode == "volumes":
        parts = create_volumes(members, prefix, split_size)
    else:
        parts = create_raw_split(members, prefix, split_size)

    sums = dest.parent / "SHA256SUMS"
    sums.write_text(
        "\n".join(f"{sha256_file(p)}  {p.name}" for p in parts) + "\n",
        encoding="utf-8",
    )

    if mode == "volumes":
        restore = (
            "Independent volumes: each extracts on its own, into this same directory.\n"
            "Order does not matter; extracting all of them rebuilds the bundle.\n\n"
            "  sha256sum -c SHA256SUMS\n"
            f"  for v in {prefix.name}.part*.tar.gz; do tar xzf \"$v\"; done\n"
        )
    else:
        restore = (
            "Binary split: ALL chunks are required, in order.\n\n"
            "  sha256sum -c SHA256SUMS\n"
            f"  cat {prefix.name}.tar.gz.* > {prefix.name}.tar.gz\n"
            f"  tar xzf {prefix.name}.tar.gz\n"
        )

    (dest.parent / "RESTORE.txt").write_text(
        restore
        + "\nThen, on the isolated side:\n"
        f"  cd {bundle_dir.name}\n"
        "  python3 mirrortool.py verify\n"
        "  pip install --index-url file://$(pwd)/simple <package>\n",
        encoding="utf-8",
    )
    return parts
