"""
Acorn Replay / ARMovie → MP4 transcoding.

Two-stage pipeline:

1. **scotch ``replay-transcode``** decodes the Replay bitstream to raw RGB24
   video frames (written to a file, not stdout, so there is no multi-GB memory
   buffer) plus an optional WAV audio track.  Compressed Replay codecs (Moving
   Lines, Moving Blocks, Super Moving Blocks, …) are decoded by running the
   original RISC OS decompressor module under scotch's ARM simulator; the module
   directory is supplied via ``--modules-dir``.  Codecs that need no module
   (e.g. type 23 raw, uncompressed) transcode without one.

2. **ffmpeg** muxes the raw frames (+ WAV) into an H.264/AAC MP4, and grabs the
   first frame as a JPEG poster thumbnail.

Both binaries are expected on ``PATH`` (installed by the worker Dockerfile).
"""

from pathlib import Path
from .base import run_tool_with_output, tool_result

# Fallback frame rate when the ARMovie header did not record one (rare; the
# field is usually present).  ffmpeg's rawvideo demuxer needs *some* rate.
_DEFAULT_FRAME_RATE = 25.0


def transcode_armovie_to_mp4(
    input_path: Path,
    output_path: Path,
    *,
    width: int | None,
    height: int | None,
    frame_rate: float | None,
    work_dir: Path,
    modules_dir: str | None = None,
    poster_path: Path | None = None,
    timeout: int | None = None,
) -> dict:
    """Transcode one ARMovie file to MP4 (+ optional poster thumbnail).

    Args:
        input_path: The ARMovie/.rpl file to decode.
        output_path: Destination ``.mp4`` path.
        width, height: Frame dimensions from the parsed ARMovie header; required
            (ffmpeg's rawvideo input needs an explicit size).
        frame_rate: Frames per second from the header (falls back to 25).
        work_dir: Scratch directory for the intermediate raw video / WAV.
        modules_dir: Optional RISC OS decompressor module directory.
        poster_path: Optional path for a first-frame JPEG poster.
        timeout: Per-subprocess timeout (seconds).

    Returns:
        Standard tool-result dict.  On success: ``output_path``, ``poster_path``
        (or None), ``has_audio``, ``width``/``height``/``frame_rate``.  On
        failure: ``error`` and a ``stage`` key ('decode' or 'mux').
    """
    if not width or not height:
        return tool_result(
            False,
            tool='replay-transcode',
            error='ARMovie header has no usable video dimensions; cannot transcode',
            stage='decode',
        )

    fps = frame_rate or _DEFAULT_FRAME_RATE
    raw_path = work_dir / f'{output_path.stem}.rgb'
    wav_path = work_dir / f'{output_path.stem}.wav'

    # ── Stage 1: Replay → raw RGB24 + WAV ──────────────────────────────────
    decode_cmd = [
        'replay-transcode',
        '--input', str(input_path),
        '--output', str(raw_path),
        '--audio-output', str(wav_path),
        '--video-colour', 'rgb888',   # force RGB24 so ffmpeg's input is predictable
        '--skip-unsupported',          # partial output instead of hard-failing
    ]
    if modules_dir:
        decode_cmd += ['--modules-dir', modules_dir]

    decode_result, decode_output = run_tool_with_output(decode_cmd, timeout=timeout)

    if decode_result.returncode != 0 or not raw_path.exists() or raw_path.stat().st_size == 0:
        err = decode_result.stderr.decode(errors='replace')[:1000]
        return tool_result(
            False,
            tool='replay-transcode',
            error=err or 'replay-transcode produced no video (codec unsupported or decompressor module missing)',
            process_output=decode_output,
            stage='decode',
        )

    has_audio = wav_path.exists() and wav_path.stat().st_size > 0

    # ── Stage 2: raw RGB24 (+ WAV) → MP4 ───────────────────────────────────
    mux_cmd = [
        'ffmpeg', '-y', '-hide_banner',
        '-f', 'rawvideo', '-pixel_format', 'rgb24',
        '-video_size', f'{width}x{height}',
        '-framerate', f'{fps:g}',
        '-i', str(raw_path),
    ]
    if has_audio:
        mux_cmd += ['-i', str(wav_path)]
    mux_cmd += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
    if has_audio:
        mux_cmd += ['-c:a', 'aac', '-b:a', '128k']
    mux_cmd += [str(output_path)]

    mux_result, mux_output = run_tool_with_output(mux_cmd, timeout=timeout)

    if mux_result.returncode != 0 or not output_path.exists():
        return tool_result(
            False,
            tool='ffmpeg',
            error=mux_result.stderr.decode(errors='replace')[:1000],
            process_output=mux_output,
            stage='mux',
        )

    # ── Stage 3 (optional): first-frame poster thumbnail ───────────────────
    made_poster: str | None = None
    if poster_path is not None:
        poster_cmd = [
            'ffmpeg', '-y', '-hide_banner',
            '-i', str(output_path),
            '-frames:v', '1', '-q:v', '3',
            str(poster_path),
        ]
        poster_result, _ = run_tool_with_output(poster_cmd, timeout=timeout)
        if poster_result.returncode == 0 and poster_path.exists():
            made_poster = str(poster_path)

    return tool_result(
        True,
        tool='replay-transcode,ffmpeg',
        output_path=str(output_path),
        output_type='mp4',
        summary=f'Transcoded {width}×{height} ARMovie to MP4'
                + (' with audio' if has_audio else ' (no audio)'),
        process_output={'decode': decode_output, 'mux': mux_output},
        has_audio=has_audio,
        poster_path=made_poster,
        width=width,
        height=height,
        frame_rate=fps,
    )

# vim: ts=4 sw=4 et
