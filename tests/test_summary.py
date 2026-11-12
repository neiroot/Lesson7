import json

import httpx
import pytest

from backend.assets import check_ollama
from backend.config import Config, model_lock
from backend.db import AppError
from backend.summary import Notes, Ollama, validate_notes
from backend.worker import matches


SEGMENT = {"id": "turn-1", "text": "Анна, подготовь отчёт к пятнице", "speaker_id": "speaker-1", "overlap": False}


def notes(quote="отчёт к пятнице", assignee=None):
    return {"overview": "Обсудили отчёт.", "decisions": [], "open_questions": [],
            "tasks": [{"description": "Подготовить отчёт", "assignee": assignee, "due": "к пятнице",
                       "evidence": [{"segment_id": "turn-1", "quote": quote}]}]}


def test_evidence_must_exist_and_support_assignee():
    validate_notes(Notes.model_validate(notes()), [SEGMENT])
    validate_notes(Notes.model_validate(notes("Анна, подготовь отчёт к пятнице", "Анна")), [SEGMENT])
    with pytest.raises(ValueError, match="unsupported assignee"):
        validate_notes(Notes.model_validate(notes(assignee="Анна")), [SEGMENT])
    with pytest.raises(ValueError, match="unattributed"):
        validate_notes(Notes.model_validate(notes(assignee="speaker-1")), [{**SEGMENT, "overlap": True}])


def test_ollama_selects_pinned_digest_when_default_runner_differs():
    spec = model_lock()["ollama"]
    pinned = "sha256:" + spec["digest"]

    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": spec["model"], "digest": "0" * 64}]})
        assert request.url.path == "/api/show"
        assert json.loads(request.content) == {"model": pinned, "runner": "llamacpp"}
        return httpx.Response(200, json={"details": {"runner": "llamacpp"}})

    with httpx.Client(transport=httpx.MockTransport(respond), base_url="http://127.0.0.1:11434") as client:
        assert check_ollama(client) == pinned


@pytest.mark.parametrize("all_bad", [False, True])
def test_invalid_answer_retries_once_and_unloads(tmp_path, all_bad):
    config = Config(data=tmp_path)
    ollama = Ollama(config)
    calls = []

    def mock(request):
        path = request.url.path
        calls.append(path)
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:2b", "digest": model_lock()["ollama"]["digest"]}]})
        if path == "/api/chat":
            payload = json.loads(request.content)
            assert payload["keep_alive"] == 0 and payload["think"] is False and payload["stream"] is False
            ids = payload["format"]["$defs"]["DraftTask"]["properties"]["segment_ids"]["items"]
            assert ids["enum"] == ["r1"]
            content = notes()
            del content["tasks"][0]["evidence"]
            content["tasks"][0]["segment_ids"] = ["r99" if all_bad or calls.count(path) == 1 else "r1"]
            content["tasks"][0]["due"] = "через неделю"
            content["tasks"][0]["assignee"] = "сотрудник"
            return httpx.Response(200, json={"done_reason": "stop", "done": True,
                                            "message": {"content": json.dumps(content)}})
        if path == "/api/generate":
            return httpx.Response(200)
        return httpx.Response(200, json={"models": []})

    ollama.client = httpx.Client(transport=httpx.MockTransport(mock), base_url="http://127.0.0.1:11434")
    if all_bad:
        with pytest.raises(AppError) as error:
            ollama.summarize([SEGMENT])
        assert error.value.payload["code"] == "invalid_summary"
    else:
        expected = notes(SEGMENT["text"])
        expected["tasks"][0]["due"] = None
        assert ollama.summarize([SEGMENT]) == expected
    assert calls == ["/api/tags", "/api/chat", "/api/generate", "/api/ps", "/api/chat", "/api/generate", "/api/ps"]
    assert not ollama.guard.exists()


def test_matching_abstains_for_small_margin_conflicts_and_uncalibrated():
    profiles = {"p1": "Анна", "p2": "Вера"}
    scores = {"A": {"p1": .91, "p2": .60}, "B": {"p1": .89, "p2": .50}, "C": {"p1": .84, "p2": .85},
              "D": {"p1": .4, "p2": .5}}
    candidates = {label: [(0, 7000)] for label in scores}
    assert not matches(scores, None, candidates, profiles)[0]
    matched, reasons = matches(scores, {"threshold": .8, "margin": .07}, candidates, profiles)
    assert not matched
    assert reasons["A"] == reasons["B"]
    assert "похожих" in reasons["C"]
    assert "не найдено" in reasons["D"]
    assert matches({"A": {"p1": .92}}, {"threshold": .8, "margin": .07}, {"A": [(0, 7000)]},
                   {"p1": "Анна"})[0] == {"A": "p1"}
