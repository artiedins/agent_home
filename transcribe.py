#!/usr/bin/env python3

import os
import threading

# Hub id works offline when HF cache already has the snapshot.
DEFAULT_MODEL = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
MODEL = os.environ.get("MOSS_MODEL", DEFAULT_MODEL)
DEVICE = os.environ.get("MOSS_DEVICE", "cpu")
DTYPE = os.environ.get("MOSS_DTYPE", "fp32")
MAX_NEW_TOKENS = int(os.environ.get("MOSS_MAX_NEW_TOKENS", "2048"))

_runner = None
_lock = threading.Lock()


def get_runner():
    global _runner
    with _lock:
        if _runner is None:
            # Heavy import/load kept inside so the daemon can start without GPU stack taxes.
            from moss_transcribe_diarize.app.model_runner import ModelRunner

            _runner = ModelRunner(MODEL, device=DEVICE, dtype=DTYPE)
        return _runner


def plain_text(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        from moss_transcribe_diarize.transcript_parser import parse_transcript

        segments = parse_transcript(raw)
    except Exception:
        segments = []
    if segments:
        parts = []
        for seg in segments:
            t = (seg.text or "").strip()
            if t:
                parts.append(t)
        if parts:
            return " ".join(parts)
    return raw


def transcribe_file(audio_path, max_new_tokens=None):
    if max_new_tokens is None:
        max_new_tokens = MAX_NEW_TOKENS
    runner = get_runner()
    result = runner.transcribe(
        audio_path,
        max_new_tokens=max_new_tokens,
        decoding="greedy",
    )
    payload = result.to_dict()
    text = plain_text(payload.get("text") or "")
    return text, payload


if __name__ == "__main__":
    import sys

    path = sys.argv[1]
    text, payload = transcribe_file(path)
    print(text)
    print("elapsed=%.1fs tokens=%s" % (payload.get("elapsed_sec") or 0, payload.get("generated_tokens")))
