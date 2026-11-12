import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import PurePath


def rss_snapshot():
    try:
        run = subprocess.run(["/bin/ps", "-ax", "-o", "ppid=,rss=,comm="],
                             capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in run.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]) * 1024, PurePath(parts[2]).name.casefold()))
    return rows


@contextmanager
def peak_rss(select):
    stop = threading.Event()
    result = {}

    def sample():
        while not stop.is_set():
            sizes = [size for parent, size, name in rss_snapshot() if select(parent, name)]
            if sizes:
                result["peak_rss_bytes_sampled"] = max(result.get("peak_rss_bytes_sampled", 0), sum(sizes))
            stop.wait(.25)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        yield result
    finally:
        stop.set()
        thread.join()


def worker_media(parent, name):
    return parent == os.getpid() and name in {"ffmpeg", "ffprobe"}


def ollama_process(_, name):
    return name in {"ollama", "llama-server"} or name.startswith("ollama_llama_server")
