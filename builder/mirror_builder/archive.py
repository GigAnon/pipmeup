"""Reproducible archiving and splitting of a bundle, for transfer media."""

from __future__ import annotations

import gzip
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from .download import sha256_file

_SIZE_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?)(?P<base>i?B?)\s*$",
                      re.IGNORECASE)
_SIZE_FACTORS = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}

EXCLUDED_TOP_LEVEL = {".trash", ".cache"}

# Supported archive formats, and how to extract them.
FORMATS = ("tar.gz", "tar.xz", "tar.bz2", "tar", "zip")
FORMAT_ALIASES = {"tgz": "tar.gz", "txz": "tar.xz", "tbz2": "tar.bz2", "tbz": "tar.bz2"}
_EXTENSIONS = {
    ".tar.gz": "tar.gz", ".tgz": "tar.gz",
    ".tar.xz": "tar.xz", ".txz": "tar.xz",
    ".tar.bz2": "tar.bz2", ".tbz2": "tar.bz2", ".tbz": "tar.bz2",
    ".tar": "tar", ".zip": "zip",
}
_EXTRACT = {"tar.gz": "tar xzf", "tar.xz": "tar xJf", "tar.bz2": "tar xjf",
            "tar": "tar xf", "zip": "unzip -q"}
# Members that are already compressed: stored as-is inside a zip.
_ALREADY_COMPRESSED = (".whl", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")


def normalise_format(name: str) -> str:
    """'tgz' -> 'tar.gz', etc. Raises ValueError for unknown formats."""
    fmt = FORMAT_ALIASES.get(name, name)
    if fmt not in FORMATS:
        raise ValueError(f"unknown archive format '{name}' (known: "
                         f"{', '.join(FORMATS + tuple(FORMAT_ALIASES))})")
    return fmt


def format_from_name(filename: str) -> str | None:
    """Archive format implied by a filename's extension, if any."""
    lowered = filename.lower()
    for extension in sorted(_EXTENSIONS, key=len, reverse=True):
        if lowered.endswith(extension):
            return _EXTENSIONS[extension]
    return None


def strip_extension(filename: str) -> str:
    lowered = filename.lower()
    for extension in sorted(_EXTENSIONS, key=len, reverse=True):
        if lowered.endswith(extension):
            return filename[: -len(extension)]
    return filename


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


def archive_members(bundle_dir: Path, root_name: str | None = None) -> list[tuple[Path, str]]:
    """(absolute path, archive name), sorted for reproducibility.

    The quarantine is excluded: it is local working data, worthless on the other
    side of the air gap.
    """
    members: list[tuple[Path, str]] = []
    prefix = root_name or bundle_dir.name
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        relative = path.relative_to(bundle_dir)
        if relative.parts[0] in EXCLUDED_TOP_LEVEL or "__pycache__" in relative.parts:
            continue
        members.append((path, f"{prefix}/{relative.as_posix()}"))
    return members


def _normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Deterministic metadata: two identical runs give identical bytes.

    Owner and group are stripped: numeric ids 0 and empty names, so the files
    belong to whoever extracts them and nothing about the build machine leaks.
    """
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.mode = 0o755 if (info.isdir() or info.name.endswith(".py")) else 0o644
    return info


class _Writer:
    """One output archive, in any supported format, with normalised metadata.

    Whatever the format, nothing about the build machine leaks in: no owner or
    group names (ids 0, empty names), no timestamps, fixed permissions.
    """

    def __init__(self, path: Path, fmt: str) -> None:
        self.path = path
        self.fmt = fmt
        self._raw = path.open("wb")
        self._gz = None
        self._tar = None
        self._zip = None
        if fmt == "zip":
            self._zip = zipfile.ZipFile(self._raw, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
        elif fmt == "tar.gz":
            # filename="" keeps the archive's own name out of the gzip header.
            self._gz = gzip.GzipFile(filename="", fileobj=self._raw, mode="wb",
                                     compresslevel=6, mtime=0)
            self._tar = tarfile.open(fileobj=self._gz, mode="w", format=tarfile.PAX_FORMAT)
        elif fmt == "tar.xz":
            self._tar = tarfile.open(fileobj=self._raw, mode="w:xz", format=tarfile.PAX_FORMAT)
        elif fmt == "tar.bz2":
            self._tar = tarfile.open(fileobj=self._raw, mode="w:bz2", format=tarfile.PAX_FORMAT)
        else:
            self._tar = tarfile.open(fileobj=self._raw, mode="w", format=tarfile.PAX_FORMAT)

    def add(self, source: Path, arcname: str) -> None:
        if self._zip is not None:
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3  # Unix: so the permission bits below are honoured
            info.external_attr = (0o755 if arcname.endswith(".py") else 0o644) << 16
            info.compress_type = (zipfile.ZIP_STORED
                                  if arcname.lower().endswith(_ALREADY_COMPRESSED)
                                  else zipfile.ZIP_DEFLATED)
            size = source.stat().st_size
            with source.open("rb") as src, \
                    self._zip.open(info, "w", force_zip64=size >= (1 << 31) - 1) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        else:
            self._tar.add(source, arcname=arcname, filter=_normalise)

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
        if self._tar is not None:
            self._tar.close()
        if self._gz is not None:
            self._gz.close()
        self._raw.close()


# Per-member overhead counted against the volume limit (tar/zip headers).
_HEADER_ALLOWANCE = 1024


def create_volumes(members: list[tuple[Path, str]], dest_prefix: Path,
                   limit: int | None, fmt: str = "tar.gz") -> list[Path]:
    """Independent archives, each under the limit when possible.

    Every volume extracts on its own: an unreadable medium only costs the
    packages it carried instead of ruining the whole set. The limit is checked
    against the uncompressed size, which is a safe upper bound: wheels and sdists
    are already compressed.
    """
    parts: list[Path] = []
    index = 0
    oversized: list[str] = []

    def open_volume() -> _Writer:
        nonlocal index
        index += 1
        name = (f"{dest_prefix.name}.part{index:03d}.{fmt}" if limit
                else f"{dest_prefix.name}.{fmt}")
        return _Writer(dest_prefix.parent / name, fmt)

    writer = open_volume()
    written = 0

    for source, arcname in members:
        size = source.stat().st_size + _HEADER_ALLOWANCE
        if limit and written > 0 and (written + size) > limit:
            writer.close()
            parts.append(writer.path)
            writer = open_volume()
            written = 0

        writer.add(source, arcname)
        written += size

        if limit and size > limit:
            oversized.append(arcname)

    writer.close()
    parts.append(writer.path)

    for name in oversized:
        print(f"  [!] {name} alone exceeds the volume limit", file=sys.stderr)
    return parts


def create_raw_split(members: list[tuple[Path, str]], dest_prefix: Path,
                     limit: int | None, fmt: str = "tar.gz") -> list[Path]:
    """A single archive, then a binary split into fixed-size chunks."""
    whole = dest_prefix.parent / f"{dest_prefix.name}.{fmt}"
    writer = _Writer(whole, fmt)
    for source, arcname in members:
        writer.add(source, arcname)
    writer.close()

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
                   mode: str, fmt: str | None = None) -> list[Path]:
    """Produce the archive (or parts) plus <stem>.SHA256SUMS and <stem>.RESTORE.txt.

    The format comes from `fmt`, else from the destination's extension, else
    tar.gz. The archive's top-level directory carries the archive's own name.
    """
    fmt = normalise_format(fmt) if fmt else (format_from_name(dest.name) or "tar.gz")
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefix = dest.parent / strip_extension(dest.name)
    root_name = prefix.name

    members = archive_members(bundle_dir, root_name)
    if mode == "volumes":
        parts = create_volumes(members, prefix, split_size, fmt)
    else:
        parts = create_raw_split(members, prefix, split_size, fmt)

    (dest.parent / f"{root_name}.SHA256SUMS").write_text(
        "\n".join(f"{sha256_file(p)}  {p.name}" for p in parts) + "\n",
        encoding="utf-8",
    )

    extract = _EXTRACT[fmt]
    if len(parts) == 1:
        restore = (
            f"  sha256sum -c {root_name}.SHA256SUMS\n"
            f"  {extract} {parts[0].name}\n"
        )
    elif mode == "volumes":
        restore = (
            "Independent volumes: each extracts on its own, into this same directory.\n"
            "Order does not matter; extracting all of them rebuilds the bundle.\n\n"
            f"  sha256sum -c {root_name}.SHA256SUMS\n"
            f"  for v in {root_name}.part*.{fmt}; do {extract} \"$v\"; done\n"
        )
    else:
        restore = (
            "Binary split: ALL chunks are required, in order.\n\n"
            f"  sha256sum -c {root_name}.SHA256SUMS\n"
            f"  cat {root_name}.{fmt}.* > {root_name}.{fmt}\n"
            f"  {extract} {root_name}.{fmt}\n"
        )

    (dest.parent / f"{root_name}.RESTORE.txt").write_text(
        restore
        + "\nThen, on the isolated side:\n"
        f"  cd {root_name}\n"
        "  python3 mirrortool.py verify\n"
        "  pip install --index-url file://$(pwd)/simple <package>\n",
        encoding="utf-8",
    )
    return parts
