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

Targets: `debian12` (Python 3.11, glibc 2.36), `debian13` (Python 3.13, glibc 2.41),
`win_amd64:3.13`, or `<profile>:<pyver>[:<glibc minor>]`. macOS is not supported.

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

## Known issues

* **pendulum 2.1.2 on Python 3.13** cannot be built from its sdist: its
  `build.py` imports `distutils` (removed in 3.12) and poetry-core runs it in the
  target venv, outside the isolated build environment. It builds with
  setuptools installed in the venv plus `--no-build-isolation`. A prebuilt-wheel
  step (build in the Docker image, add the wheel to the bundle) is the planned fix.
  The pin comes from `asciidocbuilder` requiring `pendulum<3`.
* `inflate` and `hdlcontroller` import `pkg_resources`, which the newest
  setuptools no longer ships; they install but fail to import unless an older
  setuptools is pinned.
