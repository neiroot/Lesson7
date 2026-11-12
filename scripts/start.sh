#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 TRANSFORMERS_OFFLINE=1 PYANNOTE_METRICS_ENABLED=0 DO_NOT_TRACK=1
unset HF_TOKEN HUGGING_FACE_HUB_TOKEN
app_python="${APP_PYTHON:-python}"
local_support="$HOME/Library/Application Support/AI Meeting Analyzer"
if [[ -z "${APP_PYTHON:-}" && -z "${VIRTUAL_ENV:-}" && -x "$local_support/envs/app/bin/python" ]]; then
  app_python="$local_support/envs/app/bin/python"
fi
if [[ -d "$local_support/tools/bin" ]]; then
  export PATH="$PATH:$local_support/tools/bin"
fi
if [[ "${APP_DEV:-0}" != 1 && ! -f frontend/dist/index.html ]]; then
  echo 'Соберите интерфейс командой npm --prefix frontend run build.' >&2
  exit 1
fi
ready_dir="$(mktemp -d "${TMPDIR:-/tmp}/meeting-worker.XXXXXX")"
worker_pid=''
server_pid=''
cleanup() {
  trap - EXIT INT TERM
  [[ -z "$server_pid" ]] || kill "$server_pid" 2>/dev/null || true
  [[ -z "$worker_pid" ]] || kill "$worker_pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -n "$worker_pid" ]] && kill -0 "$worker_pid" 2>/dev/null || break
    sleep .2
  done
  [[ -z "$worker_pid" ]] || kill -KILL "$worker_pid" 2>/dev/null || true
  [[ -z "$worker_pid" ]] || wait "$worker_pid" 2>/dev/null || true
  [[ -z "$server_pid" ]] || wait "$server_pid" 2>/dev/null || true
  rmdir "$ready_dir" 2>/dev/null || { rm -f "$ready_dir/ready"; rmdir "$ready_dir"; }
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$app_python" -m backend.worker --ready-file "$ready_dir/ready" &
worker_pid=$!
until [[ -f "$ready_dir/ready" ]]; do
  if ! kill -0 "$worker_pid" 2>/dev/null; then
    echo 'Исполнитель завершился при запуске.' >&2
    exit 1
  fi
  sleep .15
done
"$app_python" -m uvicorn backend.api:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers &
server_pid=$!
while kill -0 "$worker_pid" 2>/dev/null && kill -0 "$server_pid" 2>/dev/null; do
  sleep .5
done
echo 'Сервер или исполнитель завершился.' >&2
exit 1
