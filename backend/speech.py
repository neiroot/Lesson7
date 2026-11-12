"""Start a model process and pass private input/output through pipes."""

import fcntl
import json
import os
import signal
import subprocess
import time
from contextlib import contextmanager

from backend.assets import verify_model
from backend.config import OFFLINE_ENV, ROOT, model_lock, prepare_data
from backend.db import AppError, dump


@contextmanager
def exclusive_runtime(config):
    prepare_data(config)
    with (config.data / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError("worker_active", "Остановите запущенное приложение перед отдельной проверкой моделей") from None
        yield


class Speech:
    def __init__(self, config):
        self.config = config
        self.metrics = {}
        self.verified = set()

    def run(self, operation, *, timeout=1800, **payload):
        executable = self.config.runtime(operation)
        if not executable.is_file():
            raise AppError("runtime_missing", "Подготовьте локальное окружение речевых моделей")
        model = {"diarize": "community", "identify": "wespeaker", "embed": "wespeaker", "asr": "asr"}[operation]
        if model not in self.verified:
            spec = model_lock()[model]
            try:
                verify_model(self.config.models / spec["folder"], spec)
            except (OSError, ValueError, KeyError, TypeError):
                raise AppError("model_weights", "Локальные веса отсутствуют или не прошли проверку") from None
            self.verified.add(model)
        env = {**os.environ, **OFFLINE_ENV, "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2"}
        env.pop("HF_TOKEN", None)
        env.pop("HUGGING_FACE_HUB_TOKEN", None)
        payload["models"] = str(self.config.models)
        started = time.monotonic()
        guard_read, guard_write = os.pipe()
        try:
            with subprocess.Popen(
                [str(executable), "-m", "backend.model_process", operation, str(guard_read)],
                cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(guard_read,),
            ) as process:
                try:
                    output, _ = process.communicate(dump(payload).encode(), timeout=timeout)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise
                if process.returncode:
                    code = "model_memory" if process.returncode == -signal.SIGKILL else "model_failed"
                    message = "Процесс модели был остановлен. Проверьте доступную память" if code == "model_memory" else (
                        "Локальная модель не запустилась. Проверьте веса и окружение"
                    )
                    raise AppError(code, message)
        except subprocess.TimeoutExpired:
            raise AppError("model_timeout", "Этап обработки превысил допустимое время") from None
        except OSError:
            raise AppError("runtime_failed", "Не удалось запустить локальную модель") from None
        finally:
            os.close(guard_read)
            os.close(guard_write)
        try:
            result = json.loads(output)
            self.metrics[operation] = {
                "seconds": round(time.monotonic() - started, 3),
                "peak_rss_bytes": result["peak_rss_bytes"],
            }
            return result["result"]
        except (ValueError, KeyError, TypeError):
            raise AppError("model_output", "Локальная модель вернула повреждённый результат") from None
