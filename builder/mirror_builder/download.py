"""Integrity-checked artifact download."""

from __future__ import annotations

import hashlib
import time
import urllib.request
from pathlib import Path

from .client import USER_AGENT, RemoteFile


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(remote: RemoteFile, dest_dir: Path, timeout: int = 120) -> tuple[str, str]:
    """Download one artifact into dest_dir with sha256 verification. Idempotent.

    Returns (filename, 'present' | 'downloaded').
    """
    target_path = dest_dir / remote.filename

    if target_path.is_file():
        if remote.sha256 is None or sha256_file(target_path) == remote.sha256:
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
            if remote.sha256 and sha256_file(temporary) != remote.sha256:
                raise RuntimeError("sha256 mismatch")
            # Final rename: a visible artifact is a complete, verified artifact.
            temporary.replace(target_path)
            return remote.filename, "downloaded"
        except Exception as exc:  # noqa: BLE001 - retry, then report
            last = exc
            temporary.unlink(missing_ok=True)
            time.sleep(2 ** attempt)

    raise RuntimeError(f"{remote.filename}: {last}")
