"""python -m briefing transcribe|import|check|render — see SKILL.md."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from briefing.check import check_report
from briefing.importer import ImportError_, write_run
from briefing.render import render


def _report_import(meta: dict, run: Path) -> None:
    print(f"{meta['turns']} segments, {meta['words']} words, speakers: {', '.join(meta['speakers'])}")
    print(run / "plain.txt")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m briefing")
    sub = p.add_subparsers(dest="cmd", required=True)
    tr = sub.add_parser("transcribe", help="audio/video -> plain.txt via OpenAI (needs ffmpeg, openai, OPENAI_API_KEY)")
    tr.add_argument("audio", type=Path)
    tr.add_argument("--known-speaker", action="append", default=[], metavar="NAME=clip.wav",
                    help="2-10 s voice sample of one person, up to 4; they come back named")
    tr.add_argument("--chunk-minutes", type=float, default=20.0, help="use 10 if OpenAI returns 504 time-outs")
    imp = sub.add_parser("import", help="diarized transcript (JSON, SRT/VTT or text) -> plain.txt, segments.json, meta.json, notes.txt")
    imp.add_argument("transcript", type=Path)
    for sp in (tr, imp):
        sp.add_argument("--run-dir", type=Path, required=True)
        sp.add_argument("--date", default="", help="meeting date, YYYY-MM-DD")
    for name in ("check", "render"):
        sub.add_parser(name).add_argument("--run-dir", type=Path, required=True)
    a = p.parse_args(argv)

    if a.cmd == "transcribe":
        from briefing.audio_prep import AudioPrepError
        from briefing.diarize import DiarizeError, diarize_audio, parse_known_speaker_arg

        try:
            known = [parse_known_speaker_arg(k) for k in a.known_speaker]
            diarize_audio(a.audio, a.run_dir, known_speakers=known, chunk_seconds=a.chunk_minutes * 60)
            meta = write_run(a.run_dir / "diarized.json", a.run_dir, date=a.date)
        except ImportError:  # the openai package
            print("Run: pip install -r requirements-transcribe.txt", file=sys.stderr)
            return 1
        except (DiarizeError, AudioPrepError, ImportError_, OSError) as exc:
            print(exc, file=sys.stderr)
            return 1
        _report_import(meta, a.run_dir)
        return 0

    if a.cmd == "import":
        try:
            meta = write_run(a.transcript, a.run_dir, date=a.date)
        except (ImportError_, OSError) as exc:
            print(exc, file=sys.stderr)
            return 1
        _report_import(meta, a.run_dir)
        return 0

    problems = check_report(a.run_dir)
    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        print("check failed; review.html not written" if a.cmd == "render" else "check failed", file=sys.stderr)
        return 1
    if a.cmd == "check":
        print("check passed")
        return 0
    print(render(a.run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
