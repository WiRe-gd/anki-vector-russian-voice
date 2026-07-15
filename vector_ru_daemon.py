#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vector_ru_daemon.py — мост между wire-pod, локальной LLM и русским TTS
для робота Anki Vector.

Схема работы:
  wire-pod (custom intent) -> trigger.sh -> HTTP POST /ask -> этот демон
  демон: LLM (Ollama/LM Studio, OpenAI-совместимый API) -> TTS (Silero или
  XTTS v2) -> ffmpeg (WAV 16000 Гц / mono / 16-bit) -> SDK -> динамик Вектора.

Все настройки — через переменные окружения (см. блок CONFIG или systemd-юнит).
"""

import os
import re
import io
import json
import wave
import time
import shutil
import asyncio
import logging
import tempfile
import threading
import subprocess
from collections import deque

import numpy as np
import requests
import torch
from flask import Flask, request
from num2words import num2words

import anki_vector  # пакет wirepod-vector-sdk

# --------------------------------------------------------------------------
# CONFIG (переопределяется переменными окружения)
# --------------------------------------------------------------------------
LLM_URL        = os.environ.get("LLM_URL", "http://127.0.0.1:11434/v1/chat/completions")
LLM_MODEL      = os.environ.get("LLM_MODEL", "qwen2.5:7b")
LLM_API_KEY    = os.environ.get("LLM_API_KEY", "ollama")      # для Ollama — любой
LLM_TIMEOUT    = int(os.environ.get("LLM_TIMEOUT", "90"))     # сек
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "220"))

# Бэкенд: openai — напрямую в Ollama/LM Studio; homeassistant — через
# Conversation API Home Assistant (LLM + управление умным домом).
LLM_BACKEND    = os.environ.get("LLM_BACKEND", "openai")      # openai | homeassistant
HA_URL         = os.environ.get("HA_URL", "http://127.0.0.1:8123")
HA_TOKEN       = os.environ.get("HA_TOKEN", "")               # долгосрочный токен HA
HA_AGENT_ID    = os.environ.get("HA_AGENT_ID", "")            # напр. conversation.ollama_xxx
HA_LANGUAGE    = os.environ.get("HA_LANGUAGE", "ru")

# Эмоции: LLM начинает ответ тегом [радость]/[грусть]/..., демон проигрывает
# соответствующую анимацию перед репликой.
ENABLE_EMOTIONS = os.environ.get("ENABLE_EMOTIONS", "true").lower() in ("1", "true", "yes")

# Режим продолжения диалога: после ответа робот слушает продолжение
# без "Hey Vector" (звук с микрофонов через gRPC AudioFeed + локальный Vosk).
FOLLOWUP            = os.environ.get("FOLLOWUP", "false").lower() in ("1", "true", "yes")
VOSK_MODEL_PATH     = os.environ.get("VOSK_MODEL_PATH", "/opt/vector-ru/vosk-model-small-ru-0.22")
FOLLOWUP_MAX_ROUNDS = int(os.environ.get("FOLLOWUP_MAX_ROUNDS", "5"))   # реплик подряд
FOLLOWUP_WAIT_SEC   = float(os.environ.get("FOLLOWUP_WAIT_SEC", "4"))   # ждать начала речи
FOLLOWUP_SILENCE_SEC = float(os.environ.get("FOLLOWUP_SILENCE_SEC", "2.0"))  # тишина = конец фразы
FOLLOWUP_MAX_SEC    = float(os.environ.get("FOLLOWUP_MAX_SEC", "12"))   # потолок на фразу
FOLLOWUP_GAIN       = float(os.environ.get("FOLLOWUP_GAIN", "8"))       # усиление микрофона
MIC_SAMPLE_RATE     = 15625  # частота аудиофида Вектора (константа прошивки)

TTS_ENGINE     = os.environ.get("TTS_ENGINE", "silero")       # silero | xtts
SILERO_SPEAKER = os.environ.get("SILERO_SPEAKER", "xenia")    # aidar|baya|kseniya|xenia|eugene
XTTS_SPEAKER_WAV = os.environ.get("XTTS_SPEAKER_WAV", "")     # путь к образцу голоса 6-15 c (для xtts)

# --- Постобработка голоса "под Вектора" -----------------------------------
# VOICE_PITCH: множитель высоты тона. 1.0 = без изменений.
#   Сдвигаются и форманты (эффект "маленького робота", как у оригинала).
#   1.26 ≈ +4 полутона, 1.33 ≈ +5, 1.41 ≈ +6.
VOICE_PITCH    = float(os.environ.get("VOICE_PITCH", "1.0"))
# VOICE_TEMPO: скорость речи после питч-шифта (1.0 = нормальная)
VOICE_TEMPO    = float(os.environ.get("VOICE_TEMPO", "1.0"))
# ROBOT_FX: лёгкая "цифровая" окраска (битовый краш + подъём верхов)
ROBOT_FX       = os.environ.get("ROBOT_FX", "false").lower() in ("1", "true", "yes")

VOLUME         = int(os.environ.get("VOLUME", "80"))          # громкость 0-100
LISTEN_PORT    = int(os.environ.get("LISTEN_PORT", "8090"))
HISTORY_TURNS  = int(os.environ.get("HISTORY_TURNS", "6"))    # пар реплик в памяти
HISTORY_TTL    = int(os.environ.get("HISTORY_TTL", "300"))    # сек до сброса диалога

# Слова-триггеры ЧАТА: вырезаются из фразы перед отправкой в LLM
# ("вопрос почему небо синее" -> "почему небо синее").
# Командные слова умного дома (включи/выключи/...) сюда НЕ добавлять:
# они — часть команды и должны уходить в LLM/HA целиком. В wire-pod же
# в utterances кастомного интента перечисляются ОБА набора слов.
TRIGGERS = [t.strip() for t in os.environ.get(
    "TRIGGERS", "вопрос,спроси,поболтаем,слушай"
).split(",") if t.strip()]

SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT",
    "Ты — маленький домашний робот Вектор. Отвечай ТОЛЬКО по-русски, "
    "кратко и живо: одно-три коротких предложения. Никаких списков, "
    "markdown, эмодзи и английских слов. Все числа пиши словами. "
    "У тебя любопытный и дружелюбный характер. "
    "САМОЕ ПЕРВОЕ слово каждого ответа — тег твоей эмоции в квадратных "
    "скобках, один из: [радость] [грусть] [удивление] [злость] [нейтрально]. "
    "После тега сразу идёт ответ."
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-ru")

app = Flask(__name__)

# --------------------------------------------------------------------------
# TTS: инициализация выбранного движка (один раз при старте)
# --------------------------------------------------------------------------
torch.set_num_threads(max(1, (os.cpu_count() or 4) - 1))

silero_model = None
xtts_model = None

if TTS_ENGINE == "silero":
    log.info("Загружаю Silero TTS v4 (ru)... первый запуск скачает ~60 МБ")
    silero_model, _ = torch.hub.load(
        repo_or_dir="snakers4/silero-models",
        model="silero_tts",
        language="ru",
        speaker="v4_ru",
        trust_repo=True,  # без этого torch.hub задаёт вопрос в stdin и падает под systemd
    )
    silero_model.to(torch.device("cpu"))
    log.info("Silero готов, голос: %s", SILERO_SPEAKER)
elif TTS_ENGINE == "xtts":
    log.info("Загружаю Coqui XTTS v2... первый запуск скачает ~2 ГБ")
    from TTS.api import TTS  # pip install coqui-tts
    xtts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cpu")
    log.info("XTTS v2 готов")
else:
    raise SystemExit(f"Неизвестный TTS_ENGINE: {TTS_ENGINE}")

# --- Vosk для режима продолжения диалога -----------------------------------
vosk_model = None
if FOLLOWUP:
    try:
        import vosk
        vosk.SetLogLevel(-1)
        if os.path.isdir(VOSK_MODEL_PATH):
            log.info("Загружаю Vosk-модель: %s", VOSK_MODEL_PATH)
            vosk_model = vosk.Model(VOSK_MODEL_PATH)
            log.info("Vosk готов, режим продолжения диалога включён")
        else:
            log.warning("FOLLOWUP включён, но модель не найдена: %s — режим отключён",
                        VOSK_MODEL_PATH)
    except ImportError:
        log.warning("FOLLOWUP включён, но пакет vosk не установлен "
                    "(pip install vosk) — режим отключён")

# --------------------------------------------------------------------------
# Память диалога: serial -> (deque сообщений, время последней реплики)
# --------------------------------------------------------------------------
_histories = {}
_hist_lock = threading.Lock()
# Очередь на робота: не даём двум запросам говорить одновременно
_robot_lock = threading.Lock()


def _get_history(serial: str) -> deque:
    with _hist_lock:
        hist, ts = _histories.get(serial, (None, 0))
        if hist is None or time.time() - ts > HISTORY_TTL:
            hist = deque(maxlen=HISTORY_TURNS * 2)
        _histories[serial] = (hist, time.time())
        return hist


# --------------------------------------------------------------------------
# Обработка текста
# --------------------------------------------------------------------------
def strip_trigger(text: str) -> str:
    """Убирает кавычки, которыми wire-pod оборачивает !speechText.
    Чат-триггеры (вопрос/спроси/...) вырезаются из фразы; любые другие
    фразы (команды умного дома и т.п.) передаются дальше целиком."""
    text = text.strip().strip('"').strip()
    low = text.lower()
    for trig in sorted(TRIGGERS, key=len, reverse=True):
        idx = low.find(trig.lower())
        if idx != -1:
            tail = text[idx + len(trig):].strip(" ,.!?")
            # если после чат-триггера пусто — вернём пустоту (демон попросит
            # повторить); если триггер был частью слова команды — не режем
            return tail
    return text


_DIGIT_RE = re.compile(r"\d+")

def normalize_for_tts(text: str) -> str:
    """Silero не читает цифры — заменяем их словами, чистим markdown-мусор."""
    def _num(m):
        try:
            return num2words(int(m.group()), lang="ru")
        except Exception:
            return m.group()
    text = _DIGIT_RE.sub(_num, text)
    text = re.sub(r"[*_#`>|\[\]{}~^]", " ", text)   # markdown-символы
    text = re.sub(r"[a-zA-Z]{20,}", " ", text)       # случайный латинский мусор
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------
def ask_llm(serial: str, user_text: str) -> str:
    hist = _get_history(serial)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += list(hist)
    messages.append({"role": "user", "content": user_text})

    resp = requests.post(
        LLM_URL,
        headers={"Authorization": f"Bearer {LLM_API_KEY}",
                 "Content-Type": "application/json"},
        json={
            "model": LLM_MODEL,
            "messages": messages,
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": 0.7,
        },
        timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    answer = resp.json()["choices"][0]["message"]["content"].strip()

    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": answer})
    return answer


# Контекст диалога HA: serial -> conversation_id
_ha_conversations = {}

def ask_ha(serial: str, user_text: str) -> str:
    """Отправляет фразу в Home Assistant Conversation API (Assist).
    HA сам вызывает LLM и управляет устройствами."""
    payload = {"text": user_text, "language": HA_LANGUAGE}
    if HA_AGENT_ID:
        payload["agent_id"] = HA_AGENT_ID
    conv_id = _ha_conversations.get(serial)
    if conv_id:
        payload["conversation_id"] = conv_id
    resp = requests.post(
        f"{HA_URL.rstrip('/')}/api/conversation/process",
        headers={"Authorization": f"Bearer {HA_TOKEN}",
                 "Content-Type": "application/json"},
        json=payload, timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    _ha_conversations[serial] = data.get("conversation_id", conv_id)
    return data["response"]["speech"]["plain"]["speech"].strip()


# --- Эмоции: [тег] в начале ответа -> анимация на роботе -------------------
_EMOTION_RE = re.compile(
    r"^\s*\[?\s*(радость|грусть|удивление|злость|нейтрально)\s*\]?\s*[:,\-]?\s*",
    re.IGNORECASE)

# Кандидаты anim-триггеров по эмоциям; берётся первый, который реально есть
# в списке триггеров робота (список отличается между прошивками).
EMOTION_TRIGGERS = {
    "радость":   ["FistBumpSuccess", "GreetAfterLongTime", "OnboardingReactToFaceHappy"],
    "грусть":    ["FistBumpLeftHanging", "FistBumpRequestRetry", "FetchCubeFailure"],
    "удивление": ["ReactToPokeStartled", "ReactToObstacle", "SurpriseReaction"],
    "злость":    ["FrustratedByFailureMajor", "FailedToRightFromElevated", "ReactToPokeReaction"],
}

def extract_emotion(answer: str):
    """Возвращает (эмоция|None, текст без тега)."""
    m = _EMOTION_RE.match(answer)
    if m:
        return m.group(1).lower(), answer[m.end():].strip()
    return None, answer.strip()


# --------------------------------------------------------------------------
# Локальные команды: таймеры и громкость (обрабатываются без LLM)
# --------------------------------------------------------------------------
_NUM_WORDS = {
    "одну": 1, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30,
    "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
}

def _parse_duration(text: str):
    """'на пять минут', 'на 2 часа 30 минут', 'на полчаса' -> секунды или None."""
    t = text.lower()
    if "полчаса" in t:
        return 1800
    if "полтора часа" in t or "полтора часика" in t:
        return 5400
    secs, num = 0, 0
    for tok in re.findall(r"[а-яё]+|\d+", t):
        if tok.isdigit():
            num += int(tok)
        elif tok in _NUM_WORDS:
            num += _NUM_WORDS[tok]
        elif tok.startswith("час"):
            secs += (num or 1) * 3600; num = 0
        elif tok.startswith("минут"):
            secs += (num or 1) * 60; num = 0
        elif tok.startswith("секунд"):
            secs += (num or 1); num = 0
    return secs or None


def _plural(n: int, forms):  # forms = ("минуту","минуты","минут")
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]

def _human_duration(secs: int) -> str:
    parts = []
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h: parts.append(f"{num2words(h, lang='ru')} {_plural(h, ('час','часа','часов'))}")
    if m: parts.append(f"{num2words(m, lang='ru')} {_plural(m, ('минуту','минуты','минут'))}")
    if s: parts.append(f"{num2words(s, lang='ru')} {_plural(s, ('секунду','секунды','секунд'))}")
    return " ".join(parts) or "ноль секунд"


_timers = {}          # serial -> list[dict(timer, label, end_ts)]
_timers_lock = threading.Lock()

def _fire_timer(serial: str, label: str):
    with _timers_lock:
        lst = _timers.get(serial, [])
        _timers[serial] = [t for t in lst if t["label"] != label or t["end_ts"] > time.time() + 1]
    log.info("[%s] Таймер сработал: %s", serial, label)
    try:
        speak_standalone(serial, f"Динь-динь! Таймер на {label} завершён!", "радость")
    except Exception:
        log.exception("Не удалось объявить таймер")


def try_local_command(serial: str, text: str):
    """Возвращает текст ответа, если команда обработана локально, иначе None."""
    global VOLUME
    low = text.lower()

    if re.search(r"\b(потише|тише)\b", low):
        VOLUME = max(20, VOLUME - 20)
        return "Хорошо, буду говорить тише."
    if re.search(r"\b(погромче|громче)\b", low):
        VOLUME = min(100, VOLUME + 20)
        return "Хорошо, буду говорить громче."

    if "таймер" in low:
        if re.search(r"отмен|убери|удали|сбрось|останови", low):
            with _timers_lock:
                lst = _timers.pop(serial, [])
            for t in lst:
                t["timer"].cancel()
            return "Отменил таймеры." if lst else "Активных таймеров нет."
        if re.search(r"сколько|остал", low):
            with _timers_lock:
                lst = [t for t in _timers.get(serial, []) if t["end_ts"] > time.time()]
            if not lst:
                return "Активных таймеров нет."
            t = min(lst, key=lambda x: x["end_ts"])
            return f"Осталось {_human_duration(int(t['end_ts'] - time.time()))}."
        dur = _parse_duration(low)
        if dur:
            label = _human_duration(dur)
            timer = threading.Timer(dur, _fire_timer, args=(serial, label))
            timer.daemon = True
            timer.start()
            with _timers_lock:
                _timers.setdefault(serial, []).append(
                    {"timer": timer, "label": label, "end_ts": time.time() + dur})
            return f"Ставлю таймер на {label}."
        return "Не понял, на сколько поставить таймер. Скажи, например: таймер на пять минут."

    return None


# --------------------------------------------------------------------------
# Синтез речи -> WAV, пригодный для Вектора (16000 Гц, mono, s16)
# --------------------------------------------------------------------------
def _save_tensor_as_wav(audio_tensor: torch.Tensor, sample_rate: int, path: str):
    pcm = (audio_tensor.numpy() * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def synthesize(text: str, out_path: str):
    """Синтезирует text и кладёт готовый для робота WAV в out_path."""
    raw_path = out_path + ".raw.wav"
    if TTS_ENGINE == "silero":
        # Silero: лимит ~1000 символов на вызов — режем по предложениям
        chunks, buf = [], ""
        for sent in re.split(r"(?<=[.!?])\s+", text):
            if len(buf) + len(sent) > 800:
                chunks.append(buf); buf = sent
            else:
                buf = (buf + " " + sent).strip()
        if buf:
            chunks.append(buf)
        parts = []
        for ch in chunks:
            if not ch.strip():
                continue
            parts.append(silero_model.apply_tts(
                text=ch, speaker=SILERO_SPEAKER, sample_rate=24000))
        if not parts:
            raise ValueError("Пустой текст для синтеза")
        _save_tensor_as_wav(torch.cat(parts), 24000, raw_path)
    else:  # xtts
        kwargs = {"text": text, "language": "ru", "file_path": raw_path}
        if XTTS_SPEAKER_WAV:
            kwargs["speaker_wav"] = XTTS_SPEAKER_WAV
        else:
            kwargs["speaker"] = "Ana Florence"  # встроенный голос XTTS
        xtts_model.tts_to_file(**kwargs)

    # Постобработка "под Вектора" + конвертация под требования робота
    # (mono, 16-bit PCM, 16000 Гц).
    SRC_RATE = 24000  # и Silero, и XTTS отдают 24 кГц
    filters = []
    if abs(VOICE_PITCH - 1.0) > 0.01:
        # Классический приём Anki: поднимаем тон ВМЕСТЕ с формантами
        # (asetrate), затем возвращаем исходную длительность (atempo).
        filters.append(f"asetrate={int(SRC_RATE * VOICE_PITCH)}")
        filters.append(f"aresample={SRC_RATE}")
        filters.append(f"atempo={1.0 / VOICE_PITCH:.4f}")
    if abs(VOICE_TEMPO - 1.0) > 0.01:
        filters.append(f"atempo={VOICE_TEMPO:.4f}")
    if ROBOT_FX:
        filters.append("acrusher=bits=12:mode=log:mix=0.12")  # лёгкая цифра
        filters.append("treble=g=3")                            # звонкость
    filters.append("loudnorm=I=-16:TP=-1.5")  # выравнивание громкости

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", raw_path,
           "-af", ",".join(filters),
           "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", out_path]
    subprocess.run(cmd, check=True)
    os.remove(raw_path)


# --------------------------------------------------------------------------
# Проигрывание на роботе
# --------------------------------------------------------------------------
# Кэш списка anim-триггеров по серийнику (чтобы не дёргать робота каждый раз)
_anim_trigger_cache = {}

def _speak(robot, serial: str, wav_path: str, emotion: str = None):
    """Анимация эмоции (если есть) + проигрывание WAV на уже подключённом роботе."""
    if ENABLE_EMOTIONS and emotion in EMOTION_TRIGGERS:
        try:
            if serial not in _anim_trigger_cache:
                _anim_trigger_cache[serial] = set(robot.anim.anim_trigger_list)
            available = _anim_trigger_cache[serial]
            for trig in EMOTION_TRIGGERS[emotion]:
                if trig in available:
                    log.info("[%s] Эмоция '%s' -> анимация %s", serial, emotion, trig)
                    robot.anim.play_animation_trigger(trig)
                    break
        except Exception as e:
            log.warning("Анимация эмоции не сыграла: %s", e)
    robot.audio.stream_wav_file(wav_path, VOLUME)


def _extract_audio(resp) -> bytes:
    """Достаёт аудиосэмплы из AudioFeedResponse.

    В proto прошивки сэмплы — поле №5 (signal_power), но в схеме SDK-форка
    нет полей chunk_id/audio_chunk_count, и номера съехали: прошивочное поле 5
    парсер отбрасывает как неизвестное. Разбираем wire-формат вручную."""
    if resp.signal_power:          # вдруг схема всё же совпала
        return bytes(resp.signal_power)
    buf = resp.SerializeToString()  # unknown-поля сохраняются при сериализации
    i, out = 0, b""
    try:
        while i < len(buf):
            tag = shift = 0
            while True:
                b = buf[i]; i += 1
                tag |= (b & 0x7F) << shift; shift += 7
                if not b & 0x80:
                    break
            fn, wt = tag >> 3, tag & 7
            if wt == 0:      # varint
                while buf[i] & 0x80:
                    i += 1
                i += 1
            elif wt == 2:    # length-delimited
                ln = shift = 0
                while True:
                    b = buf[i]; i += 1
                    ln |= (b & 0x7F) << shift; shift += 7
                    if not b & 0x80:
                        break
                if fn == 5:  # signal_power по нумерации прошивки
                    out += buf[i:i + ln]
                i += ln
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
    except IndexError:
        pass
    return out


def listen_on_robot(robot) -> str:
    """Слушает микрофоны робота через gRPC AudioFeed и распознаёт фразу Vosk'ом.
    Возвращает текст или '' (тишина/таймаут)."""
    from anki_vector.messaging import protocol
    import vosk
    # Модель Vosk требует ровно 16000 Гц, робот отдаёт 15625 — ресемплируем
    rec = vosk.KaldiRecognizer(vosk_model, 16000)
    state = {"text": ""}

    def _resample(chunk: bytes) -> bytes:
        pcm = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        if pcm.size == 0:
            return b""
        pcm = np.clip(pcm * FOLLOWUP_GAIN, -32768, 32767)  # усиление тихого фида
        n_out = int(round(pcm.size * 16000 / MIC_SAMPLE_RATE))
        x_out = np.linspace(0, pcm.size - 1, n_out)
        out = np.interp(x_out, np.arange(pcm.size), pcm)
        return out.astype(np.int16).tobytes()

    async def _run():
        start = time.time()
        had_speech = False
        last_voice = time.time()
        n_chunks, n_bytes, peak = 0, 0, 0
        async for resp in robot.conn.grpc_interface.AudioFeed(
                protocol.AudioFeedRequest()):
            audio = _extract_audio(resp)
            n_chunks += 1
            n_bytes += len(audio)
            if audio:
                peak = max(peak, int(np.abs(
                    np.frombuffer(audio, dtype=np.int16)).max() or 0))
            if audio and rec.AcceptWaveform(_resample(audio)):
                t = json.loads(rec.Result()).get("text", "")
                if t:
                    state["text"] = t
                    return
            else:
                if json.loads(rec.PartialResult()).get("partial"):
                    had_speech = True
                    last_voice = time.time()
            now = time.time()
            if now - start > FOLLOWUP_MAX_SEC:
                break
            if had_speech and now - last_voice > FOLLOWUP_SILENCE_SEC:
                break
            if not had_speech and now - start > FOLLOWUP_WAIT_SEC:
                break
        state["text"] = json.loads(rec.FinalResult()).get("text", "")
        log.info("Аудиофид: чанков %d, аудио %d байт, пик %d",
                 n_chunks, n_bytes, peak)

    try:
        fut = asyncio.run_coroutine_threadsafe(_run(), robot.conn.loop)
        fut.result(timeout=FOLLOWUP_MAX_SEC + 10)
    except Exception as e:
        log.warning("Прослушка не удалась: %s", e)
    return state["text"].strip()


def _connect_robot(serial: str):
    """Подключение к роботу с одним повтором при таймауте."""
    last_err = None
    for attempt in (1, 2):
        try:
            robot = anki_vector.Robot(serial, cache_animation_lists=False)
            robot.connect()
            return robot
        except Exception as e:
            log.warning("Подключение к роботу (попытка %d) не удалось: %s", attempt, e)
            last_err = e
            time.sleep(1.5)
    raise RuntimeError(f"Робот недоступен: {last_err}")


def speak_standalone(serial: str, text: str, emotion: str = None):
    """Синтезировать и произнести фразу вне основного конвейера
    (используется таймерами и другими фоновыми событиями)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        synthesize(normalize_for_tts(text), wav_path)
        with _robot_lock:
            robot = _connect_robot(serial)
            try:
                _speak(robot, serial, wav_path, emotion)
            finally:
                robot.disconnect()
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def make_reply(serial: str, question: str):
    """Вопрос -> (путь к WAV, эмоция, текст ответа)."""
    local = try_local_command(serial, question)
    if local is not None:
        log.info("[%s] Локальная команда: %s", serial, local)
        answer = local
        emotion = "нейтрально"
    else:
        try:
            if LLM_BACKEND == "homeassistant":
                answer = ask_ha(serial, question)
            else:
                answer = ask_llm(serial, question)
        except Exception as e:
            log.error("LLM недоступна: %s", e)
            answer = "Не могу связаться со своим мозгом. Проверь сервер с моделью."
        emotion, answer = extract_emotion(answer)
    speak_text = normalize_for_tts(answer)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    synthesize(speak_text if speak_text else "Мне нечего сказать.", wav_path)
    return wav_path, emotion, answer


