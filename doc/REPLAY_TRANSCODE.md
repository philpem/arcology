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

`replay-transcode` **always outputs plain packed `rgb24`** — every codec's
working colour (YUV555, 6Y5UV, RGB555, palette, …) is converted to rgb24
internally — so the rawvideo input we hand ffmpeg is always `-pixel_format
rgb24`. We deliberately do **not** force `--video-colour`: passing
`--video-colour rgb888` told the transcoder to read codec 7's YUV555 working
output as RGB888, zeroing the blue channel and producing all-red frames. (Note
the transcoder's printed recipe also contains a libx264 *output* `-pix_fmt
yuv420p`; that is the encode format and must not be applied to the rawvideo
input.)

**Sound-only** Replay files (video format 0) have no frames; they are transcoded
to an **M4A** (AAC) audio file and shown with an HTML5 `<audio>` player instead
of a video player.

### Poster image

Every ARMovie may embed a **poster sprite** — a standard RISC OS spritefile
(usually a title card) located by header lines 19/20 (`sprite_offset` /
`sprite_size`). `REPLAY_TRANSCODE` extracts it to PNG (`convert_replay_poster_sprite`
in `worker/arcworker/tools/images_acorn.py`, via the bundled `spritefile` lib)
and uses it as the movie's poster:

- **Video** movies prefer the embedded poster sprite as the thumbnail/`<video poster>`,
  falling back to ffmpeg's first decoded frame only when there is no poster sprite.
- **Sound-only** movies have no first frame, so the poster sprite is the only
  image — it is shown above the `<audio>` player and as the grid thumbnail.

Extraction is best-effort: a missing/unreadable sprite simply leaves the poster
unset (no transcode failure).

Both run as best-effort, per movie: a file that cannot be decoded (see
*Decompressor modules* below) is recorded as a transcode error and skipped, and
its parsed metadata is left untouched.

## Worker code map

| Concern | Location |
|---------|----------|
| Tool wrapper (`transcode_armovie_to_mp4`) | `worker/arcworker/tools/replay_transcode.py` |
| Poster-sprite extractor (`convert_replay_poster_sprite`) | `worker/arcworker/tools/images_acorn.py` |
| Analysis handler (`process_replay_transcode`) | `worker/arcworker/analyses/metadata.py` |
| Queued after metadata parse | end of `process_replay` (same file) |
| Module directory config | `REPLAY_MODULES_DIR` in `worker/arcworker/config.py` |
| Docker build of scotch + ffmpeg | `worker/Dockerfile` (`build-scotch` stage, runtime apt) |

## Web code map

| Concern | Location |
|---------|----------|
| `mp4_output_path` / `poster_path` columns | `ReplayMovie` in `myapp/database.py` |
| Search-index update | `handle_replay_transcode` in `myapp/services/search_index.py` |
| Player card + download toolbar (media / poster / original) | `myapp/templates/artefacts/viewer.html` |
| Poster thumbnails + media-type badge (Movie/Sound) in the viewer grid | `_viewer_replay_posters` + `viewer.html` |
| MP4 / poster / original-file URLs | `_viewer_replay_detail` in `myapp/blueprints/artefacts.py` |
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
