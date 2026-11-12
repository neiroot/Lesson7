import wave

import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.config import Config, VOICE_DIM
from backend.worker import Worker


class FakeSpeech:
    def __init__(self):
        self.metrics = {}
        self.embedding_hook = None
        self.diarization = {
            "regular": [("A", 0, 18000), ("B", 17500, 30000)],
            "exclusive": [("A", 0, 18000), ("B", 18000, 30000)],
        }

    def run(self, operation, **payload):
        if operation == "diarize":
            return self.diarization
        if operation == "asr":
            return ["Анна подготовит отчёт к пятнице" for _ in payload["windows"]]
        if operation == "embed":
            if self.embedding_hook:
                return self.embedding_hook(payload)
            return [[1.0] + [0.0] * (VOICE_DIM - 1) for _ in payload["samples"]]
        return {}


class FakeOllama:
    def __init__(self):
        self.metrics = []
        self.hook = None

    def ensure_released(self):
        pass

    def summarize(self, segments):
        if self.hook:
            self.hook()
        return {"overview": segments[0]["speaker_name"] or "Обсуждение отчёта",
                "decisions": [], "tasks": [], "open_questions": []}


def write_audio(path, *, level=3500, seconds=30):
    sample = int(level).to_bytes(2, "little", signed=True)
    frames = sample * 16000 * seconds
    if path.suffix == ".pcm":
        path.write_bytes(frames)
        return
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(frames)


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config(data=tmp_path / "data", models=tmp_path / "models",
                    calibration=tmp_path / "calibration.json", max_video=1024)
    worker = Worker(config, FakeSpeech(), FakeOllama())
    monkeypatch.setattr("backend.media.validate_video", lambda _: (120000, 1))
    monkeypatch.setattr("backend.media.validate_sample", lambda *_: 0)
    monkeypatch.setattr("backend.media.extract", lambda path, output, *_: write_audio(output))
    with TestClient(create_app(config), base_url="http://127.0.0.1:8000",
                    headers={"Origin": "http://127.0.0.1:8000"}) as client:
        yield client, config, worker
