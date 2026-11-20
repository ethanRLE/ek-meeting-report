"""OpenAI gpt-4o-transcribe-diarize for speaker-labeled meeting transcripts."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from briefing.audio_prep import (
    DEFAULT_DIARIZE_CHUNK_SECONDS,
    MAX_DIARIZE_SECONDS,
    PreparedAudio,
    ffprobe_duration,
    prepare_audio_for_upload,
    split_audio_for_diarize,
)
import os

logger = logging.getLogger(__name__)

DIARIZE_MODEL = "gpt-4o-transcribe-diarize"
MAX_KNOWN_SPEAKERS = 4
DIARIZE_TIMEOUT_SECONDS = 1800.0
DIARIZE_MAX_RETRIES = 1


class DiarizeError(Exception):
    """OpenAI diarization failed."""


@dataclass(frozen=True)
class KnownSpeaker:
    name: str
    reference_path: Path


@dataclass
class DiarizeResult:
    plain: str
    raw: dict[str, Any]
    audio: PreparedAudio
    model: str
    known_speakers: list[str] = field(default_factory=list)
    diarized_json_path: Path | None = None
    plain_path: Path | None = None


def parse_known_speaker_arg(raw: str) -> KnownSpeaker:
    if "=" not in raw:
        raise DiarizeError(
            f"Invalid --known-speaker {raw!r}; expected Name=/path/to/clip.wav"
        )
    name, path_s = raw.split("=", 1)
    name = name.strip()
    path = Path(path_s.strip()).expanduser()
    if not name:
        raise DiarizeError("Known speaker name must not be empty")
    if not path.is_file():
        raise DiarizeError(f"Known speaker reference not found: {path}")
    return KnownSpeaker(name=name, reference_path=path.resolve())


def _mime_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    ext = path.suffix.lower()
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
    }.get(ext, "application/octet-stream")


def _to_data_url(path: Path) -> str:
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{_mime_for(path)};base64,{b64}"


def _result_to_dict(result: object) -> dict[str, Any]:
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        try:
            dumped = to_dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if isinstance(result, dict):
        return result
    out: dict[str, Any] = {}
    text = getattr(result, "text", None)
    if isinstance(text, str):
        out["text"] = text
    segments = getattr(result, "segments", None)
    if segments is not None:
        out["segments"] = []
        for seg in segments:
            if isinstance(seg, dict):
                out["segments"].append(seg)
            else:
                out["segments"].append(
                    {
                        "speaker": getattr(seg, "speaker", None),
                        "text": getattr(seg, "text", None),
                        "start": getattr(seg, "start", None),
                        "end": getattr(seg, "end", None),
                    }
                )
    return out


def segments_to_plain(
    segments: list[dict[str, Any]] | list[Any],
    *,
    merge_same_speaker: bool = True,
) -> str:
    lines: list[str] = []
    cur_speaker: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, cur_speaker
        if not buf:
            return
        label = cur_speaker or "Speaker"
        lines.append(f"{label}: {' '.join(buf)}")
        buf = []

    for seg in segments:
        if isinstance(seg, dict):
            speaker = seg.get("speaker")
            text = seg.get("text")
        else:
            speaker = getattr(seg, "speaker", None)
            text = getattr(seg, "text", None)
        uttered = str(text or "").strip()
        if not uttered:
            continue
        sp = str(speaker).strip() if speaker is not None else None
        if not merge_same_speaker:
            flush()
            cur_speaker = sp
            buf = [uttered]
            flush()
            continue
        if sp != cur_speaker and buf:
            flush()
        cur_speaker = sp
        buf.append(uttered)
    flush()
    return "\n\n".join(lines)


def diarized_payload_to_plain(payload: dict[str, Any]) -> str:
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        return segments_to_plain(segments)
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return ""


def _offset_segments(
    segments: list[dict[str, Any]],
    *,
    time_offset: float,
    speaker_prefix: str | None,
    known_names: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments:
        row = dict(seg)
        for key in ("start", "end"):
            val = row.get(key)
            if isinstance(val, (int, float)):
                row[key] = float(val) + time_offset
        if speaker_prefix:
            sp = row.get("speaker")
            # Named speakers are the same person in every chunk; letter labels restart per chunk.
            if sp is not None and str(sp).strip() and str(sp) not in known_names:
                row["speaker"] = f"{speaker_prefix}{sp}"
        out.append(row)
    return out


def _call_diarize_api(
    client: Any,
    audio_path: Path,
    *,
    model: str,
    known: list[KnownSpeaker],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "response_format": "diarized_json",
        "chunking_strategy": "auto",
    }
    extra_body: dict[str, Any] = {}
    if known:
        names = [k.name for k in known]
        refs = [_to_data_url(k.reference_path) for k in known]
        kwargs["known_speaker_names"] = names
        kwargs["known_speaker_references"] = refs
        extra_body["known_speaker_names"] = names
        extra_body["known_speaker_references"] = refs

    logger.info(
        "Diarizing %s with %s (size=%s)…",
        audio_path.name,
        model,
        audio_path.stat().st_size,
    )
    try:
        with audio_path.open("rb") as audio_file:
            call_kwargs = dict(kwargs)
            call_kwargs["file"] = audio_file
            try:
                result = client.audio.transcriptions.create(**call_kwargs)
            except TypeError:
                call_kwargs.pop("known_speaker_names", None)
                call_kwargs.pop("known_speaker_references", None)
                if extra_body:
                    call_kwargs["extra_body"] = extra_body
                audio_file.seek(0)
                result = client.audio.transcriptions.create(**call_kwargs)
    except Exception as exc:
        raise DiarizeError(
            f"OpenAI diarization failed: {type(exc).__name__}: {exc}"
        ) from exc
    return _result_to_dict(result)


MAX_PARALLEL_CHUNKS = 6


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _diarize_chunks(
    client: Any,
    chunks: list[Path],
    *,
    model: str,
    known: list[KnownSpeaker],
) -> list[dict[str, Any]]:
    """Diarize all chunks at once; save each result beside its chunk so a rerun only pays for failures."""
    if len(chunks) == 1:
        return [_call_diarize_api(client, chunks[0], model=model, known=known)]

    results: list[dict[str, Any] | None] = [None] * len(chunks)
    hashes = [_sha256(c) for c in chunks]
    for i, chunk in enumerate(chunks):
        saved = chunk.with_suffix(".json")
        if saved.is_file():
            try:
                data = json.loads(saved.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            # Same chunk bytes only: a different recording split the same way must not reuse it.
            if data.get("sha256") == hashes[i] and isinstance(data.get("result"), dict):
                results[i] = data["result"]
                logger.info("Reusing saved diarize result for %s", chunk.name)

    def run(i: int) -> dict[str, Any]:
        raw = _call_diarize_api(client, chunks[i], model=model, known=known)
        chunks[i].with_suffix(".json").write_text(
            json.dumps({"sha256": hashes[i], "result": raw}) + "\n", encoding="utf-8"
        )
        return raw

    pending = [i for i, r in enumerate(results) if r is None]
    errors: dict[int, DiarizeError] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=min(len(pending), MAX_PARALLEL_CHUNKS)) as pool:
            futures = {pool.submit(run, i): i for i in pending}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except DiarizeError as exc:
                    errors[i] = exc
    if errors:
        first = min(errors)
        raise DiarizeError(
            f"Chunk {first + 1}/{len(chunks)} failed ({len(errors)} failed in total; "
            f"finished chunks are saved and reused on rerun): {errors[first]}"
        ) from errors[first]
    return [r for r in results if r is not None]


def _remove_intermediate_audio(source: Path, prepared: Path, chunk_dir: Path) -> None:
    """Drop upload-prep audio once plain.txt/diarized.json exist; the source recording stays."""
    if chunk_dir.is_dir():
        shutil.rmtree(chunk_dir, ignore_errors=True)
    if prepared.resolve() != source.expanduser().resolve():
        prepared.unlink(missing_ok=True)


def diarize_audio(
    source: Path,
    dest_dir: Path,
    *,
    known_speakers: list[KnownSpeaker] | None = None,
    model: str = DIARIZE_MODEL,
    client: Any | None = None,
    keep_intermediate: bool = False,
    chunk_seconds: float = DEFAULT_DIARIZE_CHUNK_SECONDS,
) -> DiarizeResult:
    known = list(known_speakers or [])
    if len(known) > MAX_KNOWN_SPEAKERS:
        raise DiarizeError(
            f"At most {MAX_KNOWN_SPEAKERS} known speakers are supported "
            f"(got {len(known)})"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    prepared = prepare_audio_for_upload(source, dest_dir)

    if client is None:
        from openai import OpenAI

        if not os.getenv("OPENAI_API_KEY"):
            raise DiarizeError("Set the OPENAI_API_KEY environment variable to your OpenAI API key first")
        # A 20-minute chunk can take OpenAI over 10 minutes (the SDK default), and each retry starts from zero.
        client = OpenAI(timeout=DIARIZE_TIMEOUT_SECONDS, max_retries=DIARIZE_MAX_RETRIES)

    chunks = split_audio_for_diarize(
        prepared.path,
        dest_dir / "chunks",
        chunk_seconds=chunk_seconds,
        max_seconds=MAX_DIARIZE_SECONDS,
    )

    # Offsets come from the chunk files up front, so results can arrive in any order.
    durations = [ffprobe_duration(c) for c in chunks] if len(chunks) > 1 else [None]
    if len(chunks) > 1 and None in durations:
        raise DiarizeError("Could not measure every chunk's length; timestamps would be wrong")
    raw_chunks = _diarize_chunks(client, chunks, model=model, known=known)

    merged_segments: list[dict[str, Any]] = []
    chunk_payloads: list[dict[str, Any]] = []
    plain_parts: list[str] = []
    time_offset = 0.0
    known_names = frozenset(k.name for k in known)

    for i, (chunk, raw_chunk, chunk_dur) in enumerate(zip(chunks, raw_chunks, durations), start=1):
        segs = raw_chunk.get("segments")
        if not isinstance(segs, list):
            segs = []
        speaker_prefix = f"C{i:02d}_" if len(chunks) > 1 else None
        offset_segs = _offset_segments(
            [s for s in segs if isinstance(s, dict)],
            time_offset=time_offset,
            speaker_prefix=speaker_prefix,
            known_names=known_names,
        )
        merged_segments.extend(offset_segs)
        chunk_plain = (
            segments_to_plain(offset_segs)
            if offset_segs
            else diarized_payload_to_plain(raw_chunk)
        )
        if len(chunks) > 1:
            plain_parts.append(
                f"--- chunk {i}/{len(chunks)} ({chunk.name}, offset={time_offset:.1f}s) ---\n"
                f"{chunk_plain.strip()}"
            )
        else:
            plain_parts.append(chunk_plain.strip())
        chunk_payloads.append(
            {
                "chunk": chunk.name,
                "index": i,
                "time_offset_seconds": time_offset,
                "result": raw_chunk,
            }
        )
        time_offset += chunk_dur or 0.0

    plain = "\n\n".join(p for p in plain_parts if p.strip())
    if not plain.strip():
        raise DiarizeError("Diarization returned empty transcript")

    raw: dict[str, Any] = {
        "model": model,
        "segments": merged_segments,
        "text": plain,
        "chunks": chunk_payloads,
        "chunk_count": len(chunks),
        "speaker_labels_prefixed_by_chunk": len(chunks) > 1,
        "note": (
            "Unnamed speaker IDs restart in every duration chunk, so they are prefixed "
            "C01_/C02_/…; known speakers keep their names across chunks."
            if len(chunks) > 1
            else None
        ),
    }

    json_path = dest_dir / "diarized.json"
    json_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    plain_path = dest_dir / "plain.txt"
    plain_path.write_text(plain + ("\n" if not plain.endswith("\n") else ""), encoding="utf-8")

    if not keep_intermediate:
        _remove_intermediate_audio(source, prepared.path, dest_dir / "chunks")

    return DiarizeResult(
        plain=plain,
        raw=raw,
        audio=prepared,
        model=model,
        known_speakers=[k.name for k in known],
        diarized_json_path=json_path,
        plain_path=plain_path,
    )
