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
*original RISC OS decompressor module* (`Decompress,ffd`) under its ARM
simulator. **Those modules are proprietary Acorn / third-party code and are not
redistributable**, so Arcology does not ship them.

> Bundling was considered (per the feature request) but the Replay decompressor
> modules are not freely licensed, so they cannot be included in the image.

To transcode compressed codecs, provide your own module directory:

1. Place the decompressor modules in a directory, e.g. `./data/replay-modules`.
2. The directory is mounted into the worker and pointed at by
   `REPLAY_MODULES_DIR` (already wired in `docker-compose.yml`).

Without modules, only codecs that need none — notably **type 23** (raw
6Y6Y5U5V) and uncompressed movies — can be transcoded; everything else is
recorded as `transcode_errors` with stage `decode` and the viewer shows
*"Video preview not yet available (transcode pending or codec unsupported)."*

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `REPLAY_MODULES_DIR` | *(unset)* | Directory of RISC OS Replay decompressor modules passed to `replay-transcode --modules-dir`. Unset ⇒ module-free codecs only. |

`TOOL_TIMEOUT` (default 3600 s) bounds each transcode subprocess — raise it for
long movies.

## Operating

- Transcoding is queued automatically after extraction (via `REPLAY_PROCESS`).
- To (re-)transcode after adding decompressor modules, re-run the analysis:

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

- After importing modules and re-running transcodes, refresh the index if rows
  look stale: `flask rebuild-search-index`.

## Future work

ffmpeg is now present in the worker, so additional output containers (AVI, etc.)
or codecs can be added by extending `transcode_armovie_to_mp4` /
`process_replay_transcode`.
