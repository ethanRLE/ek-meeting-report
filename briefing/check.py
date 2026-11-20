"""Refuse to render a report that was not built from the whole transcript.

Every action item and quote in report.md cites sanitized.md lines (`[L531-541]`),
quotes must exist in plain.txt, something cited must sit in the last quarter of
the meeting, and read-receipt.md must quote the meeting's closing turns.
No AI calls; this only checks evidence the agent left behind.
"""
from __future__ import annotations

import re
from pathlib import Path

CITE_RE = re.compile(r"\[(L\d+(?:[-–]\d+)?(?:,\s*L\d+(?:[-–]\d+)?)*)\]")
STRIP_CITES_RE = re.compile(r"\s*" + CITE_RE.pattern)
_RANGE_RE = re.compile(r"L(\d+)(?:[-–](\d+))?")
_BULLET_RE = re.compile(r"[-*]\s+")
OWNER_RE = re.compile(r"[-*]\s+(?:\*\*)?([A-Z]{2,4}|Open)(?:\*\*)?\s*:")
RECEIPT = "read-receipt.md"
LAST_QUARTER = 0.75
CLOSING_TURNS = 0.95
MAX_ACTION_ITEMS = 15
_PROCESS_NOTE_RE = re.compile(r"(?i:operator notes|closing list|priority list)|\([^)]*:\s*[\"“]")


def norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def speaker_lines(text: str) -> list[str]:
    """The `Label: words` lines of plain.txt or sanitized.md, in order (chunk headers and blanks skipped)."""
    return [l for l in text.splitlines() if l.strip() and not l.startswith("--- chunk") and ":" in l]


def cited_ranges(text: str) -> list[tuple[int, int]]:
    return [(int(a), int(b or a)) for g in CITE_RE.findall(text) for a, b in _RANGE_RE.findall(g)]


def strip_citations(text: str) -> str:
    return STRIP_CITES_RE.sub("", text)


def _quote_text(line: str) -> str:
    body = strip_citations(line).lstrip("> ").strip()
    body = body.rpartition(" — ")[0] or body  # drop the "— speaker" tail
    return body.strip().strip('"“”')


def _quote_in_plain(quote: str, plain_norm: str) -> bool:
    segs = [norm(s) for s in re.split(r"…|\.\.\.", quote)]
    segs = [s for s in segs if len(s) >= 4]
    return bool(segs) and all(s in plain_norm for s in segs)


def _evidence_lines(report: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """(action-item lines, quote lines) with 1-based report line numbers."""
    actions: list[tuple[int, str]] = []
    quotes: list[tuple[int, str]] = []
    section = ""
    for n, raw in enumerate(report.splitlines(), start=1):
        s = raw.strip()
        if s.startswith("## "):
            section = s[3:].strip().lower()
        elif s.startswith(">"):
            quotes.append((n, s))
        elif section == "action items" and _BULLET_RE.match(s):
            actions.append((n, s))
    return actions, quotes


def check_report(run: Path) -> list[str]:
    paths = {name: run / name for name in ("report.md", "sanitized.md", "plain.txt")}
    for p in paths.values():
        if not p.is_file():
            return [f"{p.name} not found in {run}"]
    sanitized = paths["sanitized.md"].read_text(encoding="utf-8")
    plain = paths["plain.txt"].read_text(encoding="utf-8")
    total = len(sanitized.splitlines())
    plain_norm = norm(plain)
    report = paths["report.md"].read_text(encoding="utf-8")
    problems: list[str] = []
    # Talk time pairs sanitized.md lines with segments.json one to one, so no line may be lost or merged.
    s_count, p_count = len(speaker_lines(sanitized)), len(speaker_lines(plain))
    if s_count != p_count:
        problems.append(
            f"sanitized.md has {s_count} speaker lines, plain.txt has {p_count}: a cleaner dropped or merged a line"
        )

    def check_cites(where: str, text: str) -> list[tuple[int, int]]:
        ranges = cited_ranges(text)
        if not ranges:
            problems.append(f"{where}: no sanitized.md citation like [L123] or [L120-130]")
        for a, b in ranges:
            if not 1 <= a <= b <= total:
                problems.append(f"{where}: [L{a}-{b}] is outside sanitized.md (1-{total})")
        return ranges

    actions, quotes = _evidence_lines(report)
    if not actions:
        problems.append("report.md: no action items found under ## Action items")
    if len(actions) > MAX_ACTION_ITEMS:
        problems.append(
            f"report.md: {len(actions)} action items (limit {MAX_ACTION_ITEMS}); merge steps of one job, "
            "leave status and asides in the discussion, keep undecided items out of the list"
        )
    cited: list[tuple[int, int]] = []
    for n, line in actions:
        cited += check_cites(f"report.md L{n} action item", line)
        if not OWNER_RE.match(line):
            problems.append(f'report.md L{n} action item: start with an owner\'s initials ("SJ:") or "Open:"')
        if _PROCESS_NOTE_RE.search(strip_citations(line)):
            problems.append(f"report.md L{n} action item: remove the note on who said it; the citation is the evidence")
    for n, line in quotes:
        cited += check_cites(f"report.md L{n} quote", line)
        if not _quote_in_plain(_quote_text(line), plain_norm):
            problems.append(f"report.md L{n} quote: not found in plain.txt")
    if cited and max(b for _, b in cited) < LAST_QUARTER * total:
        problems.append(
            f"report.md: nothing cited after L{max(b for _, b in cited)}; "
            f"the last quarter of the meeting (from L{int(LAST_QUARTER * total)}) is unread"
        )

    receipt_path = run / RECEIPT
    if not receipt_path.is_file():
        problems.append(f"{RECEIPT} missing: state the closing priorities and quote the last turns")
        return problems
    receipt = receipt_path.read_text(encoding="utf-8")
    prio = [l for l in receipt.splitlines() if l.lower().startswith("closing priorities:")]
    if not prio or not cited_ranges(prio[0]):
        problems.append(f'{RECEIPT}: needs a "Closing priorities: … [L123]" line with a citation')
    closing = [l.strip() for l in receipt.splitlines() if l.strip().startswith(">")]
    if not closing:
        problems.append(f"{RECEIPT}: needs a > quote of the meeting's last turns")
    for line in closing:
        ranges = check_cites(f"{RECEIPT} quote", line)
        if not _quote_in_plain(_quote_text(line), plain_norm):
            problems.append(f"{RECEIPT} quote: not found in plain.txt")
        if ranges and max(b for _, b in ranges) < CLOSING_TURNS * total:
            problems.append(f"{RECEIPT} quote: must come from the last turns (from L{int(CLOSING_TURNS * total)})")
    return problems
