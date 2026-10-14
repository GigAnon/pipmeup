import pytest

from mirror_builder.targets import make_target


def test_presets_and_marker_env():
    d12 = make_target("debian12")
    assert d12.python_version == (3, 11)
    assert d12.marker_env["sys_platform"] == "linux"
    assert d12.can_build
    assert d12.platforms[0] == "manylinux_2_36_x86_64"

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
