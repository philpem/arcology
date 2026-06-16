# Worker analysis-pipeline integration tests

These tests run the **real** `AnalysisWorker` handlers and the **real** external
tools (`unzip`, `tar`, `7z`, …) against small committed fixtures, with only the
HTTP API boundary replaced by an in-memory fake server. They complement the
mocked unit tests in `ci/` (which never exercise the tools) by proving the
handler-plus-tool path end-to-end and locking the observable behaviour into
golden files.

They are **not** named `test_*.py`, so the app-tests job
(`unittest discover -p 'test_*.py'`) never runs them — they need the worker
container's tools. They run via their own workflow
(`.github/workflows/integration.yml`) and the runner below.

## Running

Inside the worker image (matches CI):

```bash
scripts/run-integration.sh           # build image + run, asserting goldens
scripts/run-integration.sh --regen   # build image + run, rewriting goldens
```

Directly (only if the required tools are on your PATH — the three archive
fixtures need just `unzip`, `tar`, `gzip`):

```bash
WORKER_API_KEY=integration-test \
  python3 ci/integration/run_integration.py -v
python3 ci/integration/run_integration.py --regen   # rewrite goldens
```

Set `ARCOLOGY_IT_STRICT=1` (CI does) to turn "tool missing → skip" into a hard
failure, so a broken image can never show green by skipping everything.

> **Regen ownership:** when regenerating via Docker, the rewritten goldens are
> owned by root (the container user). Fix with
> `sudo chown -R "$(id -u):$(id -g)" ci/integration/goldens`, or add
> `--user "$(id -u):$(id -g)"` to the `docker run` in `scripts/run-integration.sh`.

## How it works

- `harness/fake_api.py` — `FakeServerAPI` subclasses the real
  `arcworker.api.ArcologyAPI` and overrides only `_request_response`. Every real
  client method (hashing, storage writes, wire payloads) runs unchanged; only
  the **server** (Flask + DB) is simulated by an in-memory router. It also
  simulates the web app's auto-analysis scheduling (`IT_ANALYSIS_MAP`).
- `harness/driver.py` — seeds a fixture as the root artefact, then dispatches
  queued jobs whose type is in the manifest's `run_types` through the real
  `HANDLERS` until the queue drains. Jobs of other types are left queued and
  appear in the golden's `final_queue` (e.g. a promoted disc image's
  `partition_detect`).
- `harness/normalise.py` — strips temp paths, volatile fields (`modified_time`,
  `process_output`, …) and decodes JSON hints so two runs are byte-identical.
- `harness/runner.py` — runs a case, normalises, and asserts against (or
  regenerates) `goldens/<case>.expected.json`.

## Adding a fixture

1. Add a builder to `devtools/make_fixtures.py` (deterministic — fixed
   timestamps) and run `python3 devtools/make_fixtures.py <case>`. It writes the
   input binary and a `manifest.json` to `fixtures/<case>/`.
2. List the case (and the tools it needs) in the relevant `it_*.py` module.
3. Regenerate the golden (`--regen`), review the diff, and commit the fixture +
   manifest + golden.

`ANALYSIS_MAP` is duplicated in `harness/analysis_map.py` because the real one
imports Flask and cannot load in the worker container; `ci/test_integration_analysis_map.py`
(in the app-tests job) fails if the two drift.

## Current coverage

- `it_archive_extraction.py`:
  - `zip_plain` — top-level ZIP extraction **plus recursive nested-archive
    detection and extraction** (an inner `.zip` is detected, marked, extracted,
    and its contents registered under the parent's path with `parent_file_id`
    chaining and `extraction_depth` tracking).
  - `tar_gz` — gzip decompression + TAR extraction (the simple terminating case).
  - `zip_promote` — promotion of an extracted disc image to a derived
    `RAW_SECTOR` artefact, whose `PARTITION_DETECT` is queued (and appears in
    `final_queue` since it is not in this case's `run_types`).

Next iterations add partition/filesystem detection and container-built fixtures
(7z, FAT, MBR, ADFS); the harness is already generic over those.

<!-- vim: ts=4 sw=4 et -->
