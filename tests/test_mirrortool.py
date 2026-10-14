import json
import zipfile
from pathlib import Path

import pytest

import mirrortool


def make_wheel(packages: Path, name: str, version: str, requires_python: str | None = None):
    filename = f"{name}-{version}-py3-none-any.whl"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if requires_python:
        meta += f"Requires-Python: {requires_python}\n"
    with zipfile.ZipFile(packages / filename, "w") as z:
        z.writestr(f"{name}-{version}.dist-info/METADATA", meta)
    return filename


@pytest.fixture()
def bundle(tmp_path):
    (tmp_path / "packages").mkdir()
    return tmp_path


def test_reindex_manifest_and_requires_python(bundle):
    make_wheel(bundle / "packages", "foo", "1.0", ">=3.9")
    make_wheel(bundle / "packages", "bar", "2.0")
    projects, files, unknown = mirrortool.reindex(bundle)
    assert (projects, files, unknown) == (2, 2, 0)

    page = (bundle / "simple" / "foo" / "index.html").read_text()
    assert 'data-requires-python="&gt;=3.9"' in page
    assert "../../packages/foo-1.0-py3-none-any.whl#sha256=" in page
    assert "bar" in (bundle / "simple" / "index.html").read_text()
    assert mirrortool.verify(bundle) == 0


def test_verify_detects_corruption_and_unlisted(bundle):
    name = make_wheel(bundle / "packages", "foo", "1.0")
    mirrortool.reindex(bundle)
    (bundle / "packages" / name).write_bytes(b"tampered")
    make_wheel(bundle / "packages", "extra", "1.0")
    assert mirrortool.verify(bundle) == 2


def test_gc_keeps_last_n_protected_and_all_wheels(bundle):
    pk = bundle / "packages"
    for v in ("1.0", "2.0", "3.0", "4.0"):
        make_wheel(pk, "foo", v)
    (pk / "foo-2.0-cp313-cp313-manylinux_2_17_x86_64.whl").write_bytes(b"")  # 2nd wheel, same version
    make_wheel(pk, "only_pre", "1.0a1")
    artifacts, _ = mirrortool.scan(bundle)
    protected = {("foo", mirrortool.Version("1.0"))}
    obsolete, kept = mirrortool.select_obsolete(artifacts, 2, protected)

    assert [str(v) for v in kept["foo"]] == ["4.0", "3.0", "1.0"]
    assert {a.path.name for a in obsolete} == {
        "foo-2.0-py3-none-any.whl", "foo-2.0-cp313-cp313-manylinux_2_17_x86_64.whl"}
    assert "only-pre" in kept  # pre-release kept when no stable exists


def test_gc_apply_quarantines_and_reindexes(bundle, capsys):
    pk = bundle / "packages"
    for v in ("1.0", "2.0", "3.0"):
        make_wheel(pk, "foo", v)
    mirrortool.reindex(bundle)
    args = mirrortool.build_parser().parse_args(["gc", "--keep-last", "1", "--apply"])
    assert mirrortool.cmd_gc(bundle, args) == 0
    assert [p.name for p in pk.iterdir()] == ["foo-3.0-py3-none-any.whl"]
    assert len(list((bundle / ".trash").iterdir())) == 1
    assert mirrortool.verify(bundle) == 0


def test_gc_dry_run_changes_nothing(bundle):
    pk = bundle / "packages"
    for v in ("1.0", "2.0"):
        make_wheel(pk, "foo", v)
    args = mirrortool.build_parser().parse_args(["gc", "--keep-last", "1"])
    mirrortool.cmd_gc(bundle, args)
    assert len(list(pk.iterdir())) == 2
