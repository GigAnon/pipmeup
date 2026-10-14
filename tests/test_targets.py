import pytest

from mirror_builder.targets import make_target


def test_presets_and_marker_env():
    d12 = make_target("debian12")
    assert d12.python_version == (3, 11)
    assert d12.marker_env["sys_platform"] == "linux"
    assert d12.can_build
    assert d12.platforms[0] == "manylinux_2_41_x86_64"  # widest accepted

    d13 = make_target("debian13")
    assert d13.python_version == (3, 13)
    assert d13.platforms[0] == "manylinux_2_41_x86_64"


def test_windows_cannot_build():
    win = make_target("win_amd64:3.13")
    assert not win.can_build
    assert win.marker_env["sys_platform"] == "win32"


def test_macos_is_gone():
    with pytest.raises(ValueError):
        make_target("macos_arm64:3.13")


@pytest.mark.parametrize("spec", ["linux_x86_64", "linux_x86_64:3", "nope:3.13"])
def test_invalid_specs(spec):
    with pytest.raises(ValueError):
        make_target(spec)


def test_py314_preset():
    t = make_target("py314")
    assert t.python_version == (3, 14)
    assert t.python_full_version == "3.14.7"
    assert t.can_build


def test_glibc_preferred_ranks_before_newer():
    from packaging.tags import parse_tag
    t = make_target("py314")
    old = next(iter(parse_tag("cp314-cp314-manylinux_2_28_x86_64")))
    new = next(iter(parse_tag("cp314-cp314-manylinux_2_34_x86_64")))
    assert old in t.preferred_tags and new not in t.preferred_tags
    assert t.tag_rank[old] < t.tag_rank[new]
    # 2.41 is accepted, 2.42 is not
    assert next(iter(parse_tag("cp314-cp314-manylinux_2_41_x86_64"))) in t.tag_rank
    assert next(iter(parse_tag("cp314-cp314-manylinux_2_42_x86_64"))) not in t.tag_rank


def test_multiple_python_versions_in_one_spec():
    from mirror_builder.targets import make_targets
    ts = make_targets("linux_x86_64:3.11,3.13,3.14.7")
    assert [t.python_version for t in ts] == [(3, 11), (3, 13), (3, 14)]
    assert [t.identifier for t in ts] == ["linux_x86_64-py3.11", "linux_x86_64-py3.13",
                                          "linux_x86_64-py3.14"]
    with pytest.raises(ValueError):
        make_target("linux_x86_64:3.11,3.13")