# --------------------------------------------------------------------------
# Основной конвейер (в отдельном потоке на каждый запрос)
# --------------------------------------------------------------------------
def pipeline(raw_text: str, serial: str):
    t0 = time.time()
    wav_path = None
    try:
        question = strip_trigger(raw_text)
        if not question:
            question = None
        else:
            log.info("[%s] Вопрос: %s", serial, question)

        if question:
            wav_path, emotion, answer = make_reply(serial, question)
            log.info("[%s] Ответ (%.1f c): %s", serial, time.time() - t0, answer)
        else:
            wav_path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
            synthesize("Скажи вопрос вместе со словом-триггером.", wav_path)
            emotion = None

        followup_on = FOLLOWUP and vosk_model is not None
        with _robot_lock:
            time.sleep(1.2)  # даём отработать анимации интента wire-pod
            # cache_animation_lists=False: предзагрузка полного списка анимаций
            # часто ловит DEADLINE_EXCEEDED; триггеры получаем лениво в _speak
            robot = _connect_robot(serial)
            try:
                _speak(robot, serial, wav_path, emotion)
                os.remove(wav_path); wav_path = None

                rounds = 0
                while followup_on and rounds < FOLLOWUP_MAX_ROUNDS:
                    log.info("[%s] Слушаю продолжение...", serial)
                    text = listen_on_robot(robot)
                    if not text or len(text) < 3:
                        log.info("[%s] Тишина — завершаю диалог", serial)
                        break
                    log.info("[%s] Продолжение: %s", serial, text)
                    wav_path, emotion, answer = make_reply(serial, strip_trigger(text))
                    log.info("[%s] Ответ: %s", serial, answer)
                    _speak(robot, serial, wav_path, emotion)
                    os.remove(wav_path); wav_path = None
                    rounds += 1
            finally:
                robot.disconnect()
        log.info("[%s] Готово, всего %.1f c", serial, time.time() - t0)
    except Exception:
        log.exception("Ошибка в конвейере")
    finally:
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
@app.route("/ask", methods=["POST"])
def ask():
    text = request.form.get("text") or (request.json or {}).get("text", "")
    serial = request.form.get("serial") or (request.json or {}).get("serial", "")
    serial = serial.strip().strip('"')
    if not text or not serial:
        return "need text and serial", 400
    threading.Thread(target=pipeline, args=(text, serial), daemon=True).start()
    return "ok", 200


@app.route("/health", methods=["GET"])
def health():
    return f"ok, engine={TTS_ENGINE}", 200


if __name__ == "__main__":
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg не найден — apt install ffmpeg")
    log.info("Демон запущен на порту %d, TTS=%s, LLM=%s (%s)",
             LISTEN_PORT, TTS_ENGINE, LLM_URL, LLM_MODEL)
    app.run(host="0.0.0.0", port=LISTEN_PORT, threaded=True)
