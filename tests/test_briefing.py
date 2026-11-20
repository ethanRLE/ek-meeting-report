"""End to end: fake diarized transcript -> plain.txt -> check -> review.html. No network, no ffmpeg."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from briefing.__main__ import main  # noqa: E402
from briefing.importer import ImportError_, parse  # noqa: E402

SEGMENTS = [
    ("Speaker 1", 0, "Thanks for joining, let's start with the site survey."),
    ("Speaker 1", 4000, "We still need the boundary data."),
    ("Speaker 2", 9000, "I can get the boundary data to you by Friday."),
    ("Speaker 3", 15000, "What about the permit timeline?"),
    ("Speaker 2", 21000, "The permit is still waiting on the city."),
    ("Speaker 1", 30000, "Okay, so Friday for boundary data and we check the permit next week."),
    ("Speaker 3", 38000, "Sounds good, talk next week."),
]


def _plaud_json() -> str:
    return json.dumps({"data": {"source_list": [
        {"speaker": w, "start_time": t, "end_time": t + 3500, "content": text} for w, t, text in SEGMENTS
    ]}})


def _line(lines: list[str], needle: str) -> int:
    return next(i for i, l in enumerate(lines, 1) if needle in l)


def test_json_import_check_and_render(tmp_path: Path) -> None:
    src = tmp_path / "t.json"
    src.write_text(_plaud_json(), encoding="utf-8")
    run = tmp_path / "sj_mk_20261110"
    assert main(["import", str(src), "--run-dir", str(run)]) == 0

    plain = (run / "plain.txt").read_text(encoding="utf-8")
    assert plain.startswith("--- chunk 1/1 ---\nSpeaker 1: Thanks for joining")
    assert "Speaker 1: Thanks for joining, let's start with the site survey.\n\nSpeaker 1: We still need" in plain  # one line per segment
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    assert meta["turns"] == 7 and meta["speakers"] == ["Speaker 1", "Speaker 2", "Speaker 3"]
    assert meta["duration_seconds"] == pytest.approx(41.5)
    assert "Speaker 2 = " in (run / "notes.txt").read_text(encoding="utf-8")
    segs = json.loads((run / "segments.json").read_text(encoding="utf-8"))
    assert segs[0] == {"speaker": "Speaker 1", "start": 0.0, "end": 3.5}

    sanitized = plain.replace("Speaker 1:", "SJ:").replace("Speaker 2:", "MK:").replace("Speaker 3:", "TL:")
    (run / "sanitized.md").write_text(sanitized, encoding="utf-8")
    # Speaker 3 is left as a mixed label: talk time shows it under its transcript label
    (run / "notes.txt").write_text(
        "Participants: SJ, MK, TL\n\nSpeaker 1 = SJ (lead)\nSpeaker 2 = MK\nSpeaker 3 = mixed\n", encoding="utf-8"
    )
    lines = sanitized.splitlines()
    friday, permit, last = _line(lines, "by Friday"), _line(lines, "check the permit"), len(lines)
    (run / "report.md").write_text(
        "## Summary\nBoundary data is due Friday.\n\n"
        f"## Action items\n- MK: Send the boundary data by Friday [L{friday}]\n- Open: Check the permit status [L{permit}]\n\n"
        f'## Topics\n### Survey\n- Boundary data still missing\n> "I can get the boundary data to you by Friday." — MK [L{friday}]\n\n'
        "## Speakers\n### SJ\nRan the meeting.\n\n### MK\nOwns the boundary data.\n\n"
        "## Open questions / follow-ups\n- When will the city answer?\n",
        encoding="utf-8",
    )
    assert main(["check", "--run-dir", str(run)]) == 1  # no read-receipt yet
    (run / "read-receipt.md").write_text(
        f'Closing priorities: boundary data, then permit [L{permit}]\n> "Sounds good, talk next week." [L{last}]\n',
        encoding="utf-8",
    )
    assert main(["render", "--run-dir", str(run)]) == 0
    page = (run / "review.html").read_text(encoding="utf-8")
    assert '<td class="owner p2">MK</td><td>Send the boundary data by Friday</td>' in page
    assert '<td class="owner generic">Open</td>' in page
    assert "[L" not in page and "{{" not in page
    assert "November 10, 2026" in page and "0:42" in page
    assert '<p class="who p1">SJ</p>' in page
    assert "&ldquo;I can get the boundary data to you by Friday.&rdquo;<cite>— MK</cite>" in page
    # talk time, in the header: SJ 3 segments x 3.5 s, MK and TL 2 x 3.5 s each; 24.5 s of speech
    assert page.index('class="talk"') < page.index('id="summary"')
    assert '<li class="p1">SJ <span>0:10 · 43%</span></li>' in page
    assert '<li class="p3">TL <span>0:07 · 29%</span></li>' in page
    assert "left out" not in page

    # A line the agent could not tie to one person stays under its transcript label and is left out of the bar
    lines = sanitized.splitlines()
    i = _line(lines, "Sounds good")
    lines[i - 1] = lines[i - 1].replace("TL:", "Speaker 3:")
    (run / "sanitized.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main(["render", "--run-dir", str(run)]) == 0
    page = (run / "review.html").read_text(encoding="utf-8")
    assert '<li class="p3">TL <span>0:04 · 17%</span></li>' in page
    assert "0:04 could not be tied to one person and is left out" in page


def test_no_timestamps_means_no_talk_time(tmp_path: Path) -> None:
    src = tmp_path / "t.txt"
    src.write_text("Speaker 1: hello there friend\nSpeaker 2: hi back\nSpeaker 1: ok then\n", encoding="utf-8")
    run = tmp_path / "x_20261110"
    assert main(["import", str(src), "--run-dir", str(run)]) == 0
    assert json.loads((run / "segments.json").read_text(encoding="utf-8")) == []
    (run / "sanitized.md").write_text((run / "plain.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (run / "report.md").write_text("## Summary\nHi.\n\n## Action items\n- Open: say hi [L2]\n", encoding="utf-8")
    from briefing.render import render

    page = render(run).read_text(encoding="utf-8")
    assert "talk-time" not in page and "{{" not in page


def test_check_rejects_a_dropped_line(tmp_path: Path) -> None:
    plain = "--- chunk 1/1 ---\nA: hello there friend\n\nB: and\n\nA: see you on friday then\n"
    (tmp_path / "plain.txt").write_text(plain, encoding="utf-8")
    (tmp_path / "sanitized.md").write_text(plain.replace("B: and\n\n", ""), encoding="utf-8")
    (tmp_path / "report.md").write_text("## Action items\n- Open: meet friday [L4]\n", encoding="utf-8")
    (tmp_path / "read-receipt.md").write_text(
        'Closing priorities: friday [L4]\n> "see you on friday then" [L4]\n', encoding="utf-8"
    )
    from briefing.check import check_report

    assert check_report(tmp_path) == ["sanitized.md has 2 speaker lines, plain.txt has 3: a cleaner dropped or merged a line"]


def test_check_rejects_bad_owner_and_missing_quote(tmp_path: Path) -> None:
    (tmp_path / "plain.txt").write_text("A: hello there friend\n", encoding="utf-8")
    (tmp_path / "sanitized.md").write_text("A: hello there friend\n", encoding="utf-8")
    (tmp_path / "report.md").write_text(
        '## Action items\n- Sarah: do it [L1]\n\n## Topics\n> "words nobody said" — A [L1]\n', encoding="utf-8"
    )
    assert main(["check", "--run-dir", str(tmp_path)]) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "1\n00:00:01,000 --> 00:00:03,000\nSpeaker 1: hello there\n\n2\n00:00:04,000 --> 00:00:06,500\nSpeaker 2: hi back\n",
        "Speaker 1  00:00:01\nhello there\n\nSpeaker 2  00:00:04\nhi back\n",
        "00:00:01 Speaker 1\nhello there\n00:00:04 Speaker 2\nhi back\n",
        "Speaker 1: hello there\nSpeaker 2: hi back\nSpeaker 1: ok\nSpeaker 2: bye\n",
        "[00:00:01] Speaker 1: hello there\n[00:00:04] Speaker 2: hi back\n",  # starts with "[" but is not JSON
        json.dumps({"segments": [{"speaker_id": "Speaker 1", "text": "hello there"},
                                 {"speaker_id": "Speaker 2", "text": "hi back"}]}),
        json.dumps({"speaker_list": [{"id": 1, "name": "Sam"}],
                    "trans_result": [{"speaker": "Speaker 1", "content": "hello there"},
                                     {"speaker": "Speaker 2", "content": "hi back"}]}),
    ],
)
def test_text_and_subtitle_formats(raw: str) -> None:
    assert [(w, t) for w, t, _, _ in parse(raw)][:2] == [("Speaker 1", "hello there"), ("Speaker 2", "hi back")]


def test_colon_inside_a_turn_is_not_a_speaker_and_times_chain() -> None:
    raw = "Speaker 1  00:00:01\nNote this: the deadline moved\nSpeaker 2  00:00:05\nok\nSpeaker 1  00:00:09\nthanks\n"
    segs = parse(raw)
    assert segs[0] == ("Speaker 1", "Note this: the deadline moved", 1, 5)  # ends when the next one starts
    assert segs[-1] == ("Speaker 1", "thanks", 9, 9.4)  # last one: 1 word at 2.5 words a second


def test_silence_before_the_next_segment_is_not_talk() -> None:
    segs = parse("00:00:00 Speaker 1\nhold on one sec\n00:01:00 Speaker 2\nok\n00:01:02 Speaker 1\nback\n")
    assert segs[0][2:] == (0, 2 + 4 / 1.5)  # 4 words, not the 60 s until Speaker 2 starts


def test_srt_keeps_start_and_end() -> None:
    segs = parse("1\n00:00:01,000 --> 00:00:03,500\nSpeaker 1: hello there\n\n2\n00:00:04,000 --> 00:00:06,000\nSpeaker 2: hi\n")
    assert segs[0][2:] == (1.0, 3.5)


def test_empty_segments_are_skipped() -> None:
    segs = [("Speaker 1", ""), ("Speaker 1", "hello there"), ("Speaker 2", ""), ("Speaker 2", "hi back")]
    parsed = parse(json.dumps([{"speaker": w, "content": t} for w, t in segs]))
    assert [(w, t) for w, t, _, _ in parsed] == [("Speaker 1", "hello there"), ("Speaker 2", "hi back")]


def test_transcribe_without_openai_key_or_package_says_what_to_do(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["transcribe", str(tmp_path / "missing.m4a"), "--run-dir", str(tmp_path / "r")]) == 1
    assert capsys.readouterr().err.strip()  # a message, not a traceback


def test_no_speakers_stops_instead_of_one_big_turn() -> None:
    with pytest.raises(ImportError_, match="no speaker labels"):
        parse("1\n00:00:01,000 --> 00:00:02,000\nhello\n\n2\n00:00:03,000 --> 00:00:04,000\nworld\n")
    with pytest.raises(ImportError_, match="no speaker labels"):
        parse(json.dumps([{"text": "hello"}, {"text": "world"}]))


def test_unknown_format_stops_and_shows_first_lines() -> None:
    with pytest.raises(ImportError_, match="First lines:\njust some words"):
        parse("just some words\nwith no speakers\n")
