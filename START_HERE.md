# AI-анализатор видеовстреч

Локальное учебное приложение для macOS на Apple Silicon. Оно принимает WebM и MP4,
извлекает речь, сохраняет расшифровку и создаёт конспект: краткое содержание,
решения, задачи и открытые вопросы.

## Первый запуск

Нужны Python 3.12, Node.js 24, FFmpeg/FFprobe, Ollama, интернет и около 15 ГБ свободного места.

1. Запустите Ollama в отдельном окне Терминала:

   ```bash
   OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NO_CLOUD=1 \
     OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve
   ```

2. Во втором окне из корня распакованного проекта выполните:

   ```bash
   brew install python@3.12 node@24 ffmpeg git-lfs
   export PATH="$(brew --prefix node@24)/bin:$PATH"
   python3.12 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r backend/requirements.lock
   npm --prefix frontend ci
   npm --prefix frontend run build
   cp -n .env.example .env
   ollama pull qwen3.5:2b --runner llamacpp
   ```

3. Примите условия [pyannote Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1),
   создайте Hugging Face токен только для чтения и подготовьте модели:

   ```zsh
   read -rs 'meeting_token?Токен Hugging Face: '
   printf '\n'
   HF_TOKEN="$meeting_token" python scripts/setup_models.py --check
   HF_TOKEN="$meeting_token" python scripts/setup_models.py
   unset meeting_token
   python scripts/check_models.py --offline
   ```

4. Запустите приложение:

   ```bash
   bash scripts/start.sh
   ```

Откройте <http://127.0.0.1:8000>. После загрузки WebM/MP4 обработка начнётся автоматически.
Для следующего запуска достаточно запустить Ollama, активировать `.venv` и повторить шаг 4.

## Модели и инструменты

- **FFmpeg/FFprobe** — проверка WebM/MP4 и извлечение аудио.
- **pyannote Community-1** — диаризация: определение интервалов речи разных спикеров.
- **WeSpeaker ResNet34** — голосовые признаки для сопоставления речи с локальными профилями.
- **Qwen3-ASR 0.6B, 8-bit, MLX** — преобразование речи в текст.
- **Qwen3.5 2B через Ollama** — конспект с решениями, задачами и открытыми вопросами.
- **FastAPI + React/TypeScript + SQLite** — API, одностраничный интерфейс и локальное хранение.

Токен Hugging Face нужен только для первичной загрузки закрытых весов. В архиве нет
токенов, API-ключей, `.env`, видеозаписей, баз данных и весов моделей. Вся AI-обработка после
подготовки выполняется локально. Подробности и команды тестов есть в `README.md`.
