"""prepare_audio_for_upload: the upload file is always real MP3 audio."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from briefing.audio_prep import prepare_audio_for_upload

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _format(path: Path) -> str:
    return subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
         "-of", "default=nw=1:nk=1", str(path)],
        text=True,
    ).strip()


@pytest.mark.parametrize("suffix,codec", [(".m4a", ["-c:a", "aac"]), (".aifc", []), (".wav", []), (".mp3", [])])
def test_upload_is_mp3_whatever_the_input(tmp_path: Path, suffix: str, codec: list[str]) -> None:
    src = tmp_path / f"meeting{suffix}"
    subprocess.check_call(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=5", *codec, str(src)]
    )
    out = tmp_path / "run"
    prepared = prepare_audio_for_upload(src, out)
    assert prepared.path.name == "audio_for_api.mp3"
    assert _format(prepared.path) == "mp3"
