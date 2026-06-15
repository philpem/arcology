#!/usr/bin/env bash
#
# Build the worker image and run the analysis-pipeline integration suite inside
# it, mirroring what .github/workflows/integration.yml does in CI.
#
# Usage:
#   scripts/run-integration.sh            # build + run (asserts goldens)
#   scripts/run-integration.sh --regen    # build + run, rewriting goldens
#
# Notes:
#   * The test code and fixtures are mounted from the working tree, not baked
#     into the image — only the external tools come from the image.
#   * In --regen mode the rewritten goldens will be owned by root (the container
#     user).  Run `sudo chown -R "$(id -u):$(id -g)" ci/integration/goldens`
#     afterwards, or pass --user to the docker run below.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="arcology-worker:it"

REGEN=0
if [[ "${1:-}" == "--regen" ]]; then
    REGEN=1
fi

echo ">>> Building worker image ($IMAGE)..."
docker build -f "$REPO_ROOT/worker/Dockerfile" -t "$IMAGE" "$REPO_ROOT"

RUN_ENV=(-e WORKER_API_KEY=integration-test)
if [[ "$REGEN" == "1" ]]; then
    echo ">>> Running integration suite in REGEN mode..."
    RUN_ENV+=(-e ARCOLOGY_IT_REGEN=1)
else
    echo ">>> Running integration suite (strict)..."
    RUN_ENV+=(-e ARCOLOGY_IT_STRICT=1)
fi

docker run --rm \
    -v "$REPO_ROOT:/repo" \
    -w /repo \
    "${RUN_ENV[@]}" \
    --entrypoint python3 \
    "$IMAGE" \
    ci/integration/run_integration.py -v

# vim: ts=4 sw=4 et
