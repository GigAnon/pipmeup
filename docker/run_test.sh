#!/usr/bin/env bash
# Host side: test a bundle on throw-away Debian containers, with NO network.
#
#   docker/run_test.sh [-b BUNDLE_DIR] [-s SKIP] [TARGET...]
#
#   -b  bundle directory (default: build/bundle)
#   -s  comma-separated distributions to skip, e.g. wxpython (very long build)
#   TARGET: debian12 debian13 (default: every Debian target present in the bundle)
#
# The image is built WITH network (apt), then the test runs WITHOUT it.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bundle="$repo/build/bundle"
skip=""

while getopts "b:s:" opt; do
  case $opt in
    b) bundle="$(cd "$OPTARG" && pwd)" ;;
    s) skip="$OPTARG" ;;
    *) exit 2 ;;
  esac
done
shift $((OPTIND - 1))

declare -A base=( [debian12]=debian:12 [debian13]=debian:13 )

targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
  for t in "${!base[@]}"; do
    [ -f "$bundle/locks/roots-$t.txt" ] && targets+=("$t")
  done
fi
[ ${#targets[@]} -gt 0 ] || { echo "no Debian target found in $bundle/locks" >&2; exit 2; }

out="$repo/build/test-reports"
mkdir -p "$out"
status=0

for t in "${targets[@]}"; do
  echo "################ $t ################"
  docker build -q --build-arg "BASE=${base[$t]}" -t "pipmeup-test:$t" "$repo/docker" >/dev/null
  docker run --rm --network none \
    -v "$bundle:/bundle:ro" \
    -v "$repo/docker:/check:ro" \
    -v "$out:/out" \
    -e "TARGET=$t" -e "SKIP=$skip" \
    "pipmeup-test:$t" || status=1
done

echo
echo "reports: $out"
exit $status
