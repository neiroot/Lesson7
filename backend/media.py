import array
import json
import math
import os
import struct
import subprocess
import wave
from pathlib import PurePath

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from backend.db import AppError

FORMATS = "mov,matroska,webm,wav,mp3,aiff"
PCM_BYTES_PER_MS = 32  # 16 kHz, mono, signed 16-bit samples


async def receive_file(request, destination, limit, extensions):
    content_type, parameters = parse_options_header(request.headers.get("content-type", ""))
    boundary = parameters.get(b"boundary", b"")
    if content_type != b"multipart/form-data" or not 0 < len(boundary) <= 70:
        raise AppError("unsupported_type", "Ожидается загрузка файла через форму", 415)

    headers, field, value = {}, bytearray(), bytearray()
    received, size, parts = 0, 0, 0
    filename = None
    complete = False

    def append(buffer, data, start, end):
        if len(buffer) + end - start > 8192:
            raise AppError("invalid_upload", "Слишком длинный заголовок файла")
        buffer.extend(data[start:end])

    def part_begin():
        nonlocal parts
        parts += 1
        if parts > 1:
            raise AppError("invalid_upload", "Загрузите один файл за запрос")

    def header_end():
        headers[bytes(field).lower()] = bytes(value)
        field.clear()
        value.clear()

    def headers_finished():
        nonlocal filename
        disposition, options = parse_options_header(headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or options.get(b"name") != b"file" or not options.get(b"filename"):
            raise AppError("invalid_upload", "Выберите файл")
        raw = options[b"filename"].decode("utf-8", "replace").replace(chr(92), "/")
        filename = "".join(char for char in PurePath(raw).name if char.isprintable()).strip()
        suffix = PurePath(filename).suffix.lower()
        if suffix not in extensions:
            raise AppError("unsupported_type", f"Допустимые файлы — {', '.join(sorted(extensions))}", 415)
        if len(filename) > 240:
            filename = filename[:240 - len(suffix)] + suffix

    def part_data(data, start, end):
        nonlocal size
        size += end - start
        if size > limit:
            raise AppError("too_large", f"Размер файла превышает {limit // 2**20} МиБ", 413)
        target.write(data[start:end])

    def end():
        nonlocal complete
        complete = True

    callbacks = {
        "on_part_begin": part_begin,
        "on_header_field": lambda *args: append(field, *args),
        "on_header_value": lambda *args: append(value, *args),
        "on_header_end": header_end,
        "on_headers_finished": headers_finished,
        "on_part_data": part_data,
        "on_end": end,
    }
    try:
        with destination.open("xb") as target:
            parser = MultipartParser(boundary, callbacks)
            async for chunk in request.stream():
                received += len(chunk)
                if received > limit + 65536:
                    raise AppError("too_large", f"Размер файла превышает {limit // 2**20} МиБ", 413)
                parser.write(chunk)
            parser.finalize()
    except MultipartParseError:
        raise AppError("invalid_upload", "Форма загрузки повреждена") from None
    if not complete or not filename or not size:
        raise AppError("invalid_upload", "Файл пуст или загрузка прервалась")
    return filename


def tool(args, timeout, code):
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              check=True, timeout=timeout).stdout
    except FileNotFoundError:
        raise AppError("ffmpeg_missing", "Установите FFmpeg и FFprobe") from None
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        raise AppError(code, "Не удалось прочитать или декодировать медиафайл") from None


def probe(path):
    return json.loads(tool([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", FORMATS,
        "-show_format", "-show_streams",
        "-of", "json", str(path),
    ], 30, "invalid_media"))


def ebml_vint(data, pos, *, identifier=False):
    if pos >= len(data) or not data[pos]:
        raise ValueError
    width = 9 - data[pos].bit_length()
    if pos + width > len(data):
        raise ValueError
    value = int.from_bytes(data[pos:pos + width], "big")
    return (value if identifier else value ^ (1 << (7 * width))), pos + width


def is_webm(path):
    with path.open("rb") as source:
        head = source.read(4096)
    try:
        tag, cursor = ebml_vint(head, 0, identifier=True)
        length, cursor = ebml_vint(head, cursor)
        if tag != 0x1A45DFA3 or cursor + length > len(head):
            return False
        end = cursor + length
        while cursor < end:
            field, cursor = ebml_vint(head, cursor, identifier=True)
            length, cursor = ebml_vint(head, cursor)
            if cursor + length > end:
                return False
            if field == 0x4282:
                return head[cursor:cursor + length] == b"webm"
            cursor += length
    except ValueError:
        pass
    return False


