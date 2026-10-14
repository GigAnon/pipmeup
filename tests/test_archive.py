import hashlib
import random
import tarfile
import zipfile
from pathlib import Path

import pytest

from mirror_builder.archive import (
    FORMATS, create_archive, format_from_name, normalise_format, parse_size, strip_extension)


@pytest.fixture()
def bundle(tmp_path):
    root = tmp_path / "bundle"
    (root / "packages").mkdir(parents=True)
    (root / ".trash" / "x").mkdir(parents=True)
    (root / ".trash" / "x" / "old.whl").write_bytes(b"old")
    (root / "mirrortool.py").write_text("print('hi')\n")
    (root / "packages" / "a-1.0-py3-none-any.whl").write_bytes(random.Random(1).randbytes(5000))
    (root / "packages" / "b-1.0-py3-none-any.whl").write_bytes(random.Random(2).randbytes(5000))
    (root / "packages" / "c-1.0.tar.gz").write_bytes(random.Random(3).randbytes(5000))
    return root


def names(path: Path, fmt: str) -> list[str]:
    if fmt == "zip":
        with zipfile.ZipFile(path) as z:
            return sorted(z.namelist())
    with tarfile.open(path) as t:
        return sorted(t.getnames())


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("fmt", FORMATS)
def test_each_format_roundtrips_and_excludes_trash(bundle, tmp_path, fmt):
    dest = tmp_path / "out" / f"mybundle.{fmt}"
    parts = create_archive(bundle, dest, None, "volumes")
    assert [p.name for p in parts] == [f"mybundle.{fmt}"]
    got = names(parts[0], fmt)
    assert "mybundle/mirrortool.py" in got
    assert "mybundle/packages/a-1.0-py3-none-any.whl" in got
    assert not any(".trash" in n for n in got)
    assert (dest.parent / "mybundle.SHA256SUMS").is_file()
    assert (dest.parent / "mybundle.RESTORE.txt").is_file()


@pytest.mark.parametrize("fmt", [f for f in FORMATS if f != "zip"])
def test_tar_owner_and_group_are_stripped(bundle, tmp_path, fmt):
    parts = create_archive(bundle, tmp_path / f"o.{fmt}", None, "volumes")
    with tarfile.open(parts[0]) as t:
        for member in t:
            assert (member.uid, member.gid) == (0, 0)
            assert (member.uname, member.gname) == ("", "")
            assert member.mtime == 0
            assert "uname" not in member.pax_headers and "gname" not in member.pax_headers


def test_zip_has_no_owner_and_fixed_metadata(bundle, tmp_path):
    parts = create_archive(bundle, tmp_path / "o.zip", None, "volumes")
    with zipfile.ZipFile(parts[0]) as z:
        for info in z.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.extra == b""            # no uid/gid or timestamp extra fields
            mode = info.external_attr >> 16
            assert mode == (0o755 if info.filename.endswith(".py") else 0o644)
        assert z.testzip() is None
        assert z.read("o/mirrortool.py") == b"print('hi')\n"


@pytest.mark.parametrize("fmt", FORMATS)
def test_archives_are_reproducible(bundle, tmp_path, fmt):
    one = create_archive(bundle, tmp_path / "x" / f"same.{fmt}", None, "volumes")[0]
    two = create_archive(bundle, tmp_path / "y" / f"same.{fmt}", None, "volumes")[0]
    assert digest(one) == digest(two)


def test_gzip_header_does_not_embed_the_filename(bundle, tmp_path):
    part = create_archive(bundle, tmp_path / "secret-name.tar.gz", None, "volumes")[0]
    assert b"secret-name" not in part.read_bytes()[:64]


@pytest.mark.parametrize("fmt", ["tar.gz", "zip"])
def test_volumes_respect_the_limit_and_are_independent(bundle, tmp_path, fmt):
    parts = create_archive(bundle, tmp_path / f"v.{fmt}", 7000, "volumes")
    assert len(parts) >= 3
    assert [p.name for p in parts][0] == f"v.part001.{fmt}"
    seen = []
    for part in parts:
        seen += [n for n in names(part, fmt) if n.startswith("v/packages/")]
    assert sorted(seen) == [f"v/packages/{n}" for n in
                            ("a-1.0-py3-none-any.whl", "b-1.0-py3-none-any.whl", "c-1.0.tar.gz")]


def test_raw_split_reassembles(bundle, tmp_path):
    parts = create_archive(bundle, tmp_path / "r.tar.gz", 3000, "raw")
    assert len(parts) > 1 and parts[0].name == "r.tar.gz.000"
    whole = tmp_path / "joined.tar.gz"
    whole.write_bytes(b"".join(p.read_bytes() for p in parts))
    assert "r/mirrortool.py" in names(whole, "tar.gz")


def test_format_helpers():
    assert format_from_name("x.tgz") == "tar.gz"
    assert format_from_name("x.TAR.XZ") == "tar.xz"
    assert format_from_name("x.zip") == "zip"
    assert format_from_name("x.rar") is None
    assert strip_extension("pipmeup-bundle-20261014.tar.gz") == "pipmeup-bundle-20261014"
    assert normalise_format("tgz") == "tar.gz"
    with pytest.raises(ValueError):
        normalise_format("rar")
    assert parse_size("4G") == 4 * 1024 ** 3
