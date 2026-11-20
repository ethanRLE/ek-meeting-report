# ek-meeting-report

An AI skill that turns a meeting transcript into a briefing page (`review.html`): summary, action items with owners, talk time, open questions, what each person focused on, and topics with exact quotes.

Every action item and quote must cite the transcript line it came from, and a checker refuses to build the page until they do. That stops the AI from inventing commitments or skipping the end of the meeting.

Works with:
- **Plaud**: through the Plaud connector, or an exported transcript file
- **Any transcript with speaker labels**: `.txt`, `.json`, `.srt` or `.vtt`, uploaded or pasted
- **A recording**: transcribed on your computer with your own OpenAI API key (optional)

Runs in **Claude** (desktop app or claude.ai, and Claude Code) and **ChatGPT** (Business, Enterprise and Edu workspaces that allow skills).

## Install the skill

Download `meeting-briefing.zip` from the [latest release](../../releases/latest).

**Claude (desktop app or claude.ai):** Settings → Capabilities → turn on code execution → Skills → Upload skill → choose the zip.

**ChatGPT:** Skills → Create → Upload from your computer → choose the zip. Your workspace admin may need to turn on skill uploads first. If the skill shows as installed but the chat can't use it, wait 20 minutes and try again.

**Claude Code:** clone this repo into `~/.claude/skills/meeting-briefing`.

Then ask: *"Make a meeting briefing from my latest Plaud recording"*, or upload a transcript and ask *"Make a meeting briefing from this."* Uploading the file works better than pasting for long meetings.

## Transcribe a recording (optional, on your computer)

Only needed when you have audio or video but no transcript. This runs on your own computer, not in the chat app.

1. Install Python 3.10+ and [ffmpeg](https://ffmpeg.org/download.html).
2. `pip install -r requirements-transcribe.txt`
3. Get an **OpenAI API key** at [platform.openai.com](https://platform.openai.com/api-keys). This is billed separately from a ChatGPT subscription; a ChatGPT Business plan does not include API use.
4. Set the key and run, from this folder:

   ```bash
   export OPENAI_API_KEY=sk-...        # Windows PowerShell: $env:OPENAI_API_KEY = "sk-..."
   python -m briefing transcribe "meeting.m4a" --run-dir "meetings/sj_mk_20261110" --date 2026-11-10
   ```

5. Give the resulting `plain.txt` to the AI and ask for a meeting briefing.

Long meetings are split into 20-minute pieces and sent at the same time. If OpenAI times out (504), run the same command again with `--chunk-minutes 10`; finished pieces are not sent again. Add `--known-speaker "SJ=sam.wav"` (a 2–10 second clip of one person talking, up to 4) to get people back by name.

## Lexicon (optional)

Copy `lexicon-template.md` once per project and fill in the people and the words the transcription gets wrong. Tell the AI which lexicon a meeting uses. After each meeting it suggests new rows for you to approve.

## Privacy

Transcripts and reports stay with you. The only network call is `transcribe`, which sends audio to OpenAI. On Claude's web app, Plaud data passes through Plaud's servers.

## Files

| File | What it is |
|------|------------|
| `SKILL.md` | The steps the AI follows |
| `chunk-cleaner-brief.md` | Instructions for cleaning one part of the transcript |
| `lexicon-template.md` | Blank people and wording lists for one project |
| `briefing/` | `python -m briefing transcribe / import / check / render` (standard library only, except `transcribe`) |
| `tests/` | `python -m pytest tests` (the audio tests need ffmpeg) |
