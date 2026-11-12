#!/usr/bin/env python3
"""Prepare the pinned artifacts and separate speech runtimes. This is the online step."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.assets import check_ollama, verify_model
from backend.config import ROOT, Config, model_lock
from backend.db import AppError
from backend.speech import exclusive_runtime

MIN_FREE_BYTES = 15_000_000_000


def installed(config, spec):
    try:
        verify_model(config.models / spec["folder"], spec)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def download(config, spec, token):
    from httpx import TransportError
    from huggingface_hub import HfApi, snapshot_download

    repo, revision = spec["repository"], spec["revision"]
    print(f"Подготовка {repo} · {revision[:10]}", flush=True)
    local = config.models / f".download-{spec['folder']}-{revision[:10]}"
    for attempt in range(3):
        try:
            snapshot_download(repo, revision=revision, token=token, local_dir=local,
                              allow_patterns=list(spec["files"]), max_workers=2)
            break
        except TransportError:
            if attempt == 2:
                raise
            print("Связь прервалась. Продолжаем загрузку.", flush=True)
    gated = {name for name, checksum in spec["files"].items() if checksum is None}
    if gated:
        hashes = {}
        for item in HfApi().list_repo_tree(repo, revision=revision, recursive=True, token=token):
            if item.path in gated and getattr(item, "lfs", None):
                hashes[item.path] = item.lfs.sha256
        if set(hashes) != gated:
            raise ValueError("Hugging Face не предоставил контрольные суммы закрытых файлов")
        (local / ".verified.json").write_text(json.dumps({"revision": revision, "sha256": hashes}))
    verify_model(local, spec)
    shutil.rmtree(local / ".cache", ignore_errors=True)
    destination = config.models / spec["folder"]
    if destination.exists():
        with tempfile.TemporaryDirectory(dir=config.models) as workspace:
            backup = Path(workspace) / "old"
            destination.rename(backup)
            try:
                local.rename(destination)
            except OSError:
                backup.rename(destination)
                raise
    else:
        local.rename(destination)


def install_runtime(config, name):
    stage = "diarize" if name == "diarize" else "asr"
    python = config.runtime(stage)
    if not os.getenv(name.upper() + "_PYTHON") and not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(python.parent.parent)], check=True)
    env = {key: value for key, value in os.environ.items() if key not in {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"}}
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / f"backend/requirements-{name}.lock")],
                   env=env, check=True)


def check_requirements(config):
    specs = [item for item in model_lock().values() if "folder" in item]
    needed = [spec for spec in specs if not installed(config, spec)]
    problems = []
    volume_path = next(path for path in (config.models, *config.models.parents) if path.exists())
    free = shutil.disk_usage(volume_path).free
    print(f"Свободно в томе каталога моделей: {free / 10**9:.2f} ГБ", flush=True)
    if needed and free < MIN_FREE_BYTES:
        problems.append(
            f"После загрузки модели Ollama для подготовки речевых моделей нужно "
            f"{MIN_FREE_BYTES / 10**9:g} ГБ. Сейчас не хватает {(MIN_FREE_BYTES - free) / 10**9:.2f} ГБ."
        )
    token = os.environ.get("HF_TOKEN")
    if any(spec["folder"] == "community-1" for spec in needed) and not token:
        problems.append("Примите условия доступа Community‑1 и передайте HF_TOKEN процессу подготовки.")
    try:
        check_ollama()
    except AppError as error:
        problems.append(error.payload["message"])
    if problems:
        raise SystemExit("\n".join(problems))
    return needed, token


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Проверить условия установки без загрузок")
    args = parser.parse_args()
    os.umask(0o077)
    if sys.platform != "darwin" or os.uname().machine != "arm64" or sys.version_info[:2] != (3, 12):
        raise SystemExit("Для подготовки моделей нужен Python 3.12 arm64 на Apple Silicon.")
    config = Config()
    if args.check:
        check_requirements(config)
        print("Можно начать подготовку речевых моделей.")
        return
    with exclusive_runtime(config):
        setup(config)


def setup(config):
    needed, token = check_requirements(config)
    config.models.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.models.chmod(0o700)
    for spec in needed:
        download(config, spec, token if spec["folder"] == "community-1" else False)
    for stage in ("diarize", "asr"):
        install_runtime(config, stage)
    print("Модели и окружения подготовлены. Запустите scripts/check_models.py --offline.")


if __name__ == "__main__":
    try:
        main()
    except AppError as error:
        raise SystemExit(error.payload["message"]) from None
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, ValueError):
            raise SystemExit(f"Проверка модели не прошла. {error}") from None
        raise SystemExit(f"Подготовка прервана ({type(error).__name__}). Проверьте интернет, доступ и место.") from None
