"""Turn a diarized transcript (JSON segments, SRT/VTT, or plain text) into plain.txt.

plain.txt is the one format every later step reads, one segment per paragraph:
    --- chunk 1/3 ---
    Speaker 1: words of one segment

    Speaker 2: next segment
Segments are never merged, so a citation like [L120] points at a few seconds of talk.
Each segment's speaker and times go to segments.json for talk time.
Unrecognised input stops with the first lines shown; it never guesses.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TURNS_PER_CHUNK = 80
WORDS_PER_SECOND = 2.5  # typical speech rate, used to estimate the end of the last text segment

_SPEAKER_KEYS = ("speaker", "speaker_name", "speakerName", "speaker_label", "speakerLabel", "spk", "name")
_TEXT_KEYS = ("text", "content", "sentence", "transcript", "words")
_START_KEYS = ("start", "start_time", "startTime", "begin", "start_ms", "startMs", "offset")
_END_KEYS = ("end", "end_time", "endTime", "end_ms", "endMs", "stop")
_LIST_KEYS = ("source_list", "segments", "transcript", "transcription", "utterances", "data", "result", "items")

_STAMP = r"(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_ARROW_RE = re.compile(rf"({_STAMP})\s*-->\s*({_STAMP})")
_TIME = r"\(?\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\)?"
# "Speaker 1  00:01:23"  or  "00:01:23 Speaker 1"  on its own line, words on the next lines
_HEAD_AFTER_RE = re.compile(rf"^(?P<who>[^\d:\[\(][^:]{{0,40}}?)\s+{_TIME}$")
_HEAD_BEFORE_RE = re.compile(rf"^{_TIME}\s+(?P<who>[^:]{{1,40}})$")
# "Speaker 1: words"  or  "[00:01:23] Speaker 1: words"
_INLINE_RE = re.compile(rf"^(?:{_TIME}\s+)?(?P<who>[^:\d][^:]{{0,40}}):\s+(?P<text>.+)$")
_VOICE_RE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>|$)", re.IGNORECASE)

Segment = tuple[str, str, "float | None", "float | None"]  # speaker, text, start, end (seconds)


class ImportError_(ValueError):
    pass


def _clock(s: str) -> float:
    total = 0.0
    for p in s.split(":"):
        total = total * 60 + float(p.replace(",", "."))
    return total


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v) if re.fullmatch(r"[\d.]+", v.strip()) else _clock(v.strip())
        except ValueError:
            return None
    return None


def _pick(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _speaker(d: dict):
    who = _pick(d, _SPEAKER_KEYS)
    if who is None:  # any other speaker-ish key, e.g. speaker_id
        who = next((v for k, v in d.items() if re.search(r"(?i)speaker|spk", k) and v not in (None, "")), None)
    return who


def _find_segments(data):
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return data if any(k in data[0] for k in _TEXT_KEYS) else None  # key, not value: first segment may be empty
    if isinstance(data, dict):
        for k in _LIST_KEYS:
            if k in data:
                found = _find_segments(data[k])
                if found:
                    return found
        for v in data.values():
            found = _find_segments(v)
            if found:
                return found
    return None


def _from_json(data) -> list[Segment]:
    segs = _find_segments(data)
    if not segs:
        raise ImportError_("JSON has no list of segments with a text field")
    out: list[Segment] = []
    for s in segs:
        text = _pick(s, _TEXT_KEYS) or ""
        if isinstance(text, list):  # word lists
            text = " ".join(str(w.get("text", w.get("word", ""))) if isinstance(w, dict) else str(w) for w in text)
        who = _speaker(s)
        out.append((str(who) if who is not None else "Unknown", str(text),
                    _num(_pick(s, _START_KEYS)), _num(_pick(s, _END_KEYS))))
    last = max((t for seg in out for t in seg[2:] if t is not None), default=None)
    words = sum(len(t.split()) for _, t, _, _ in out)
    # People say about 2-3 words a second; far fewer per "second" means the times are in milliseconds.
    if last and words / last < 0.1:
        out = [(w, t, a / 1000 if a is not None else None, b / 1000 if b is not None else None) for w, t, a, b in out]
    return out


def _from_subtitles(text: str) -> list[Segment]:
    out: list[Segment] = []
    for block in re.split(r"\n\s*\n", text):
        start = end = None
        words: list[str] = []
        for l in (l.strip() for l in block.splitlines() if l.strip()):
            m = _ARROW_RE.search(l)
            if m:
                start, end = _clock(m.group(1)), _clock(m.group(2))
            elif not (l.isdigit() or l.upper().startswith("WEBVTT")):
                words.append(l)
        if not words:
            continue
        payload = " ".join(words)
        v = _VOICE_RE.search(payload)
        m = _INLINE_RE.match(payload)
        if v:
            who, said = v.group(1).strip(), re.sub(r"<[^>]+>", "", v.group(2)).strip()
        elif m:
            who, said = m.group("who").strip(), m.group("text").strip()
        else:
            who, said = "Unknown", payload
        out.append((who, said, start, end))
    return out


def _label(m: re.Match | None) -> str | None:
    if m and len(m.group("who").split()) <= 4:
        return m.group("who").strip()
    return None


def _from_text(text: str) -> list[Segment]:
    out: list[Segment] = []
    who: str | None = None
    start: float | None = None
    buf: list[str] = []
    matched = 0
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # A real speaker label repeats; "we meet at 10:30" or "Note: ..." usually does not.
    seen: dict[str, int] = {}
    for line in lines:
        w = _label(_HEAD_AFTER_RE.match(line) or _HEAD_BEFORE_RE.match(line)) or _label(_INLINE_RE.match(line))
        if w:
            seen[w] = seen.get(w, 0) + 1
    # shortcut: a named speaker who talks only once is merged into the turn before; fine for real meetings
    real = {w for w, c in seen.items() if c >= 2 or re.fullmatch(r"(?i)speaker\s*\d+", w)} or set(seen)

    def flush():
        if who is not None and buf:
            out.append((who, " ".join(buf), start, None))
        buf.clear()

    for line in lines:
        head = _HEAD_AFTER_RE.match(line) or _HEAD_BEFORE_RE.match(line)
        inline = _INLINE_RE.match(line)
        head = head if _label(head) in real else None
        inline = inline if _label(inline) in real else None
        if head:
            flush()
            who, matched = head.group("who").strip(), matched + 1
            start = _clock(head.group(2) if head.re is _HEAD_AFTER_RE else head.group(1))
        elif inline:
            flush()
            who, matched = inline.group("who").strip(), matched + 1
            start = _clock(inline.group(1)) if inline.group(1) else None
            buf.append(inline.group("text").strip())
        elif who is not None:
            buf.append(line)
    flush()
    if matched == 0:
        raise ImportError_("no speaker labels found")
    return out


def parse(raw: str) -> list[Segment]:
    """Return [(speaker, text, start, end), ...]; times in seconds or None."""
    raw = raw.replace("\r\n", "\n").lstrip("\ufeff")
    data = None
    if raw.lstrip()[:1] in "[{":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            pass  # text that starts with "[00:00:01] Speaker 1: …"
    try:
        if data is not None:
            segs = _from_json(data)
        elif _ARROW_RE.search(raw):
            segs = _from_subtitles(raw)
        else:
            segs = _from_text(raw)
        if all(who == "Unknown" for who, *_ in segs):
            raise ImportError_("no speaker labels found")
    except ImportError_ as exc:
        head = "\n".join(raw.splitlines()[:8])
        raise ImportError_(f"Could not read this transcript ({exc}). First lines:\n{head}") from None
    out: list[Segment] = []
    for who, text, start, end in segs:
        who = who.replace(":", "").strip() or "Unknown"
        text = " ".join(text.split())
        if text:
            out.append((who, text, start, end))
    if not out:
        raise ImportError_("Transcript has no spoken text")
    # A segment without an end runs until the next one starts, but no longer than slow speech of its words
    # would take, so a silent minute is not counted as talk. The last one gets the typical-speed estimate.
    for i, (who, text, start, end) in enumerate(out):
        if start is not None and end is None:
            words = len(text.split())
            nxt = out[i + 1][2] if i + 1 < len(out) else None
            end = min(nxt, start + 2 + words / 1.5) if nxt is not None else start + words / WORDS_PER_SECOND
            out[i] = (who, text, start, end)
    return out


def write_run(source: Path, run: Path, *, date: str = "") -> dict:
    segs = parse(source.read_text(encoding="utf-8"))
    run.mkdir(parents=True, exist_ok=True)
    n = -(-len(segs) // TURNS_PER_CHUNK)
    out: list[str] = []
    for i, (who, text, _, _) in enumerate(segs):
        if i % TURNS_PER_CHUNK == 0:
            out.append(f"--- chunk {i // TURNS_PER_CHUNK + 1}/{n} ---")
        out.append(f"{who}: {text}\n")
    (run / "plain.txt").write_text("\n".join(out), encoding="utf-8")

    timed = [{"speaker": w, "start": a, "end": b} for w, _, a, b in segs if a is not None and b is not None]
    (run / "segments.json").write_text(json.dumps(timed, indent=1), encoding="utf-8")
    ends = [s["end"] for s in timed]
    speakers = list(dict.fromkeys(w for w, *_ in segs))
    meta = {"source": str(source), "turns": len(segs), "words": sum(len(t.split()) for _, t, _, _ in segs),
            "speakers": speakers, "duration_seconds": max(ends) if ends else None, "date": date}
    (run / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    notes = run / "notes.txt"
    if not notes.exists():
        notes.write_text(
            "Participants: \n\n"
            "Speaker map (transcript label = initials, role):\n"
            + "".join(f"{s} = \n" for s in speakers)
            + "\nWord fixes (heard -> correct):\n\nReader wording (internal term -> plain words):\n",
            encoding="utf-8",
        )
    return meta
