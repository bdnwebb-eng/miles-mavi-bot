"""ElevenLabs text-to-speech for Miles: turns replies into Telegram-ready voice notes.

Reads the voice from config/settings.yaml (voice: block) and the API key from
ELEVENLABS_API_KEY. If either is missing, the bot simply stays text-only.
Output is Ogg/Opus, which Telegram accepts natively as a voice note.
"""
from __future__ import annotations

import logging
import os

import httpx

import config_loader as cfg

log = logging.getLogger("miles.tts")

MAX_TTS_CHARS = 900  # cost control: cap voice note length
STT_MODEL = "scribe_v1"


def enabled() -> bool:
    """True when a key and a configured voice exist."""
    return bool(os.getenv("ELEVENLABS_API_KEY")) and bool(
        cfg.settings().get("voice", {}).get("voice_id")
    )


def stt_enabled() -> bool:
    """True when speech to text can run (a key exists)."""
    return bool(os.getenv("ELEVENLABS_API_KEY"))


def transcribe(audio: bytes, mime_type: str = "audio/ogg",
               filename: str = "voice.ogg") -> str | None:
    """Transcribe a Telegram voice note (or audio file) with ElevenLabs Speech to
    Text. Returns the transcript text, or None on any failure; the caller degrades
    to a friendly retry message, never silence."""
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key or not audio:
        return None
    try:
        r = httpx.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": key},
            files={"file": (filename, audio, mime_type)},
            data={"model_id": STT_MODEL},
            timeout=60,  # long voice notes take a while
        )
        if r.status_code == 200:
            text = (r.json().get("text") or "").strip()
            return text or None
        log.warning("STT failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log.warning("STT error: %s", e)
    return None


def synthesize(text: str) -> bytes | None:
    """Return Ogg/Opus audio bytes for `text`, or None on any failure."""
    key = os.getenv("ELEVENLABS_API_KEY")
    voice = cfg.settings().get("voice", {})
    voice_id = voice.get("voice_id")
    if not key or not voice_id or not text:
        return None
    body = {
        "text": text[:MAX_TTS_CHARS],
        "model_id": voice.get("model_id", "eleven_multilingual_v2"),
        "voice_settings": {
            "stability": voice.get("stability", 0.55),
            "similarity_boost": voice.get("similarity_boost", 0.75),
        },
    }
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        "?output_format=opus_48000_64"
    )
    try:
        r = httpx.post(url, headers={"xi-api-key": key}, json=body, timeout=60)
        if r.status_code == 200:
            return r.content
        log.warning("TTS failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log.warning("TTS error: %s", e)
    return None
