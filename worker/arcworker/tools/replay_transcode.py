"""
Acorn Replay / ARMovie → MP4 transcoding.

Two-stage pipeline:

1. **scotch ``replay-transcode``** decodes the Replay bitstream and muxes the
   decoded video *and* audio into a single self-describing **NUT** container
   (``--output-format nut``), written to a file (not stdout, so there is no
   multi-GB memory buffer).  The NUT stream carries the geometry, frame rate and
   audio track itself, so ffmpeg no longer needs the ``-f rawvideo
   -pixel_format -video_size -framerate`` recipe nor a sidecar WAV.  Compressed
   Replay codecs (Moving Lines, Moving Blocks, Super Moving Blocks, …) and
   MovieFS-wrapped PC codecs (Cinepak, …) are decoded by running the original
   RISC OS decompressor module under scotch's ARM simulator; the module
   directory is supplied via ``--modules-dir``.  Codecs that need no module
   transcode without one.

2. **ffmpeg** reads the NUT stream (``-i movie.nut``) and re-encodes it to an
   H.264/AAC MP4, then grabs the first frame as a JPEG poster thumbnail.

Both binaries are expected on ``PATH`` (installed by the worker Dockerfile);
``ffprobe`` (shipped with ffmpeg) is used to report whether the muxed stream
carries an audio track.
"""

from pathlib import Path
from .base import run_tool_with_output, tool_result
from .media_transcode import probe_media

# Fallback frame rate reported when the ARMovie header did not record one (rare;
# the field is usually present).  Purely informational now — NUT carries the
# real frame rate, so it is no longer handed to ffmpeg.
_DEFAULT_FRAME_RATE = 25.0


def _decode_to_nut(
    input_path: Path,
    nut_path: Path,
    *,
    modules_dir: str | None,
    timeout: int | None,
) -> tuple[bool, str | None, dict]:
    """Run ``replay-transcode`` to mux the movie into a NUT container file.

    Returns ``(ok, error, process_output)``.  ``ok`` is True only when the
    subprocess succeeded *and* produced a non-empty NUT file.
    """
    decode_cmd = [
        'replay-transcode',
        '--input', str(input_path),
        '--output', str(nut_path),
        '--output-format', 'nut',      # mux video + audio into one NUT stream
        '--skip-unsupported',          # partial output instead of hard-failing
    ]
    if modules_dir:
        decode_cmd += ['--modules-dir', modules_dir]

    result, output = run_tool_with_output(decode_cmd, timeout=timeout)
    if result.returncode != 0 or not nut_path.exists() or nut_path.stat().st_size == 0:
        err = result.stderr.decode(errors='replace')[:1000]
        return False, (err or 'replay-transcode produced no output '
                       '(codec unsupported or decompressor module missing)'), output
    return True, None, output


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
        width, height: Frame dimensions from the parsed ARMovie header.  No
            longer fed to ffmpeg (the NUT stream is self-describing) but still
            sanity-checked and echoed back in the result.
        frame_rate: Frames per second from the header (reported; falls back to
            25 only for the echoed value).
        work_dir: Scratch directory for the intermediate NUT container.
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
    nut_path = work_dir / f'{output_path.stem}.nut'

    # ── Stage 1: Replay → NUT (video + audio muxed) ────────────────────────
    ok, err, decode_output = _decode_to_nut(
        input_path, nut_path, modules_dir=modules_dir, timeout=timeout,
    )
    if not ok:
        return tool_result(
            False,
            tool='replay-transcode',
            error=err,
            process_output=decode_output,
            stage='decode',
        )

    # Does the muxed stream carry audio?  (Informational; also decides whether
    # we ask ffmpeg to encode an audio track.)
    probe = probe_media(nut_path, timeout=timeout)
    has_audio = bool(probe.get('has_audio')) if probe.get('success') else False

    # ── Stage 2: NUT → MP4 ─────────────────────────────────────────────────
    mux_cmd = [
        'ffmpeg', '-y', '-hide_banner',
        '-i', str(nut_path),
        '-map', '0:v:0',
    ]
    if has_audio:
        mux_cmd += ['-map', '0:a:0?']
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


def transcode_armovie_to_audio(
    input_path: Path,
    output_path: Path,
    *,
    work_dir: Path,
    modules_dir: str | None = None,
    timeout: int | None = None,
) -> dict:
    """Transcode a sound-only ARMovie (video_format 0) to an M4A (AAC) file.

    Sound-only Replay files have no video frames but should still be playable
    (audio only). scotch muxes the audio track into a NUT container; ffmpeg
    encodes it to M4A for an HTML5 ``<audio>`` player.

    Returns a standard tool-result dict (``output_path``, ``has_audio=True``,
    ``audio_only=True``) on success, or ``error`` + ``stage`` on failure.
    """
    nut_path = work_dir / f'{output_path.stem}.nut'

    # ── Stage 1: Replay → NUT (audio only for sound-only movies) ───────────
    ok, err, decode_output = _decode_to_nut(
        input_path, nut_path, modules_dir=modules_dir, timeout=timeout,
    )
    if not ok:
        return tool_result(
            False,
            tool='replay-transcode',
            error=err or 'replay-transcode produced no audio',
            process_output=decode_output,
            stage='decode',
        )

    # The stream must actually carry audio, otherwise there is nothing to play.
    probe = probe_media(nut_path, timeout=timeout)
    if not (probe.get('success') and probe.get('has_audio')):
        return tool_result(
            False,
            tool='replay-transcode',
            error='replay-transcode produced no audio',
            process_output=decode_output,
            stage='decode',
        )

    # ── Stage 2: NUT → M4A ─────────────────────────────────────────────────
    enc_cmd = [
        'ffmpeg', '-y', '-hide_banner',
        '-i', str(nut_path),
        '-vn',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        str(output_path),
    ]
    enc_result, enc_output = run_tool_with_output(enc_cmd, timeout=timeout)

    if enc_result.returncode != 0 or not output_path.exists():
        return tool_result(
            False,
            tool='ffmpeg',
            error=enc_result.stderr.decode(errors='replace')[:1000],
            process_output=enc_output,
            stage='mux',
        )

    return tool_result(
        True,
        tool='replay-transcode,ffmpeg',
        output_path=str(output_path),
        output_type='m4a',
        summary='Transcoded sound-only ARMovie to M4A audio',
        process_output={'decode': decode_output, 'encode': enc_output},
        has_audio=True,
        audio_only=True,
        poster_path=None,
    )

# vim: ts=4 sw=4 et
