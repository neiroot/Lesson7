"""Private, single-operation model entry point. Never invoked by the HTTP server."""

import contextlib
import json
import os
import resource
import sys
import threading
from pathlib import Path

from backend.config import PREPROCESS, VOICE_DIM, VOICE_MODEL
from backend.media import audio_length, pcm
from backend.timing import match_speakers_at_boundary, normalize_intervals

DIARIZATION_BLOCK_MS = 180_000
DIARIZATION_CONTEXT_MS = 20_000


def watch_parent(fd):
    with os.fdopen(fd, "rb") as guard:
        guard.read()
    os._exit(125)


def diarize(payload, models):
    import numpy as np
    import torch
    from pyannote.audio import Pipeline

    torch.set_num_threads(2)
    path = Path(payload["path"])
    length = audio_length(path)
    result = {"regular": [], "exclusive": []}
    if not length:
        return result
    pipeline = Pipeline.from_pretrained(str(models / "community-1"), token=False)
    pipeline.to(torch.device("cpu"))

    def process_block(start, end):
        samples = np.frombuffer(pcm(path, [(start, end)]), dtype="<i2").astype(np.float32)
        samples /= 32768
        output = pipeline({"waveform": torch.from_numpy(samples[None, :]), "sample_rate": 16000})
        blocks = {}
        for kind, annotation in (("regular", output.speaker_diarization),
                                 ("exclusive", output.exclusive_speaker_diarization)):
            intervals = [(speaker, interval.start * 1000, interval.end * 1000)
                         for interval, _, speaker in annotation.itertracks(yield_label=True)]
            blocks[kind] = [(label, left + start, right + start)
                            for label, left, right in normalize_intervals(intervals, end - start)]
        return blocks

    if length <= 300_000:
        return process_block(0, length)

    previous = []
    next_label = 0
    for start in range(0, length, DIARIZATION_BLOCK_MS):
        end = min(length, start + DIARIZATION_BLOCK_MS)
        block = process_block(max(0, start - DIARIZATION_CONTEXT_MS), min(length, end + DIARIZATION_CONTEXT_MS))
        labels = match_speakers_at_boundary(previous, block["exclusive"])
        for label, _, _ in block["regular"] + block["exclusive"]:
            if label not in labels:
                labels[label] = f"SPEAKER_{next_label:02d}"
                next_label += 1
        previous = [(labels[label], left, right) for label, left, right in block["exclusive"]]
        for kind, intervals in block.items():
            result[kind].extend((labels[label], max(left, start), min(right, end))
                                for label, left, right in intervals if left < end and right > start)
    return result


def load_embedder(models):
    import numpy as np
    import onnxruntime as ort
    import torch
    from torchaudio.compliance.kaldi import fbank

    torch.set_num_threads(1)
    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(str(models / "wespeaker/voxceleb_resnet34_LM.onnx"),
                                   sess_options=options, providers=["CPUExecutionProvider"])

    def embed(path, windows):
        signal = np.frombuffer(pcm(Path(path), windows), dtype="<i2").astype(np.float32)
        waveform = torch.from_numpy(signal[None, :])
        features = fbank(waveform, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0,
                         sample_frequency=16000, window_type="hamming", use_energy=False)
        features -= features.mean(dim=0)
        vector = session.run(["embs"], {"feats": features[None, :].numpy()})[0].reshape(-1)
        length = np.linalg.norm(vector)
        if vector.size != VOICE_DIM or not np.isfinite(length) or length <= 0:
            raise ValueError("invalid embedding")
        return vector / length

    return embed


def embed(payload, models):
    extractor = load_embedder(models)
    return [extractor(item["path"], item["windows"]).tolist() for item in payload["samples"]]


def identify(payload, models):
    import sqlite3

    import numpy as np

    def centroid(vectors):
        vector = np.mean(vectors, axis=0)
        length = np.linalg.norm(vector)
        return vector / length if np.isfinite(length) and length > 0 else None

    profiles = {}
    with contextlib.closing(sqlite3.connect(payload["db"])) as db:
        for profile_id, blob in db.execute(
            """SELECT profile_id,vector FROM speaker_embeddings WHERE model_id=? AND model_revision=?
               AND dimension=? AND preprocess_version=?""",
            (VOICE_MODEL, payload["revision"], VOICE_DIM, PREPROCESS),
        ):
            vector = np.frombuffer(blob, dtype="<f4")
            length = np.linalg.norm(vector)
            if vector.size == VOICE_DIM and np.isfinite(length) and length > 0:
                profiles.setdefault(profile_id, []).append(vector / length)
    centroids = {key: centroid(vectors) for key, vectors in profiles.items()}
    if not any(vector is not None for vector in centroids.values()):
        return {}
    extractor = load_embedder(models)
    scores = {}
    for label, windows in payload["candidates"].items():
        if not windows:
            continue
        query = centroid([extractor(payload["path"], [window]) for window in windows])
        if query is not None:
            scores[label] = {key: float(np.clip(query @ vector, -1, 1))
                             for key, vector in centroids.items() if vector is not None}
    return scores


def asr(payload, models):
    import mlx.core as mx
    import numpy as np
    from mlx_qwen3_asr import Session

    mx.set_default_device(mx.gpu)
    session = Session(model=str(models / "asr"))
    path = Path(payload["path"])
    results = []
    for start, end in payload["windows"]:
        signal = np.frombuffer(pcm(path, [(start, end)]), dtype="<i2").astype(np.float32) / 32768
        if signal.size:
            # Pad the input tensor with silence so the shortest turns reach the feature extractor.
            signal = np.pad(signal, (0, max(0, 4800 - signal.size)))
            results.append(session.transcribe(signal, language="Russian").text.strip())
        else:
            results.append("")
    mx.synchronize()
    return results


def main():
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
    operation, guard = sys.argv[1:]
    threading.Thread(target=watch_parent, args=(int(guard),), daemon=True).start()
    payload = json.load(sys.stdin)
    models = Path(payload["models"])
    action = {"diarize": diarize, "embed": embed, "identify": identify, "asr": asr}[operation]
    with contextlib.redirect_stdout(sys.stderr):
        result = action(payload, models)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024
    json.dump({"result": result, "peak_rss_bytes": peak}, sys.stdout, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    main()
