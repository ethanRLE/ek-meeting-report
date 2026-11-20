"""Extract and compress meeting audio for the OpenAI Transcriptions API (25 MB limit)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_DIARIZE_SECONDS = 1400.0
DEFAULT_DIARIZE_CHUNK_SECONDS = 1200.0
_BITRATE_LADDER_K = (64, 48, 32, 24)


class AudioPrepError(Exception):
    """ffmpeg/ffprobe audio preparation failed."""


@dataclass(frozen=True)
class PreparedAudio:
    path: Path
    source_path: Path
    duration_seconds: float | None
    size_bytes: int
    bitrate_k: int | None


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise AudioPrepError(
            "ffmpeg and ffprobe must be on PATH to prepare meeting audio for diarization"
        )


def ffprobe_duration(path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return float(out) if out else None
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def _ffmpeg_extract_mp3(source: Path, dest: Path, *, bitrate_k: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{bitrate_k}k",
        str(dest),
    ]
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        raise AudioPrepError(
            f"ffmpeg failed extracting audio from {source.name} at {bitrate_k}k"
        ) from exc
    if not dest.is_file() or dest.stat().st_size == 0:
        raise AudioPrepError(f"ffmpeg produced empty audio at {dest}")


def prepare_audio_for_upload(
    source: Path,
    dest_dir: Path,
    *,
    dest_name: str = "audio_for_api.mp3",
) -> PreparedAudio:
    require_ffmpeg()
    source = source.expanduser().resolve()
    if not source.is_file():
        raise AudioPrepError(f"Audio/video file not found: {source}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / dest_name
    last_size = 0
    used_bitrate: int | None = None

    # Only real MP3s skip conversion: the upload is named .mp3, so any other format must be re-encoded.
    if source.suffix.lower() == ".mp3":
        size = source.stat().st_size
        if size <= MAX_UPLOAD_BYTES:
            if source.resolve() != dest.resolve():
                shutil.copy2(source, dest)
            else:
                dest = source
            return PreparedAudio(
                path=dest,
                source_path=source,
                duration_seconds=ffprobe_duration(dest),
                size_bytes=dest.stat().st_size,
                bitrate_k=None,
            )

    for bitrate_k in _BITRATE_LADDER_K:
        logger.info("Extracting mono MP3 at %sk from %s…", bitrate_k, source.name)
        _ffmpeg_extract_mp3(source, dest, bitrate_k=bitrate_k)
        last_size = dest.stat().st_size
        used_bitrate = bitrate_k
        duration = ffprobe_duration(dest)
        # Long audio is split into chunks before upload, so only each chunk has to fit the limit.
        if last_size <= MAX_UPLOAD_BYTES or (duration or 0) > MAX_DIARIZE_SECONDS:
            return PreparedAudio(
                path=dest,
                source_path=source,
                duration_seconds=duration,
                size_bytes=last_size,
                bitrate_k=used_bitrate,
            )

    raise AudioPrepError(
        f"Prepared audio still exceeds OpenAI 25 MB limit after compression "
        f"({last_size} bytes)."
    )


def split_audio_for_diarize(
    audio: Path,
    chunk_dir: Path,
    *,
    chunk_seconds: float = DEFAULT_DIARIZE_CHUNK_SECONDS,
    max_seconds: float = MAX_DIARIZE_SECONDS,
) -> list[Path]:
    require_ffmpeg()
    audio = audio.expanduser().resolve()
    if not audio.is_file():
        raise AudioPrepError(f"Audio file not found: {audio}")

    duration = ffprobe_duration(audio)
    if duration is None:
        logger.warning(
            "Could not probe duration for %s; uploading as a single file",
            audio.name,
        )
        return [audio]
    if duration <= max_seconds:
        return [audio]

    chunk_dir.mkdir(parents=True, exist_ok=True)
    for old in chunk_dir.glob("diarize_chunk_*.mp3"):
        old.unlink(missing_ok=True)

    seg = max(60.0, min(float(chunk_seconds), max_seconds - 30.0))
    pattern = chunk_dir / "diarize_chunk_%03d.mp3"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(audio),
        "-f",
        "segment",
        "-segment_time",
        str(int(seg)),
        "-reset_timestamps",
        "1",
        "-c",
        "copy",
        str(pattern),
    ]
    logger.info(
        "Splitting %s (%.1fs) into ~%.0fs segments for diarize duration limit…",
        audio.name,
        duration,
        seg,
    )
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        raise AudioPrepError(
            f"ffmpeg failed splitting {audio.name} for diarize duration limit"
        ) from exc

    chunks = sorted(chunk_dir.glob("diarize_chunk_*.mp3"))
    if not chunks:
        raise AudioPrepError(f"ffmpeg produced no diarize chunks in {chunk_dir}")

    fixed: list[Path] = []
    for i, chunk in enumerate(chunks):
        d = ffprobe_duration(chunk)
        if d is not None and d > max_seconds:
            tighter = chunk_dir / f"diarize_chunk_{i:03d}_trim.mp3"
            try:
                subprocess.check_call(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(chunk),
                        "-t",
                        str(max_seconds - 5),
                        "-c",
                        "copy",
                        str(tighter),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError as exc:
                raise AudioPrepError(
                    f"ffmpeg failed trimming overlong chunk {chunk.name}"
                ) from exc
            chunk.unlink(missing_ok=True)
            fixed.append(tighter)
        else:
            fixed.append(chunk)
    return fixed
