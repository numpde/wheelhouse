#!/usr/bin/env bash
# Run each workflow against the local engine selected by the operator entrypoint.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
lane=$1
docker_host=$2
docker=(docker --host "$docker_host")
image=wheelhouse:proof
run=("${docker[@]}" run --rm --pull never
    --mount "type=bind,src=$PWD/tests,dst=/tests,readonly"
    --mount "type=bind,src=$PWD/dist/wheelhouse.pyz,dst=/bundle/wheelhouse.pyz,readonly"
    --workdir /tests --entrypoint python --env PYTHONPATH=/bundle/wheelhouse.pyz)

build_image() {
    "${docker[@]}" build --tag "$image" .
}

package() (
    mkdir -p dist
    trap 'rm -f dist/wheelhouse.pyz.tmp' EXIT
    "${docker[@]}" run --rm --pull never --network none --read-only \
        --entrypoint python "$image" -B /opt/wheelhouse/build_bundle.py \
        > dist/wheelhouse.pyz.tmp
    mv dist/wheelhouse.pyz.tmp dist/wheelhouse.pyz
    cd dist
    sha256sum wheelhouse.pyz > SHA256SUMS
)

repo_checks() {
    "${docker[@]}" run --rm --pull never --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges \
        --mount "type=bind,src=$PWD/wheelhouse.py,dst=/source/wheelhouse.py,readonly" \
        --mount "type=bind,src=$PWD/tests,dst=/source/tests,readonly" \
        --mount "type=bind,src=$PWD/notes,dst=/source/notes,readonly" \
        --mount "type=bind,src=$PWD/tools,dst=/source/tools,readonly" \
        --mount "type=bind,src=$PWD/requirements.txt,dst=/source/requirements.txt,readonly" \
        --tmpfs /tmp:rw,nosuid,nodev,size=64m \
        --env PYTHONPATH=/source --workdir /source --entrypoint python "$image" \
        -B -m unittest discover -s tests/repo-checks -t tests/repo-checks -v
}

unit_tests() {
    "${run[@]}" --network none "$image" -S -B -m unittest discover -s unit -t . -v
}

integration_tests() (
    volume=$("${docker[@]}" volume create)
    trap '"${docker[@]}" volume rm "$volume" >/dev/null' EXIT
    "${run[@]}" --mount "type=volume,src=$volume,dst=/trial" "$image" \
        -m unittest -v integration.test_wheelhouse.Online
    "${run[@]}" --network none --mount "type=volume,src=$volume,dst=/trial,readonly" "$image" \
        -m unittest -v integration.test_wheelhouse.Offline
)

case $lane in
    image) build_image ;;
    package) build_image; package ;;
    tests/repo-checks) build_image; repo_checks ;;
    tests/unit) build_image; package; unit_tests ;;
    tests/integration) build_image; package; integration_tests ;;
    test) build_image; repo_checks; package; unit_tests; integration_tests ;;
    publish/release) bash tools/publish.sh "$docker_host" ;;
    *) printf 'Unknown workflow: %s\n' "$lane" >&2; exit 2 ;;
esac
