import hashlib
import json
import re

from backend.config import OLLAMA_URL, model_lock
from backend.db import AppError


def identity(path, expected):
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        with path.open("rb") as source:
            return "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
    if re.fullmatch(r"git-sha1:[0-9a-f]{40}", expected):
        hasher = hashlib.sha1(usedforsecurity=False)
        hasher.update(f"blob {path.stat().st_size}\0".encode())
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        return "git-sha1:" + hasher.hexdigest()
    raise ValueError("invalid model checksum")


def verify_model(folder, spec):
    expected = dict(spec["files"])
    if any(value is None for value in expected.values()):
        receipt = json.loads((folder / ".verified.json").read_text())
        if receipt["revision"] != spec["revision"]:
            raise ValueError("revision")
        for name, value in expected.items():
            if value is None:
                digest = receipt["sha256"][name]
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("invalid receipt")
                expected[name] = "sha256:" + digest
    for name, digest in expected.items():
        if identity(folder / name, digest) != digest:
            raise ValueError("invalid model checksum")


def ollama_request(model):
    return {"model": model, **({"runner": "llamacpp"} if model.startswith("sha256:") else {})}


def check_ollama(client=None, *, timeout=4):
    import httpx

    if client is None:
        with httpx.Client(base_url=OLLAMA_URL, trust_env=False) as local:
            return check_ollama(local, timeout=timeout)
    try:
        response = client.get("/api/tags", timeout=timeout)
        response.raise_for_status()
        models = response.json()["models"]
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        raise AppError("ollama_unavailable", "Запустите локальный Ollama") from None
    spec = model_lock()["ollama"]
    if any(model.get("name") == spec["model"] and
           model.get("digest", "").removeprefix("sha256:") == spec["digest"] for model in models):
        return spec["model"]

    # A model index can report a different default runner in /api/tags.
    reference = "sha256:" + spec["digest"]
    try:
        response = client.post("/api/show", json=ollama_request(reference), timeout=timeout)
        if response.is_success and response.json().get("details", {}).get("runner") == "llamacpp":
            return reference
    except (httpx.HTTPError, ValueError):
        pass
    raise AppError("ollama_model", "Установите закреплённую версию локальной модели qwen3.5:2b")