def is_mp4(path):
    with path.open("rb") as source:
        header = source.read(4096)
    cursor = 0
    while cursor + 8 <= len(header):
        size, tag = struct.unpack_from(">I4s", header, cursor)
        body = cursor + 8
        if size == 1 and cursor + 16 <= len(header):
            size = struct.unpack_from(">Q", header, body)[0]
            body += 8
        if size < body - cursor or cursor + size > len(header):
            return False
        if tag == b"ftyp":
            major = header[body:body + 4]
            brands = [major] + [header[i:i + 4] for i in range(body + 8, cursor + size, 4)]
            return major not in {b"qt  ", b"M4A ", b"M4B "} and any(
                brand.startswith((b"iso", b"mp4")) or brand in {b"avc1", b"M4V ", b"MSNV", b"dash"}
                for brand in brands
            )
        cursor += size
    return False


def seconds(info):
    try:
        duration = float(info["format"]["duration"])
        if not math.isfinite(duration):
            raise ValueError
        return duration
    except (KeyError, ValueError, TypeError):
        raise AppError("invalid_duration", "Не удалось определить длительность файла") from None


def audio_stream(info):
    tracks = [track for track in info.get("streams", []) if track.get("codec_type") == "audio"]
    if not tracks:
        raise AppError("no_audio", "В файле нет аудиодорожки")
    return next((track for track in tracks if track.get("disposition", {}).get("default")), tracks[0])["index"]


def validate_video(path):
    info = probe(path)
    formats = set(info.get("format", {}).get("format_name", "").split(","))
    if not (("mp4" in formats and is_mp4(path)) or ("webm" in formats and is_webm(path))):
        raise AppError("invalid_container", "Поддерживаются видео в контейнерах MP4 и WebM")
    if not any(track.get("codec_type") == "video" and not track.get("disposition", {}).get("attached_pic")
               for track in info.get("streams", [])):
        raise AppError("no_video", "В файле нет видеодорожки")
    duration = seconds(info)
    if duration <= 0:
        raise AppError("invalid_duration", "Длительность видео должна быть больше нуля")
    return max(1, round(duration * 1000)), audio_stream(info)


def validate_sample(path, max_seconds):
    info = probe(path)
    formats = set(info.get("format", {}).get("format_name", "").split(","))
    if not formats & {"wav", "mp3", "m4a"} or any(
        track.get("codec_type") == "video" and not track.get("disposition", {}).get("attached_pic")
        for track in info.get("streams", [])
    ):
        raise AppError("invalid_sample", "Загрузите голосовой образец в WAV, MP3 или M4A")
    duration = seconds(info)
    if not 5 <= duration <= max_seconds:
        raise AppError("sample_duration", f"Длительность образца должна быть от 5 до {max_seconds} секунд")
    return audio_stream(info)


def extract(path, output, stream, cap_seconds=None):
    cap = ["-t", str(cap_seconds)] if cap_seconds is not None else []
    audio_format = "s16le" if output.suffix == ".pcm" else "wav"
    tool([
        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file",
        "-format_whitelist", FORMATS,
        "-i", str(path), "-map", f"0:{stream}", *cap, "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", audio_format, "-y", str(output),
    ], 100 if cap else None, "decode_failed")


def audio_length(path):
    if path.suffix == ".pcm":
        return path.stat().st_size // PCM_BYTES_PER_MS
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() // 16


def pcm(path, windows):
    if path.suffix == ".pcm":
        pieces = []
        with path.open("rb") as source:
            for start, end in windows:
                begin = max(0, start) * PCM_BYTES_PER_MS
                source.seek(begin)
                pieces.append(source.read(max(0, end * PCM_BYTES_PER_MS - begin)))
        return b"".join(pieces)
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise AppError("invalid_pcm", "Неверный формат подготовленного звука")
        frames = wav.getnframes()
        pieces = []
        for start, end in windows:
            begin = max(0, min(start * 16, frames))
            wav.setpos(begin)
            pieces.append(wav.readframes(max(0, min(end * 16, frames) - begin)))
        return b"".join(pieces)


def quality(path, windows, config):
    signal = array.array("h", pcm(path, windows))
    if os.sys.byteorder != "little":
        signal.byteswap()
    speech = 0
    clipped = 0
    for start in range(0, len(signal), 480):
        frame = signal[start:start + 480]
        energy = sum((sample / 32768) ** 2 for sample in frame) / len(frame)
        if energy and 10 * math.log10(energy) >= config.min_rms:
            speech += len(frame)
        clipped += sum(abs(sample) >= 32760 for sample in frame)
    reasons = []
    if speech < 5 * 16000:
        reasons.append("Меньше 5 секунд полезной речи или слишком тихий звук")
    if signal and clipped / len(signal) > config.max_clipping:
        reasons.append("Слышна перегрузка записи")
    return speech // 16, reasons


def cut(path, output, windows):
    temporary = output.with_suffix(".partial")
    try:
        with wave.open(str(temporary), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(pcm(path, windows))
        temporary.chmod(0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
