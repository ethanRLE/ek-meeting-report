"""diarize_audio with a fake OpenAI client: chunk merge, saved results, parallel order."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from briefing.diarize import DiarizeError, KnownSpeaker, diarize_audio

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


class FakeClient:
    def __init__(self, fail_on: set[str] = frozenset()) -> None:
        self.seen: list[str] = []
        self.fail_on = fail_on
        self._lock = threading.Lock()
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        name = Path(kwargs["file"].name).name
        with self._lock:
            self.seen.append(name)
        if name in self.fail_on:
            raise RuntimeError("simulated API failure")
        if name.endswith("000.mp3"):
            time.sleep(0.5)  # first chunk answers last
        return {"segments": [{"speaker": "A", "text": f"hello from {name}", "start": 1.0, "end": 2.0}]}


@pytest.fixture(scope="module")
def long_mp3(tmp_path_factory) -> Path:
    """50 minutes of silence: splits into 20 + 20 + 10 minute chunks."""
    path = tmp_path_factory.mktemp("audio") / "meeting.mp3"
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "3000",
         "-c:a", "libmp3lame", "-b:a", "32k", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return path


def test_chunks_merge_in_order_with_offsets(long_mp3: Path, tmp_path: Path) -> None:
    client = FakeClient()
    diarize_audio(long_mp3, tmp_path, client=client)

    assert sorted(client.seen) == [f"diarize_chunk_00{i}.mp3" for i in range(3)]
    data = json.loads((tmp_path / "diarized.json").read_text(encoding="utf-8"))
    segs = data["segments"]
    assert [s["speaker"] for s in segs] == ["C01_A", "C02_A", "C03_A"]
    assert [round(s["start"]) for s in segs] == [1, 1201, 2401]

    plain = (tmp_path / "plain.txt").read_text(encoding="utf-8")
    assert plain.index("chunk 1/3") < plain.index("chunk 2/3") < plain.index("chunk 3/3")
    assert not (tmp_path / "chunks").exists()
    assert not (tmp_path / "audio_for_api.mp3").exists()


def test_named_speakers_keep_names_and_visitors_stay_apart(long_mp3: Path, tmp_path: Path) -> None:
    clip = tmp_path / "EK.wav"
    subprocess.check_call(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=3", str(clip)]
    )

    class NamedClient(FakeClient):
        def create(self, **kwargs):
            assert kwargs["known_speaker_names"] == ["EK"]
            assert kwargs["known_speaker_references"][0].startswith("data:audio/wav;base64,")
            super().create(**kwargs)
            return {"segments": [
                {"speaker": "EK", "text": "status", "start": 1.0, "end": 2.0},
                {"speaker": "A", "text": "visitor", "start": 3.0, "end": 4.0},
            ]}

    out = tmp_path / "run"
    diarize_audio(long_mp3, out, client=NamedClient(), known_speakers=[KnownSpeaker("EK", clip)])
    speakers = [s["speaker"] for s in json.loads((out / "diarized.json").read_text(encoding="utf-8"))["segments"]]
    assert speakers == ["EK", "C01_A", "EK", "C02_A", "EK", "C03_A"]


def test_shorter_chunks_when_asked(long_mp3: Path, tmp_path: Path) -> None:
    client = FakeClient()
    diarize_audio(long_mp3, tmp_path, client=client, chunk_seconds=700)
    assert len(client.seen) == 5
    segs = json.loads((tmp_path / "diarized.json").read_text(encoding="utf-8"))["segments"]
    assert [round(s["start"]) for s in segs] == [1, 701, 1401, 2101, 2801]


def test_real_client_waits_30_minutes_and_retries_once(long_mp3: Path, tmp_path: Path, monkeypatch) -> None:
    import openai

    made: list[dict] = []

    def fake_openai(**kwargs):
        made.append(kwargs)
        return FakeClient()

    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    diarize_audio(long_mp3, tmp_path)
    assert made == [{"timeout": 1800.0, "max_retries": 1}]


def test_failed_chunk_keeps_finished_ones_and_rerun_pays_only_for_it(long_mp3: Path, tmp_path: Path) -> None:
    with pytest.raises(DiarizeError, match="Chunk 3/3"):
        diarize_audio(long_mp3, tmp_path, client=FakeClient(fail_on={"diarize_chunk_002.mp3"}))
    assert (tmp_path / "chunks" / "diarize_chunk_000.json").is_file()
    assert (tmp_path / "chunks" / "diarize_chunk_001.json").is_file()
    assert not (tmp_path / "plain.txt").exists()

    rerun = FakeClient()
    diarize_audio(long_mp3, tmp_path, client=rerun)
    assert rerun.seen == ["diarize_chunk_002.mp3"]
    assert (tmp_path / "plain.txt").read_text(encoding="utf-8").count("hello from") == 3
    assert not (tmp_path / "chunks").exists()
