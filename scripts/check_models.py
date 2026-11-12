#!/usr/bin/env python3
"""Smoke-test the real local runtimes and Ollama in order."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import media
from backend.assets import check_ollama, verify_model
from backend.config import OFFLINE_ENV, Config, model_lock, prepare_data
from backend.db import AppError
from backend.speech import Speech, exclusive_runtime
from backend.summary import Ollama
from backend.timing import normalize_intervals
from backend.voices import vector_blob


def local_speech(path):
    voices = subprocess.run(["/usr/bin/say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    voice = next((row.split("ru_RU")[0].strip() for row in voices.splitlines() if "ru_RU" in row), None)
    if not voice:
        raise AppError("speech_example", "Передайте локальный образец русской речи через --audio")
    subprocess.run(["/usr/bin/say", "-v", voice, "-o", str(path),
                    "На встрече мы решили подготовить краткий отчёт. Анна подготовит отчёт к пятнице."],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", required=True, action="store_true")
    parser.add_argument("--audio", type=Path, default=os.getenv("SMOKE_AUDIO"))
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.update(OFFLINE_ENV)
    config = Config()
    prepare_data(config)
    with exclusive_runtime(config):
        check(config, args.audio)


def check(config, audio):
    ollama = Ollama(config)
    ollama.ensure_released()
    for spec in model_lock().values():
        if "folder" in spec:
            verify_model(config.models / spec["folder"], spec)
    check_ollama()
    speech = Speech(config)
    with tempfile.TemporaryDirectory(dir=config.data / "work") as work:
        source = Path(audio) if audio else None
        if not source:
            source = Path(work) / "example.aiff"
            local_speech(source)
        wav = Path(work) / "speech.wav"
        info = media.probe(source)
        media.extract(source, wav, media.audio_stream(info), 18)
        length = media.audio_length(wav)
        result = speech.run("diarize", path=str(wav))
        if not normalize_intervals(result["regular"], length) or not normalize_intervals(result["exclusive"], length):
            raise AppError("no_speech", "Детектор не нашёл речь в проверочном файле")
        embeddings = speech.run("embed", samples=[{"path": str(wav), "windows": [(0, length)]}])
        vector_blob(embeddings[0])
        text, = speech.run("asr", path=str(wav), windows=[(0, length)])
        if not text.strip():
            raise AppError("no_transcript", "ASR не распознала проверочный образец")
        ollama.summarize([{"id": "smoke", "speaker_id": "speaker", "speaker_label": "Спикер 1",
                          "speaker_name": None, "speaker_source": None,
                          "overlap": False, "start_ms": 0, "end_ms": length, "text": text}])
    print(json.dumps({"result": "ok", "speech": speech.metrics, "ollama": ollama.metrics}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except AppError as error:
        raise SystemExit(error.payload["message"]) from None
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Офлайн-проверка не прошла ({type(error).__name__}). Проверьте веса и локальный пример.") from None
