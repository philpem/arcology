# Acorn Replay / ARMovie → MP4 transcoding

Arcology can transcode Acorn Replay (ARMovie, RISC OS filetype `&AE7`) videos
found inside disc-image extractions into browser-playable **MP4**, shown in an
HTML5 video player on the artefact viewer.

This is handled by the **`REPLAY_PROCESS`** analysis, which — on any extraction
that contains ARMovie files — parses each movie's header into searchable
metadata *and* transcodes its video, in a single pass. (Parsing and transcoding
were previously two analyses, `REPLAY_PROCESS` then `REPLAY_TRANSCODE`; they were
merged because both re-discovered and re-parsed the same files.)

## Pipeline

```
ARMovie (.rpl)
   │  scotch  replay-transcode --output-format nut
   │            (decode bitstream → NUT container: video + audio + geometry/fps)
   ▼
movie.nut
   │  ffmpeg -i movie.nut  (re-encode → H.264/AAC MP4, first frame → JPEG poster)
   ▼
MP4 + poster.jpg   →  saved as analysis output files
                       recorded on the ReplayMovie row (mp4_output_path, poster_path)
```

- **scotch** (`replay-transcode`, <https://github.com/philpem/scotch>) decodes
  the Replay bitstream and muxes the decoded video *and* audio into a single
  self-describing **NUT** container (`--output-format nut`). It is pure C11 +
  libm with a vendored ARM emulator and is compiled in the worker Docker image
  (`build-scotch` stage). The pinned `SCOTCH_VERSION` must be a revision with
  the NUT muxer — the worker now depends on it.
- **ffmpeg** reads the NUT stream and re-encodes it to MP4, and extracts the
  poster. `ffprobe` (shipped with ffmpeg) reports whether the muxed stream
  carries an audio track. Installed as a runtime package in the worker image.

The NUT container is **self-describing**: it carries the geometry, frame rate
and audio track, so ffmpeg is invoked with a plain `-i movie.nut` — the old
`-f rawvideo -pixel_format rgb24 -video_size WxH -framerate FPS` recipe and the
sidecar WAV are gone. scotch still converts every codec's working colour
(YUV555, 6Y5UV, RGB555, palette, …) to packed rgb24 internally before muxing,
so the colour-handling concerns that motivated *not* forcing `--video-colour`
(passing `--video-colour rgb888` read codec 7's YUV555 output as RGB888, zeroing
the blue channel → all-red frames) are now entirely inside scotch.

**Why a NUT *file*, not a stdout pipe.** Upstream documents
`replay-transcode --output-format nut | ffmpeg -i -`. The worker instead writes
the NUT to a scratch file (`--output movie.nut`) and feeds ffmpeg that path,
because Replay decodes are routinely multi-GB and the worker's subprocess
helper captures stdout into memory — piping would reintroduce the unbounded
in-RAM buffer the file-based intermediate exists to avoid (see the worker I/O
rules in `CLAUDE.md`). The on-disk NUT is comparable in size to the previous
raw-RGB24 intermediate, so the scratch footprint is unchanged.

**Sound-only** Replay files (video format 0) have no frames; they are transcoded
to an **M4A** (AAC) audio file and shown with an HTML5 `<audio>` player instead
of a video player.

### Poster image

Every ARMovie may embed a **poster sprite** — a standard RISC OS spritefile
(usually a title card) located by header lines 19/20 (`sprite_offset` /
`sprite_size`). `REPLAY_PROCESS` extracts it to PNG (`convert_replay_poster_sprite`
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
| Analysis handler (`process_replay` — parses + transcodes) | `worker/arcworker/analyses/metadata.py` |
| Module directory config | `REPLAY_MODULES_DIR` in `worker/arcworker/config.py` |
| Docker build of scotch + ffmpeg | `worker/Dockerfile` (`build-scotch` stage, runtime apt) |

## Web code map

| Concern | Location |
|---------|----------|
| `mp4_output_path` / `poster_path` columns | `ReplayMovie` in `myapp/database.py` |
| Search-index row (metadata + transcode outputs) | `handle_replay_movies` in `myapp/services/search_index.py` |
| Player card + download toolbar (media / poster / original) | `myapp/templates/artefacts/viewer.html` |
| Poster thumbnails + centred play/audio overlay, interleaved into the unified viewer grid by filename | `_viewer_replay_groups` (blueprint) + `replay_card` / `replay_thumb` macros in `viewer.html` |
| File-list / search icon: `bi-film` (video) vs `bi-music-note-beamed` (sound-only) | `file_viewer_metadata_icons` in `myapp/templates/_macros.html` |
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

## Output deduplication (content-addressed cache)

Transcoded outputs are **content-addressed on the source file's SHA-256**:
the MP4/poster live at `outputs/media/{source_sha256}/{tool_version}/` (see
`arcology_shared/transcode_paths.py`) and are linked by a shared, refcounted
`OutputBlob`. Two artefacts holding byte-identical source media therefore share
one stored output, and the worker skips ffmpeg on a cache hit
(`transcode_cached`). The shared bytes are reclaimed by the storage GC only when
the **last** referencing artefact is deleted.

`tool_version` is `MEDIA_TRANSCODE_TOOL_VERSION` (in `transcode_paths.py`, shared
by worker and web). Bump it to invalidate **every** cached transcode at once
(e.g. after changing ffmpeg flags/codecs): a new value routes transcodes to a
fresh namespace; the old outputs age out via GC.

**Legacy duplicates** (outputs produced before content-addressing) can be
collapsed onto shared blobs without re-encoding — see `dedup-transcode-outputs`
in `doc/ADMIN_COMMANDS.md`.

## Operating

- Parsing + transcoding are queued automatically after extraction (one
  `REPLAY_PROCESS` analysis per extraction with ARMovie files).

- **Re-transcoding a *bad* output is NOT a plain `reanalyse`.** Because outputs
  are cached on the source hash + tool version, re-running the analysis is a
  cache *hit* and re-serves the same (bad) bytes. Use `redo-transcode`, which
  invalidates the cached output first, then re-encodes:

  ```bash
  flask redo-transcode --artefact <artefact-uuid>      # invalidate + re-queue
  flask redo-transcode --source-hash <source-sha256>   # by source media hash
  flask redo-transcode --artefact <uuid> --no-reanalyse  # just clear the cache
  ```

  A plain `flask reanalyse --analysis <REPLAY_PROCESS-uuid>` re-parses and
  re-transcodes only when the cache is cold (or after a `tool_version` bump) —
  e.g. after changing `REPLAY_MODULES_DIR` for sources not yet cached. See
  `doc/ADMIN_COMMANDS.md` for the full flag reference.

- Dedicated transcode worker pool (transcoding is CPU-heavy):

  ```yaml
  environment:
    - WORKER_ANALYSIS_TYPES=REPLAY_PROCESS
  ```

- If transcoded rows look stale, refresh the index: `flask rebuild-search-index`.

## Future work

ffmpeg is now present in the worker, so additional output containers (AVI, etc.)
or codecs can be added by extending `transcode_armovie_to_mp4` / `process_replay`.
