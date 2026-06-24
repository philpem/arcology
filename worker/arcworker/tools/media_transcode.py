"""
Generic audio / video transcoding via ffmpeg.

Non-native media containers (AVI, QuickTime/MOV, MPEG-1/2, Matroska, WMV, …)
are not reliably playable in browsers, so MEDIA_TRANSCODE re-encodes them to a
universally-playable H.264/AAC MP4 (video) or AAC M4A (audio-only) with
``+faststart``.  Browser-native containers (MP4/WebM/MP3/…) never reach here —
they are streamed directly by the viewer.

``probe_media`` (ffprobe) reports container/codec/track metadata so the web
side can show the same kind of technical detail it shows for Acorn Replay
movies, and so the handler can decide video-vs-audio-only.

ffmpeg/ffprobe stream from the input path, so a multi-GB source is never read
into RAM.  Both binaries are expected on ``PATH`` (installed by the worker
Dockerfile).
"""

import json
from pathlib import Path
from .base import run_tool_with_output, tool_result


def _to_float(value) -> float | None:
    """Parse an ffprobe numeric field (possibly a ``"30000/1001"`` ratio)."""
    if value is None:
        return None
    try:
        if isinstance(value, str) and '/' in value:
            num, _, den = value.partition('/')
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(input_path: Path, *, timeout: int | None = None) -> dict:
    """Probe a media file with ffprobe.

    Returns a dict with ``success`` plus, on success, ``has_video``,
    ``has_audio``, ``container_format``, ``video_codec``, ``width``, ``height``,
    ``frame_rate``, ``audio_codec``, ``sample_rate``, ``channels`` and
    ``duration_seconds`` (any of which may be ``None`` when absent).  On failure
    returns ``{'success': False, 'error': ...}``.
    """
    cmd = [
        'ffprobe', '-v', 'quiet',
        '-print_format', 'json',
        '-show_format', '-show_streams',
        str(input_path),
    ]
    result, output = run_tool_with_output(cmd, timeout=timeout)
    if result.returncode != 0:
        return tool_result(
            False,
            tool='ffprobe',
            error=result.stderr.decode(errors='replace')[:1000] or 'ffprobe failed',
            process_output=output,
        )

    try:
        data = json.loads(result.stdout.decode(errors='replace') or '{}')
    except json.JSONDecodeError as e:
        return tool_result(False, tool='ffprobe', error=f'ffprobe output not JSON: {e}')

    fmt = data.get('format') or {}
    streams = data.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return tool_result(
        True,
        tool='ffprobe',
        has_video=video is not None,
        has_audio=audio is not None,
        container_format=fmt.get('format_name'),
        video_codec=video.get('codec_name') if video else None,
        width=_int(video.get('width')) if video else None,
        height=_int(video.get('height')) if video else None,
        frame_rate=_to_float(video.get('avg_frame_rate') or video.get('r_frame_rate')) if video else None,
        audio_codec=audio.get('codec_name') if audio else None,
        sample_rate=_int(audio.get('sample_rate')) if audio else None,
        channels=_int(audio.get('channels')) if audio else None,
        duration_seconds=_to_float(fmt.get('duration')),
    )


def extract_media_poster(
    input_path: Path,
    poster_path: Path,
    *,
    timeout: int | None = None,
) -> str | None:
    """Grab a first-frame JPEG poster from a video file.

    Used for *passthrough* video (played as-is, so there is no transcoded output
    to grab a frame from).  Best-effort: returns the saved poster path on
    success, or ``None`` (a poster is a nicety, never a reason to fail).
    """
    cmd = [
        'ffmpeg', '-y', '-hide_banner',
        '-i', str(input_path),
        '-frames:v', '1', '-q:v', '3',
        str(poster_path),
    ]
    result, _ = run_tool_with_output(cmd, timeout=timeout)
    if result.returncode == 0 and poster_path.exists():
        return str(poster_path)
    return None


def transcode_media_to_mp4(
    input_path: Path,
    output_path: Path,
    *,
    work_dir: Path,
    has_audio: bool = True,
    poster_path: Path | None = None,
    timeout: int | None = None,
) -> dict:
    """Transcode a video container to a browser-playable H.264/AAC MP4.

    Re-encodes (never stream-copies) so the output is guaranteed H.264/yuv420p
    regardless of the input codec.  Audio is mapped optionally — pass
    ``has_audio=False`` to skip the audio track entirely.  When *poster_path* is
    given, a first-frame JPEG is grabbed from the transcoded output.

    Returns a standard tool-result dict (``output_path``, ``output_type='mp4'``,
    ``has_audio``, ``poster_path``) on success, or ``error`` + ``stage`` on
    failure (``'transcode'`` or ``'poster'``).
    """
    cmd = [
        'ffmpeg', '-y', '-hide_banner',
        '-i', str(input_path),
        '-map', '0:v:0',
    ]
    if has_audio:
        cmd += ['-map', '0:a:0?']
    cmd += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
    if has_audio:
        cmd += ['-c:a', 'aac', '-b:a', '128k']
    cmd += [str(output_path)]

    result, output = run_tool_with_output(cmd, timeout=timeout)
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        return tool_result(
            False,
            tool='ffmpeg',
            error=result.stderr.decode(errors='replace')[:1000] or 'ffmpeg transcode failed',
            process_output=output,
            stage='transcode',
        )

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
        tool='ffmpeg',
        output_path=str(output_path),
        output_type='mp4',
        summary='Transcoded media to H.264/AAC MP4'
                + (' with audio' if has_audio else ' (no audio)'),
        process_output=output,
        has_audio=has_audio,
        poster_path=made_poster,
    )


def transcode_media_to_audio(
    input_path: Path,
    output_path: Path,
    *,
    work_dir: Path,
    timeout: int | None = None,
) -> dict:
    """Transcode an audio-only container to an M4A (AAC) file for ``<audio>``.

    Drops any (cover-art) video stream with ``-vn``.  Returns a standard
    tool-result dict (``output_path``, ``output_type='m4a'``, ``has_audio=True``,
    ``audio_only=True``) on success, or ``error`` + ``stage`` on failure.
    """
    cmd = [
        'ffmpeg', '-y', '-hide_banner',
        '-i', str(input_path),
        '-vn',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        str(output_path),
    ]
    result, output = run_tool_with_output(cmd, timeout=timeout)
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        return tool_result(
            False,
            tool='ffmpeg',
            error=result.stderr.decode(errors='replace')[:1000] or 'ffmpeg audio transcode failed',
            process_output=output,
            stage='transcode',
        )

    return tool_result(
        True,
        tool='ffmpeg',
        output_path=str(output_path),
        output_type='m4a',
        summary='Transcoded audio-only media to M4A',
        process_output=output,
        has_audio=True,
        audio_only=True,
        poster_path=None,
    )

# vim: ts=4 sw=4 et
