#!/usr/bin/env bash
# Build an application-private runtime; no host packages or APT sources change.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
output=${1:-"$script_dir/../../dist-kylin"}
mkdir -p -- "$output"
output=$(cd -- "$output" && pwd)
for tool in docker python3 curl sha256sum; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done
if [[ $(uname -m) != x86_64 ]]; then
  echo 'This runtime currently supports x86_64 / Hygon x86 only.' >&2
  exit 1
fi
mapfile -t upstream < <(python3 - "$script_dir/upstream.json" <<'PY'
import json, sys
with open(sys.argv[1]) as handle:
    data = json.load(handle)
for key in ('version', 'url', 'sha256', 'baseImage'):
    print(data[key])
PY
)
build_dir=$(mktemp -d)
container=''
cleanup() {
  if [[ -n $container ]]; then docker rm "$container" >/dev/null; fi
  # The directory was created above and contains only this build's inputs.
  rm -r -- "$build_dir"
}
trap cleanup EXIT
curl --fail --location --retry 3 --connect-timeout 20 \
  "${upstream[1]}" --output "$build_dir/cockpit.deb"
printf '%s  %s\n' "${upstream[2]}" "$build_dir/cockpit.deb" | sha256sum --check --status
cp -- "$script_dir/Dockerfile" "$build_dir/Dockerfile"
cp -- "$script_dir/sources.list" "$build_dir/sources.list"
image="cockpit-kylin-runtime:${upstream[0]}"
docker build --network host --build-arg "BASE_IMAGE=${upstream[3]}" --tag "$image" "$build_dir"
container=$(docker create "$image")
docker cp "$container:/out/runtime.tar.gz" "$output/runtime.tar.gz"
docker cp "$container:/out/packages.tsv" "$output/packages.tsv"
cp -- "$script_dir/upstream.json" "$output/upstream.json"
(cd -- "$output" && sha256sum runtime.tar.gz > SHA256SUMS)
printf 'Runtime ready: %s\n' "$output/runtime.tar.gz"
