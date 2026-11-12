#!/usr/bin/env python3
"""Derive auto-matching thresholds from distinct local, annotated Russian recordings."""

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import media
from backend.config import OFFLINE_ENV, PREPROCESS, Config, model_lock
from backend.db import AppError
from backend.speech import Speech, exclusive_runtime
from backend.summary import Ollama
from backend.timing import clean_windows, normalize_intervals, review_windows, timeline
from backend.voices import vector_blob
from backend.worker import matches


def centroid(vectors):
    vector = [sum(column) / len(column) for column in zip(*vectors, strict=True)]
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("invalid centroid")
    return [value / norm for value in vector]


def embeddings(speech, samples):
    vectors = speech.run("embed", samples=samples)
    for vector in vectors:
        vector_blob(vector)
    if len(vectors) != len(samples):
        raise ValueError("embedding count")
    return vectors


def pcm_file(file, workspace):
    out = workspace / "source.wav"
    info = media.probe(file)
    duration = media.seconds(info)
    if not 5 <= duration <= 300:
        raise ValueError("calibration recording duration")
    media.extract(file, out, media.audio_stream(info), duration + 1)
    return out


def ground_truth(intervals, annotations):
    scored = []
    length = sum(end - start for start, end in intervals)
    for person in annotations:
        duration = sum(max(0, min(end, b) - max(start, a))
                       for start, end in intervals for a, b in person["intervals"])
        scored.append((duration, person["profile"]))
    scored.sort(key=lambda item: item[0], reverse=True)
    if length and scored and scored[0][0] >= length * .8 and (len(scored) == 1 or scored[1][0] < length * .2):
        return scored[0][1]
    return None


def collect(meetings, profiles, config, speech, workspace, base):
    result = []
    for meeting in meetings:
        path = pcm_file((base / meeting["file"]).resolve(), workspace)
        output = speech.run("diarize", path=str(path))
        regular = normalize_intervals(output["regular"], media.audio_length(path))
        exclusive = normalize_intervals(output["exclusive"], media.audio_length(path))
        spans = list(timeline(regular, exclusive))
        samples = {}
        truth = {}
        for label in sorted({item[0] for item in regular + exclusive}):
            truth[label] = ground_truth(clean_windows(spans, label), meeting["speakers"])
            windows = []
            for start, end, clean in review_windows(spans, regular, label):
                duration, reasons = media.quality(path, [(start, end)], config)
                if clean and not reasons:
                    windows.append((start, end, duration))
            samples[label] = []
            if windows:
                vectors = embeddings(speech, [{"path": str(path), "windows": [(start, end)]}
                                              for start, end, _ in windows])
                samples[label] = [(duration, vector) for (_, _, duration), vector in zip(windows, vectors, strict=True)]
        result.append((truth, samples))
    return result


def prepare_rankings(meetings, profiles, minimum):
    results = []
    for truth, samples in meetings:
        scores, windows = {}, {}
        for label, records in samples.items():
            vectors = [vector for duration, vector in records if duration >= minimum]
            windows[label] = vectors
            if vectors:
                query = centroid(vectors)
                scores[label] = {name: max(-1, min(1, sum(a * b for a, b in zip(query, profile, strict=True))))
                                 for name, profile in profiles.items()}
        results.append((truth, scores, windows))
    return results


def evaluate(meetings, profiles, threshold, margin):
    stats = {"clusters": 0, "negatives": 0, "correct_accepts": 0, "false_accepts": 0, "abstained": 0}
    for truth, scores, candidates in meetings:
        selected, _ = matches(scores, {"threshold": threshold, "margin": margin}, candidates, profiles)
        for label, profile in truth.items():
            stats["clusters"] += 1
            stats["negatives"] += profile is None
            if label not in selected:
                stats["abstained"] += 1
            elif profile == selected[label]:
                stats["correct_accepts"] += 1
            else:
                stats["false_accepts"] += 1
    stats["abstention_rate"] = round(stats["abstained"] / max(1, stats["clusters"]), 4)
    return stats


def fit(tune, validation, profiles):
    best = None
    for minimum in (5000, 7500, 10000, 12500, 15000):
        rankings = prepare_rankings(tune, profiles, minimum)
        for score in range(35, 96):
            for gap in range(2, 22, 2):
                threshold, margin = score / 100, gap / 100
                stats = evaluate(rankings, profiles, threshold, margin)
                if stats["false_accepts"] or not stats["correct_accepts"] or not stats["negatives"]:
                    continue
                rank = (stats["correct_accepts"], threshold, margin, minimum)
                if best is None or rank > best[0]:
                    best = (rank, stats)
    if best is None:
        raise AppError("calibration_tune", "На наборе настройки нет безопасного порога с подтверждёнными совпадениями")
    (_, threshold, margin, minimum), tune_stats = best
    stats = evaluate(prepare_rankings(validation, profiles, minimum), profiles, threshold, margin)
    return {"threshold": threshold, "margin": margin, "min_speech_ms": minimum,
            "tune": tune_stats, "validation": stats}


def calibrate(config, manifest, file):
    base = file.resolve().parent
    seen = set()
    if not manifest["profiles"] or not manifest["tune"] or not manifest["validation"]:
        raise ValueError("incomplete dataset")
    groups = [[item for items in manifest["profiles"].values() for item in items], manifest["tune"],
              manifest["validation"]]
    for group in groups:
        paths = {(base / row["file"]).resolve() for row in group}
        if paths & seen:
            raise ValueError("training and validation recordings must be distinct")
        seen |= paths
    speech = Speech(config)
    Ollama(config).ensure_released()
    profiles = {}
    with tempfile.TemporaryDirectory(dir=config.data / "work") as folder:
        work = Path(folder)
        for label, samples in manifest["profiles"].items():
            vectors = []
            for sample in samples:
                path = pcm_file((base / sample["file"]).resolve(), work)
                windows = [(sample["start_ms"], sample["end_ms"])]
                if not 0 <= windows[0][0] < windows[0][1] <= media.audio_length(path):
                    raise ValueError("enrollment window outside the recording")
                if media.quality(path, windows, config)[1]:
                    raise ValueError("enrollment sample quality")
                vectors.extend(embeddings(speech, [{"path": str(path), "windows": windows}]))
            profiles[label] = centroid(vectors)
        tune = collect(manifest["tune"], profiles, config, speech, work, base)
        validation = collect(manifest["validation"], profiles, config, speech, work, base)
    result = fit(tune, validation, profiles)
    result.update(model_revision=model_lock()["wespeaker"]["revision"], preprocess=PREPROCESS,
                  quality={"min_rms": config.min_rms, "max_clipping": config.max_clipping})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    stats = result["validation"]
    if stats["false_accepts"] or not stats["correct_accepts"] or not stats["negatives"]:
        raise AppError("calibration_validation", "Независимая проверка не прошла. Новые пороги не сохранены")
    path = config.calibration
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        json.dump(result, target, ensure_ascii=False, indent=2)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.update(OFFLINE_ENV)
    config = Config()
    with exclusive_runtime(config):
        calibrate(config, json.loads(args.manifest.read_text()), args.manifest)


if __name__ == "__main__":
    try:
        main()
    except AppError as error:
        raise SystemExit(error.payload["message"]) from None
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise SystemExit(f"Проверьте манифест и локальные записи ({type(error).__name__}).") from None
