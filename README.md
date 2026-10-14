# pipmeup

Builds a **self-sufficient, offline PyPI mirror bundle** for air-gapped Debian
(12 and 13) machines, and tests that bundle in throw-away containers with no
network.

```
builder/            builds the bundle (needs network): resolver, downloader, archiver
bundle-template/    files copied verbatim into every bundle (mirrortool.py, README)
requirements/       root packages (packages.txt)
docker/             offline install test on Debian 12 / 13
tests/              unit tests (pytest, no network)
build/              output, git-ignored: build/bundle is the delivery bundle
```

The builder depends on the bundle template, never the other way round: a bundle
contains nothing that needs the builder.

## Build

```sh
pip install packaging pytest
python3 builder/build_bundle.py                       # all default targets -> build/bundle
python3 builder/build_bundle.py --target debian13     # one target
python3 builder/build_bundle.py --archive build/dist/bundle.tar.gz --split-size 4G
```

Targets: `py314` (Python 3.14.7), `debian12` (Python 3.11, glibc 2.36), `debian13`
(Python 3.13, glibc 2.41), `win_amd64:3.14.7`, or `<profile>:<pyver>[:<glibc minor>]`. macOS is not supported.

How it differs from `pip download --platform`: dependencies are resolved by the
builder against a synthetic marker environment for each target, so
`pywin32; sys_platform == "win32"`-style dependencies are not silently dropped
when building from Linux.

**Source builds.** Distributions with no wheel are shipped as sdists on Linux
targets (assumes `build-essential` on the machine; Windows targets are
wheels-only and only emit a warning). For every sdist the builder reads
`[build-system].requires` and resolves those build requirements into the bundle
too, in their own closure (a build environment may need a different setuptools
than the runtime graph). Undeclared build requirements can't be seen
statically; the Docker test is the safety net.

## Test on a target (needs Docker on the host)

```sh
docker/run_test.sh                     # debian12 + debian13, no network
docker/run_test.sh -s wxpython         # skip the very long wxPython build
docker/run_test.sh debian13
```

Per target it verifies the manifest, installs every root one at a time
(`pip install --index-url file:///bundle/simple`), runs `pip check`, installs
the hash-locked lockfile, and imports every top-level module. Reports land in
`build/test-reports/`.

`pip install --no-index --index-url ...` does **not** work: `--no-index` makes
pip ignore `--index-url`. Network isolation comes from `docker run --network none`.

## Unit tests

```sh
python3 -m pytest tests
```

## Notes

* **pkg_resources**: removed from setuptools in 82 and not published as a separate
  distribution, so `setuptools<82` is pinned in `requirements/packages.txt`
  (`inflate` and `hdlcontroller` import it).
* **wxpython** has no Linux wheel on PyPI (only Windows and macOS). The Linux
  wheels published at extras.wxpython.org cover Debian up to 11 and Ubuntu up to
  24.04, not Debian 12/13, so Linux targets build the sdist (heavy GTK build;
  `docker/run_test.sh -s wxpython` skips it).
* **Targets**: `py314` is Python 3.14.7 on glibc 2.36 (the conservative floor,
  usable on Debian 12 and 13), tested in the `python:3.14-bookworm` image.
* Dropped `asciidocbuilder`: it pinned `pendulum<3`, and pendulum 2.x cannot be
  built on Python >= 3.12 (its build script imports `distutils`, outside the
  isolated build environment). `pendulum` itself stays as a root and now
  resolves to 3.x, which ships wheels.
