# PyPI mirror bundle

Self-contained, offline PyPI mirror. Nothing here needs network access or the
tooling that built it.

```
packages/          wheels and sdists (flat)
simple/            PEP 503 static index
locks/             per-target lockfiles, build-environment locks, protected.txt
mirrortool.py      verify / reindex / gc (standard library + `packaging`)
bundle.json        what this bundle was built for
MANIFEST.sha256    sha256 of every file in packages/
```

## Install

```sh
python3 mirrortool.py verify            # integrity check after transfer
python3 -m venv venv && . venv/bin/activate
pip install --index-url file://$PWD/simple <package>
```

Do **not** add `--no-index`: pip then ignores `--index-url` altogether.
Any static HTTP server works too: `python3 -m http.server -d . 8000`, then
`--index-url http://host:8000/simple`.

Reproducible install of a whole target (hash-checked):

```sh
pip install --index-url file://$PWD/simple --require-hashes -r locks/requirements-debian13.txt
```

## Source builds

Some packages only exist as sdists (see `bundle.json`, `source_only`). Linux
targets build them locally, which needs a compiler toolchain
(`build-essential`, `python3-dev`) plus the system `-dev` libraries of the
package (e.g. `libgmp-dev` for fastecdsa). Their Python build requirements
(setuptools, wheel, ...) are already in the bundle, so no network is needed.

## Maintenance

```sh
python3 mirrortool.py reindex                 # after adding/removing files in packages/
python3 mirrortool.py gc --keep-last 2        # dry run; protects locks/protected.txt
python3 mirrortool.py gc --keep-last 2 --apply
python3 mirrortool.py purge-trash 30 --apply  # delete quarantine batches older than 30 days
```

`gc` keeps the N newest versions of every distribution (all their wheels, for
every platform), never touches anything pinned in `locks/protected.txt`, and
only *moves* evicted files to `.trash/<timestamp>/`. The index and manifest are
rebuilt automatically after `--apply`.
