"""Resolver tests against a fake in-memory index (no network)."""

from packaging.requirements import Requirement
from packaging.tags import parse_tag
from packaging.version import Version

from mirror_builder.client import RemoteFile, parse_remote_filename, read_build_requires
from mirror_builder.resolver import Resolver
from mirror_builder.targets import make_target


def remote(filename, requires_python=None):
    version, kind, tags = parse_remote_filename(filename)
    return RemoteFile(filename, f"https://x/{filename}", "0" * 64, requires_python,
                      False, False, version, kind, tags)


class FakeClient:
    def __init__(self, files, deps=None, build=None):
        self._files = files
        self._deps = deps or {}
        self._build = build or {}

    def files(self, name):
        from mirror_builder.client import PackageNotFound
        if name not in self._files:
            raise PackageNotFound(name)
        return [remote(f) for f in self._files[name]]

    def requires_dist(self, r):
        return self._deps.get(r.filename, [])

    def build_requires(self, r):
        return self._build.get(r.filename, ["setuptools>=40.8.0", "wheel"])


INDEX = {
    "pure": ["pure-1.0-py3-none-any.whl"],
    "native": ["native-1.0-cp313-cp313-manylinux_2_17_x86_64.whl",
               "native-1.0-cp313-cp313-win_amd64.whl"],
    "srconly": ["srconly-2.0.tar.gz"],
    "setuptools": ["setuptools-70.0-py3-none-any.whl", "setuptools-60.0-py3-none-any.whl"],
    "wheel": ["wheel-0.40-py3-none-any.whl"],
    "winonly": ["winonly-1.0-cp313-cp313-win_amd64.whl"],
}


def resolve(roots, target, **kw):
    client = FakeClient(INDEX, **kw)
    return Resolver(client, make_target(target)).resolve([Requirement(r) for r in roots])


def test_wheel_selection_per_target():
    linux = resolve(["native"], "debian13")
    assert "manylinux" in linux.artifacts["native"].filename
    win = resolve(["native"], "win_amd64:3.13")
    assert "win_amd64" in win.artifacts["native"].filename


def test_sdist_fallback_linux_pulls_build_requirements():
    res = resolve(["srconly"], "debian13")
    assert res.source_only == ["srconly"]
    closure = res.build_closures["srconly-2.0.tar.gz"]
    assert set(closure.pins) == {"setuptools", "wheel"}
    assert closure.pins["setuptools"] == Version("70.0")
    names = set(res.all_artifacts())
    assert "setuptools-70.0-py3-none-any.whl" in names
    assert ("setuptools", Version("70.0")) in res.all_pins()


def test_windows_never_builds_sdist_and_warns():
    res = resolve(["srconly"], "win_amd64:3.13")
    assert "srconly" not in res.pins
    assert res.unavailable and not res.problems
    assert not res.build_closures


def test_build_env_may_pin_differently_from_runtime():
    # runtime needs old setuptools, the sdist's build env wants a new one.
    deps = {"pure-1.0-py3-none-any.whl": ["setuptools<65"]}
    client = FakeClient(INDEX, deps=deps,
                        build={"srconly-2.0.tar.gz": ["setuptools>=65"]})
    res = Resolver(client, make_target("debian13")).resolve(
        [Requirement("pure"), Requirement("srconly")])
    assert res.pins["setuptools"] == Version("60.0")
    closure = res.build_closures["srconly-2.0.tar.gz"]
    assert closure.pins["setuptools"] == Version("70.0")
    assert not res.problems


def test_root_markers_are_honoured():
    linux = resolve(['winonly; sys_platform == "win32"'], "debian13")
    assert "winonly" not in linux.pins and not linux.problems
    win = resolve(['winonly; sys_platform == "win32"'], "win_amd64:3.13")
    assert "winonly" in win.pins


def test_missing_distribution_is_a_problem():
    res = resolve(["doesnotexist"], "debian13")
    assert res.problems


def test_read_build_requires(tmp_path):
    import io, tarfile

    def make(name, files):
        path = tmp_path / name
        with tarfile.open(path, "w:gz") as tar:
            for member, data in files.items():
                info = tarfile.TarInfo(member)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    modern = make("a-1.0.tar.gz", {"a-1.0/pyproject.toml":
                  b'[build-system]\nrequires=["flit_core>=3.2"]\n'})
    assert read_build_requires(modern) == ["flit_core>=3.2"]

    legacy = make("b-1.0.tar.gz", {"b-1.0/setup.py": b"x"})
    assert read_build_requires(legacy)[0].startswith("setuptools")

    no_system = make("c-1.0.tar.gz", {"c-1.0/pyproject.toml": b'[tool.x]\na=1\n'})
    assert read_build_requires(no_system)[0].startswith("setuptools")
