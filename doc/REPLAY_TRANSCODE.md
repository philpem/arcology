# Acorn Replay / ARMovie → MP4 transcoding

Arcology can transcode Acorn Replay (ARMovie, RISC OS filetype `&AE7`) videos
found inside disc-image extractions into browser-playable **MP4**, shown in an
HTML5 video player on the artefact viewer.

This is handled by the **`REPLAY_TRANSCODE`** analysis, which runs *after* the
`REPLAY_PROCESS` metadata parse on any extraction that contains ARMovie files.

## Pipeline

```
ARMovie (.rpl)
   │  scotch  replay-transcode  (decode bitstream → raw RGB24 frames + WAV)
   ▼
raw RGB24 + WAV
   │  ffmpeg  (mux → H.264/AAC MP4, first frame → JPEG poster)
   ▼
MP4 + poster.jpg   →  saved as analysis output files
                       recorded on the ReplayMovie row (mp4_output_path, poster_path)
```

- **scotch** (`replay-transcode`, <https://github.com/philpem/scotch>) decodes
  the Replay bitstream. It is pure C11 + libm with a vendored ARM emulator and
  is compiled in the worker Docker image (`build-scotch` stage).
- **ffmpeg** muxes the decoded frames/audio into MP4 and extracts the poster.
  Installed as a runtime package in the worker image.

The raw video's **pixel format is taken from the ffmpeg recipe scotch prints**
(rather than assuming RGB), so codecs whose native colour isn't packed RGB don't
come out colour-corrupted (e.g. all-red).

**Sound-only** Replay files (video format 0) have no frames; they are transcoded
to an **M4A** (AAC) audio file and shown with an HTML5 `<audio>` player instead
of a video player.

Both run as best-effort, per movie: a file that cannot be decoded (see
*Decompressor modules* below) is recorded as a transcode error and skipped, and
its parsed metadata is left untouched.

## Worker code map

| Concern | Location |
|---------|----------|
| Tool wrapper (`transcode_armovie_to_mp4`) | `worker/arcworker/tools/replay_transcode.py` |
| Analysis handler (`process_replay_transcode`) | `worker/arcworker/analyses/metadata.py` |
| Queued after metadata parse | end of `process_replay` (same file) |
| Module directory config | `REPLAY_MODULES_DIR` in `worker/arcworker/config.py` |
| Docker build of scotch + ffmpeg | `worker/Dockerfile` (`build-scotch` stage, runtime apt) |

## Web code map

| Concern | Location |
|---------|----------|
| `mp4_output_path` / `poster_path` columns | `ReplayMovie` in `myapp/database.py` |
| Search-index update | `handle_replay_transcode` in `myapp/services/search_index.py` |
| Player card + download button | `myapp/templates/artefacts/viewer.html` |
| Poster thumbnails in the viewer grid | `_viewer_replay_posters` + `viewer.html` |
| MP4/poster URLs | `_viewer_replay_detail` in `myapp/blueprints/artefacts.py` |
| `.mp4` MIME registration | `arcology_shared/storage.py` |

The MP4 and poster are **analysis output files** (served by `get_output_file`,
gated by the artefact's visibility/restrictions), *not* derived artefacts — so
they never appear as entries in the file listing or the derived-artefacts
sidebar. Each transcoded movie shows as a poster thumbnail in the viewer that
links to its player card (`viewer?file=<path>`), where the MP4 plays inline and
a **Download converted movie (MP4)** button is offered.

## Decompressor modules (compressed codecs)

Most Replay videos use a **compressed** codec (Moving Lines = 1, Moving Blocks =
7/17/20, Super Moving Blocks = 19, …). scotch decodes these by running the
original RISC OS decompressor module (`Decompress,ffd`) under its ARM simulator.

These modules are **freeware and are bundled** with the worker image, copied
from scotch's `vendor/armovie-codecs/` (layout `DecompNN/Decompress,ffd` +
`Info`, plus `MovingLine` for codec 1 and third-party Escape/LinePack codecs).
So compressed codecs transcode **out of the box** — no manual setup.

Licensing of the bundled modules (per scotch's `vendor/armovie-codecs/README.md`):

- Acorn codecs: distributed by Acorn as freeware, now open source via **RISC OS
  Open Ltd**; taken unmodified from a compiled RISC OS 2003 ARMovie build.
- Escape codec: freeware, © **Eidos plc 1993**.
- LinePack codec: freeware, © **Henrik Bjerregaard Pedersen 1995**.

The image sets `REPLAY_MODULES_DIR=/usr/local/share/armovie-codecs`. Override it
to point at a different module set (e.g. a mounted directory) if needed. A movie
whose codec still cannot be decoded is recorded in `transcode_errors` (stage
`decode`) and the viewer shows *"Video preview not yet available (transcode
pending or codec unsupported)."*

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `REPLAY_MODULES_DIR` | `/usr/local/share/armovie-codecs` (bundled) | Directory of RISC OS Replay decompressor modules passed to `replay-transcode --modules-dir`. The worker only passes it when the directory exists, so running outside the Docker image (where it is absent) simply limits transcoding to module-free codecs. |

`TOOL_TIMEOUT` (default 3600 s) bounds each transcode subprocess — raise it for
long movies.

## Operating

- Transcoding is queued automatically after extraction (via `REPLAY_PROCESS`).
- To re-transcode (e.g. after changing `REPLAY_MODULES_DIR`), re-run the analysis:

  ```bash
  flask reanalyse --analysis <REPLAY_TRANSCODE-uuid>
  # or re-run the parse, which re-queues the transcode:
  flask reanalyse --analysis <REPLAY_PROCESS-uuid>
  ```

- Dedicated transcode worker pool (transcoding is CPU-heavy):

  ```yaml
  environment:
    - WORKER_ANALYSIS_TYPES=REPLAY_TRANSCODE
  ```

- If transcoded rows look stale, refresh the index: `flask rebuild-search-index`.

## Future work

ffmpeg is now present in the worker, so additional output containers (AVI, etc.)
or codecs can be added by extending `transcode_armovie_to_mp4` /
`process_replay_transcode`.
