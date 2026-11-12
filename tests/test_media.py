import shutil
import subprocess
import wave

import pytest

from backend import media
from backend.config import Config
from backend.db import AppError
from backend.timing import asr_windows, match_speakers_at_boundary, timeline
from tests.conftest import write_audio

ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg is not installed")


def video(path, duration, *, audio=True):
    args = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=48x48:r=2"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=450:sample_rate=16000"]
    if path.suffix == ".mp4":
        args += ["-c:v", "mpeg4", "-c:a", "aac"]
    else:
        args += ["-c:v", "libvpx", "-c:a", "libopus"]
    subprocess.run([*args, "-t", str(duration), str(path)], check=True, timeout=30, capture_output=True)


@ffmpeg
@pytest.mark.parametrize("seconds", [1, 119, 301])
def test_mp4_accepts_short_and_long_recordings(tmp_path, seconds):
    path = tmp_path / "clip.mp4"
    video(path, seconds)
    raw = path.with_suffix(".bin")
    path.rename(raw)
    duration, audio = media.validate_video(raw)
    assert duration == seconds * 1000
    if seconds == 1:
        wav = tmp_path / "audio.wav"
        media.extract(raw, wav, audio, 1)
        with wave.open(str(wav), "rb") as output:
            assert (output.getnchannels(), output.getframerate(), output.getsampwidth()) == (1, 16000, 2)
    elif seconds > 300:
        pcm = tmp_path / "audio.pcm"
        media.extract(raw, pcm, audio)
        assert media.audio_length(pcm) >= 301000
        assert any(media.pcm(pcm, [(300500, 301000)]))


def test_boundary_match_preserves_unambiguous_labels():
    previous = [("speaker-1", 170000, 171000), ("speaker-2", 171000, 172000), ("speaker-1", 172000, 173000)]
    current = [("new-2", 170000, 171000), ("new-1", 171000, 172000),
               ("new-3", 172000, 172400), ("new-2", 172400, 173000), ("new-4", 180000, 182000)]
    assert match_speakers_at_boundary(previous, current) == {"new-2": "speaker-1", "new-1": "speaker-2"}


@ffmpeg
def test_webm_ebml_and_missing_audio(tmp_path):
    webm = tmp_path / "clip.webm"
    video(webm, 125)
    assert 125000 <= media.validate_video(webm)[0] <= 125100
    mkv = tmp_path / "clip.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(webm), "-c", "copy", str(mkv)],
                   check=True, timeout=15)
    with pytest.raises(AppError) as error:
        media.validate_video(mkv)
    assert error.value.payload["code"] == "invalid_container"
    mp4 = tmp_path / "silent.mp4"
    video(mp4, 120, audio=False)
    with pytest.raises(AppError) as error:
        media.validate_video(mp4)
    assert error.value.payload["code"] == "no_audio"
    webm.write_bytes(b"broken")
    with pytest.raises(AppError) as error:
        media.validate_video(webm)
    assert error.value.payload["code"] == "invalid_media"


def test_quality_and_timing(tmp_path):
    wav = tmp_path / "test.wav"
    write_audio(wav, seconds=4)
    assert media.quality(wav, [(0, 4000)], Config())[1]
    write_audio(wav, seconds=6, level=32767)
    assert "Слышна перегрузка записи" in media.quality(wav, [(0, 6000)], Config())[1]
    write_audio(wav, seconds=6, level=0)
    assert media.quality(wav, [(0, 6000)], Config())[0] == 0
    regular = [("A", 0, 46000), ("B", 20000, 20300), ("B", 46050, 46100)]
    exclusive = [("A", 0, 46000), ("B", 46050, 46100)]
    spans = list(timeline(regular, exclusive))
    windows = list(asr_windows(spans))
    assert (20000, 20300, None, True) in windows
    assert windows[-1] == (46050, 46100, "B", False)
    assert all(end - start <= 20000 for start, end, *_ in windows)
    assert sum(end - start for start, end, *_ in windows) == 46050
