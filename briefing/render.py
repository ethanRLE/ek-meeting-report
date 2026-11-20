"""Render review.html from the agent's report.md. No AI calls; everything is escaped."""
from __future__ import annotations

import html
import json
import re
from datetime import date
from pathlib import Path

from briefing.check import OWNER_RE, speaker_lines, strip_citations

TEMPLATE = Path(__file__).resolve().parent / "templates" / "review-template.html"
_BULLET_RE = re.compile(r"[-*]\s+")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_OWNER_ITEM_RE = re.compile(OWNER_RE.pattern + r"\s*(.+)$")


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _inline(text: str) -> str:
    return _BOLD_RE.sub(r"<strong>\1</strong>", _esc(text))


def fmt_hms(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def participants_from_notes(notes: str) -> list[str]:
    for line in notes.splitlines():
        if line.lower().startswith("participants:"):
            return list(dict.fromkeys(re.findall(r"\b([A-Z]{2,4})\b", line)))
    return []


def _quote_html(text: str) -> str:
    body, _, who = text.rpartition(" — ")
    if not body:
        body, who = text, ""
    body = body.strip().strip('"“”')
    cite = f"<cite>— {_inline(who.strip())}</cite>" if who.strip() else ""
    return f"<blockquote>&ldquo;{_inline(body)}&rdquo;{cite}</blockquote>"


def markdown_to_html(text: str) -> str:
    """The small markdown subset report.md uses: ###/#### headings, bullets, > quotes, paragraphs, **bold**."""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in text.splitlines():
        line = raw.strip()
        heading = re.match(r"(#{3,4})\s+(.*)", line)
        if not line:
            flush()
        elif heading:
            flush()
            tag = "h3" if len(heading.group(1)) == 3 else "h4"
            out.append(f"<{tag}>{_inline(heading.group(2))}</{tag}>")
        elif line.startswith(">"):
            flush()
            out.append(_quote_html(line.lstrip("> ").strip()))
        elif _BULLET_RE.match(line):
            if para:
                flush()
            items.append(_BULLET_RE.sub("", line, count=1))
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return "\n".join(out) or "<p></p>"


def _section(report: str, *headings: str) -> str:
    lines = report.splitlines()
    for heading in headings:
        for i, line in enumerate(lines):
            if line.strip() == heading:
                end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
                return "\n".join(lines[i + 1 : end]).strip()
    return ""


def speaker_map(notes: str) -> dict[str, str]:
    """`Speaker 3 = EK (staff)` lines in notes.txt -> {"Speaker 3": "EK"}. Labels mapped to no initials stay as they are."""
    out = {}
    for line in notes.splitlines():
        m = re.match(r"^(.+?)\s*=\s*([A-Z]{2,4})\b", line.strip())
        if m:
            out[m.group(1).strip()] = m.group(2)
    return out


def _line_labels(run: Path) -> list[str]:
    """Speaker label of every segment line in sanitized.md, in order (chunk headers and blanks skipped)."""
    path = run / "sanitized.md"
    if not path.is_file():
        return []
    return [l.split(":", 1)[0].strip() for l in speaker_lines(path.read_text(encoding="utf-8"))]


def talk_time(run: Path, notes: str) -> list[tuple[str, float]]:
    """[(person or transcript label, seconds)], most talk first. Empty when the transcript had no times.

    Labels come from sanitized.md when it has one line per segment, so lines the agent
    re-assigned by content (e.g. from a mixed label) count for the right person.
    """
    path = run / "segments.json"
    if not path.is_file():
        return []
    segs = json.loads(path.read_text(encoding="utf-8"))
    labels = _line_labels(run)
    if len(labels) != len(segs):  # not cleaned yet, or lines were lost: fall back to the speaker map
        labels = [s["speaker"] for s in segs]
    names = speaker_map(notes)
    totals: dict[str, float] = {}
    for s, label in zip(segs, labels):
        who = names.get(label, label)
        totals[who] = totals.get(who, 0.0) + max(0.0, s["end"] - s["start"])
    return sorted(totals.items(), key=lambda kv: -kv[1])


def _talk_time_section(rows: list[tuple[str, float]], people: list[str], cls) -> str:
    """A thin bar plus one line per participant. Talk not tied to a participant is only mentioned in the note."""
    if people:
        other = sum(s for who, s in rows if who not in people)
        rows = [(who, s) for who, s in rows if who in people]
    else:
        other = 0.0
    speech = sum(s for _, s in rows)
    if not speech:
        return ""
    bar = "".join(f'<span class="{cls(w)}" style="width:{100 * s / speech:.1f}%"></span>' for w, s in rows)
    legend = "".join(
        f'<li class="{cls(w)}">{_esc(w)} <span>{fmt_hms(s)} · {round(100 * s / speech)}%</span></li>' for w, s in rows
    )
    note = "Talk time from the transcript's timestamps"
    if other >= 1:
        note += f"; {fmt_hms(other)} could not be tied to one person and is left out"
    return (
        '<div class="talk" aria-label="Talk time">\n'
        f'        <div class="talk-bar">{bar}</div>\n'
        f'        <ul class="talk-legend">{legend}</ul>\n'
        f'        <p class="talk-note">{_esc(note)}.</p>\n      </div>'
    )


def _run_date(run: Path, meta: dict) -> date | None:
    for s in (str(meta.get("date") or ""), run.name):
        m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return None


def render(run: Path) -> Path:
    report = strip_citations((run / "report.md").read_text(encoding="utf-8"))
    notes_path, meta_path = run / "notes.txt", run / "meta.json"
    notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else ""
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    people_ids = participants_from_notes(notes)

    def cls(initials: str) -> str:
        # one colour per participant, in the order of the Participants: line
        i = people_ids.index(initials) if initials in people_ids else 99
        return f"p{i + 1}" if i < 5 else "generic"  # the template has five person colours

    rows = []
    for line in map(str.strip, _section(report, "## Action items").splitlines()):
        if not _BULLET_RE.match(line):
            continue
        m = _OWNER_ITEM_RE.match(line)
        owner, item = (m.group(1), m.group(2).strip()) if m else ("Open", _BULLET_RE.sub("", line, count=1))
        rows.append(f'<tr><td class="owner {cls(owner)}">{_esc(owner)}</td><td>{_inline(item)}</td></tr>')

    cards = []
    for block in re.split(r"^### ", _section(report, "## Speakers"), flags=re.MULTILINE)[1:]:
        name, _, body = block.partition("\n")
        name = name.strip()
        initials = name.split()[0].strip("—-:") if name else ""
        cards.append(
            '<section class="card">\n'
            f'        <p class="who {cls(initials)}">{_inline(name)}</p>\n'
            f"        {markdown_to_html(body)}\n      </section>"
        )

    questions = _section(report, "## Open questions / follow-ups", "## Open questions")
    if questions.lower().strip("-* .") == "none":
        questions = ""
    questions_section = (
        '<section class="section" id="open-questions">\n'
        '      <h2><span class="eyebrow">Still to decide</span>Open questions</h2>\n'
        f'      <div class="body">\n        {markdown_to_html(questions)}\n      </div>\n    </section>'
        if questions else ""
    )

    d = _run_date(run, meta)
    secs = meta.get("duration_seconds")
    talk = _talk_time_section(talk_time(run, notes), people_ids, cls)
    participants = " and ".join(people_ids) or "participants"
    values = {
        "PARTICIPANTS": _esc(participants),
        "DATE_LONG": _esc(f"{d:%B} {d.day}, {d.year}" if d else "Meeting"),
        "DATE_SHORT": _esc(d.isoformat() if d else ""),
        "DURATION": _esc(fmt_hms(secs) if secs else "duration unknown"),
        "PARTICIPANT_COUNT": str(max(len(people_ids), len(cards))),
        "SUMMARY_HTML": markdown_to_html(_section(report, "## Summary") or "No summary was written."),
        "ACTION_ROWS": "\n          ".join(rows),
        "TALK_TIME": talk,
        "OPEN_QUESTIONS_NAV":'<a href="#open-questions">Open questions</a>' if questions else "",
        "OPEN_QUESTIONS_SECTION": questions_section,
        "PEOPLE_CARDS": "\n      ".join(cards),
        "TOPICS_HTML": markdown_to_html(_section(report, "## Topics")),
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)
    out = run / "review.html"
    out.write_text(page, encoding="utf-8")
    return out
