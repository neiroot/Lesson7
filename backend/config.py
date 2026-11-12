import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HOME = Path.home() / "Library/Application Support/AI Meeting Analyzer"
_SETTINGS = {
    "DATA_DIR", "MODEL_DIR", "CALIBRATION_FILE", "MAX_VIDEO_MIB", "MAX_SAMPLE_MIB",
    "MAX_SAMPLE_SECONDS", "MIN_RMS_DBFS", "MAX_CLIPPING_RATIO", "OLLAMA_NUM_CTX",
    "APP_DEV", "DIARIZE_PYTHON", "ASR_PYTHON", "SMOKE_AUDIO", "YANDEX_CLIENT_ID",
    "YANDEX_DISK_PATH", "YANDEX_POLL_SECONDS",
}
for _name, _value in dotenv_values(ROOT / ".env", interpolate=False).items():
    if _name in _SETTINGS and _value is not None:
        os.environ.setdefault(_name, _value)

CONSENT_VERSION = "1"
CONSENT_TEXT = (
    "Подтверждаю, что этот человек согласился на локальное сохранение "
    "голосового отпечатка для распознавания в будущих встречах"
)
VOICE_MODEL = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
VOICE_DIM = 256
PREPROCESS = "wespeaker-kaldi-fbank-v1"
OLLAMA_MODEL = "qwen3.5:2b"
OLLAMA_URL = "http://127.0.0.1:11434"
OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "PYANNOTE_METRICS_ENABLED": "0",
    "DO_NOT_TRACK": "1",
}

def setting_path(name, default):
    return (ROOT / Path(os.environ.get(name, default)).expanduser()).resolve()

@dataclass(frozen=True)
class Config:
    data: Path = field(default_factory=lambda: setting_path("DATA_DIR", LOCAL_HOME / "data"))
    models: Path = field(default_factory=lambda: setting_path("MODEL_DIR", LOCAL_HOME / "models"))
    calibration: Path = field(default_factory=lambda: setting_path("CALIBRATION_FILE", LOCAL_HOME / "calibration.json"))
    max_video: int = field(default_factory=lambda: int(os.getenv("MAX_VIDEO_MIB", "500")) * 2**20)
    max_sample: int = field(default_factory=lambda: int(os.getenv("MAX_SAMPLE_MIB", "50")) * 2**20)
    max_sample_seconds: int = field(default_factory=lambda: int(os.getenv("MAX_SAMPLE_SECONDS", "120")))
    min_rms: float = field(default_factory=lambda: float(os.getenv("MIN_RMS_DBFS", "-38")))
    max_clipping: float = field(default_factory=lambda: float(os.getenv("MAX_CLIPPING_RATIO", "0.005")))
    context: int = field(default_factory=lambda: int(os.getenv("OLLAMA_NUM_CTX", "8192")))
    development: bool = field(default_factory=lambda: os.getenv("APP_DEV") == "1")
    yandex_client_id: str = field(default_factory=lambda: os.getenv("YANDEX_CLIENT_ID", "").strip())
    yandex_disk_path: str = field(default_factory=lambda: os.getenv("YANDEX_DISK_PATH", "disk:/Видеовстречи"))
    yandex_poll_seconds: int = field(default_factory=lambda: max(5, int(os.getenv("YANDEX_POLL_SECONDS", "10"))))

    @property
    def db(self):
        return self.data / "meetings.sqlite3"

    @property
    def yandex_token_file(self):
        return self.data / "secrets/yandex-oauth.json"

    @property
    def yandex_redirect_uri(self):
        return "http://127.0.0.1:8000/api/integrations/yandex/callback"

    @property
    def hosts(self):
        hosts = {"127.0.0.1:8000", "localhost:8000"}
        if self.development:
            hosts |= {"127.0.0.1:5173", "localhost:5173"}
        return hosts

    def runtime(self, stage):
        group = {"diarize": "diarize", "embed": "diarize", "identify": "diarize", "asr": "asr"}[stage]
        project_python = ROOT / f".venv-{group}/bin/python"
        local_python = LOCAL_HOME / f"envs/{group}/bin/python"
        default = local_python if local_python.is_file() and not project_python.is_file() else project_python
        # The executable's virtual environment is discovered from its original path.
        return ROOT / Path(os.environ.get(group.upper() + "_PYTHON", default)).expanduser()

def model_lock():
    return json.loads((ROOT / "scripts/models.lock.json").read_text())

def prepare_data(config):
    config.data.mkdir(parents=True, mode=0o700, exist_ok=True)
    config.data.chmod(0o700)
    for folder in ("incoming", "work", "clips", "drafts", "secrets"):
        directory = config.data / folder
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
