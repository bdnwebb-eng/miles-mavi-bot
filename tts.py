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


def enabled() -> bool:
    """True when a key and a configured voice exist."""
    return bool(os.getenv("ELEVENLABS_API_KEY")) and bool(
        cfg.settings().get("voice", {}).get("voice_id")
    )


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
