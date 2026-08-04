#!/usr/bin/env python3

import os
import sys
import time
import traceback
import subprocess
from dataclasses import dataclass
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

try:
    from dotenv import load_dotenv

    # Load from the daemon's tree so cwd does not matter for TELEGRAM_/PQ_* etc.
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from telegram import TelegramClient
from telegram.client import TelegramClientError

import timers
import rheem_api
import commands
import transcribe

# How often to poll telegram (seconds). Longer = less battery/cpu but slower response.
POLL_TIMEOUT = 10

# Where downloaded Telegram media is written (created on start if missing).
MEDIA_DIR = "media"

# Telegram iPhone/Android push-to-talk notes arrive as message.voice (Ogg Opus).
# Anything else (photos, docs, music attachments, video notes) is refused.
ACCEPTED_MEDIA_TYPE = "voice"
ACCEPTED_MIME_MARKERS = ("ogg", "opus")

ROOT = os.path.dirname(os.path.abspath(__file__))
LIFE_DIR = os.path.join(ROOT, "life")
HARNESS_RUN = os.path.join(ROOT, "harness", "run_agent.sh")
AGENT_LOG = os.path.join(LIFE_DIR, "agent.log")
LOCAL_TZ_NAME = "America/Los_Angeles"

# One in-flight agent; further freeform notes wait here.
_agent_proc = None
_pending = []


@dataclass
class Context:
    _client: TelegramClient

    @property
    def timers(self):
        return timers

    @property
    def rheem(self):
        return rheem_api

    def send(self, msg):
        self._client.send(msg)

    def ask(self, question, timeout=300):
        return self._client.ask(question, timeout=timeout)


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def local_now_iso():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(LOCAL_TZ_NAME)).isoformat(timespec="seconds")
        except Exception:
            pass
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_life():
    os.makedirs(LIFE_DIR, exist_ok=True)
    project = os.path.join(LIFE_DIR, "project.md")
    if not os.path.isfile(project):
        # Bare fallback if the checked-in constitution is missing.
        with open(project, "w", encoding="utf-8") as f:
            f.write("You help the user with personal notes and next actions. " "This directory (/workspace) is yours to organize. " "Use send_telegram for short replies.\n")
        log(f"Wrote default {project}")


def write_p_md(text, source, meta):
    ensure_life()
    message_id = meta.get("message_id", "")
    received = meta.get("received_at") or local_now_iso()
    lines = [
        f"Source: {source}",
        f"Telegram message id: {message_id}",
        f"Received: {received}",
        "",
        "---",
        "",
        (text or "").rstrip(),
        "",
    ]
    path = os.path.join(LIFE_DIR, "p.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def agent_env():
    # Inherit process env (dotenv-loaded). Force the cash-saving life-bot model so
    # an outer coding harness PQ_MODEL cannot leak into phone helper jobs.
    env = os.environ.copy()
    env["PQ_MODEL"] = "dsv4-flash"
    if not env.get("PQ_PLAYWRIGHT"):
        env["PQ_PLAYWRIGHT"] = "1"
    return env


def start_agent(job):
    global _agent_proc
    text = job["text"]
    source = job["source"]
    meta = job.get("meta") or {}
    p_path = write_p_md(text, source, meta)
    ensure_life()
    log_f = open(AGENT_LOG, "a", encoding="utf-8")
    log_f.write("\n===== agent start {0} source={1} msg={2} =====\n".format(local_now_iso(), source, meta.get("message_id", "")))
    log_f.flush()
    try:
        _agent_proc = subprocess.Popen(
            ["bash", HARNESS_RUN, LIFE_DIR],
            cwd=ROOT,
            env=agent_env(),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        log_f.close()
        _agent_proc = None
        log(f"Failed to spawn agent: {e}")
        raise
    # Parent can close; child keeps the fd via dup.
    log_f.close()
    log(f"Spawned agent pid={_agent_proc.pid} p.md={p_path} " f"source={source} msg={meta.get('message_id', '')} chars={len(text)}")


def poll_agent():
    global _agent_proc
    if _agent_proc is None:
        return
    rc = _agent_proc.poll()
    if rc is None:
        return
    log(f"Agent exited rc={rc} pid={_agent_proc.pid}")
    _agent_proc = None
    if _pending:
        nxt = _pending.pop(0)
        log(f"Draining queue ({len(_pending)} still waiting); starting next job")
        try:
            start_agent(nxt)
        except Exception as e:
            log(f"Failed to start queued agent: {e}")
            traceback.print_exc()


def handoff_to_agent(ctx, text, source="voice", meta=None):
    # Drop thin p.md + one background run_agent.sh over life/. Never block the
    # daemon loop on the agent; house cmds and timers keep running.
    global _agent_proc
    meta = dict(meta or {})
    meta.setdefault("received_at", local_now_iso())
    text = (text or "").strip()
    if not text:
        log("handoff_to_agent called with empty text; ignoring")
        return

    preview = text if len(text) <= 200 else text[:200] + "..."
    log("Agent handoff request source={0} chars={1}: {2}".format(source, len(text), preview.replace("\n", " ")))

    job = {"text": text, "source": source, "meta": meta}

    poll_agent()
    if _agent_proc is not None:
        _pending.append(job)
        log(f"Agent busy; queued job (queue depth={len(_pending)})")
        # One-line ack so the phone is not silent while a long run touches many files.
        ctx.send("Queued — still finishing the previous note.")
        return

    try:
        start_agent(job)
    except Exception as e:
        log(f"Agent spawn failed: {e}")
        traceback.print_exc()
        ctx.send(f"Could not start helper agent: {e}")


def process_timers(ctx):
    due = timers.get_due()
    for t in due:
        name = t["name"]
        log(f"Timer fired: {name}")

        handler = commands.TIMER_HANDLERS.get(name)
        if handler:
            try:
                response = handler(ctx, t)
                if response:
                    ctx.send(response)
            except Exception as e:
                log(f"Timer handler error: {e}")
                traceback.print_exc()

        if t.get("interval"):
            timers.reschedule(t["id"], t["interval"])
            log(f"Rescheduled {name} for {t['interval']}s")
        else:
            timers.delete(t["id"])


def is_accepted_voice(msg):
    if (msg.media_type or "") != ACCEPTED_MEDIA_TYPE:
        return False
    mime = (msg.mime_type or "").lower()
    if not mime:
        # Telegram nearly always sets audio/ogg for voice; allow empty only as fallback.
        return True
    return any(marker in mime for marker in ACCEPTED_MIME_MARKERS)


def safe_unlink(path):
    if not path:
        return
    try:
        os.unlink(path)
        log(f"Deleted media file: {path}")
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"Could not delete {path}: {e}")


def reject_media(ctx, msg):
    kind = msg.media_type or "media"
    mime = msg.mime_type or "?"
    log(f"Rejected media type={kind} mime={mime}")
    # If an older path left a download, wipe it.
    safe_unlink(msg.local_path)
    msg.local_path = ""
    ctx.send(f"Unsupported media ({kind}, mime={mime}). " "Send a Telegram voice note only (Ogg Opus). " "Photos, video, files, and music attachments are ignored.")


def process_voice(ctx, client, msg):
    # Download only after acceptance so junk never lands on disk.
    path = msg.local_path
    if not path:
        try:
            path = client.download_media(msg)
            msg.local_path = path
        except TelegramClientError as e:
            log(f"Voice download failed: {e}")
            ctx.send(f"Got a voice note but failed to download it: {e}")
            return

    abs_path = os.path.abspath(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = msg.file_size or 0
    dur = msg.duration or 0
    log(f"Voice accepted path={abs_path} bytes={size} duration={dur}s mime={msg.mime_type}")
    ctx.send("Got voice note — transcribing...")

    text = ""
    payload = {}
    try:
        text, payload = transcribe.transcribe_file(path)
    except Exception as e:
        log(f"Transcription error: {e}")
        traceback.print_exc()
        ctx.send(f"Transcription failed: {e}")
        safe_unlink(path)
        msg.local_path = ""
        return

    # Audio is disposable once we have text for the agent.
    safe_unlink(path)
    msg.local_path = ""

    text = (text or "").strip()
    if not text:
        log("Transcription returned empty text")
        ctx.send("Transcription came back empty. Try speaking closer or a longer note.")
        return

    elapsed = payload.get("elapsed_sec")
    tokens = payload.get("generated_tokens")
    log(f"Transcription ok chars={len(text)} elapsed={elapsed} tokens={tokens}")
    handoff_to_agent(
        ctx,
        text,
        source="voice",
        meta={
            "elapsed_sec": elapsed,
            "generated_tokens": tokens,
            "duration": dur,
            "message_id": msg.message_id,
            "received_at": local_now_iso(),
        },
    )


def process_media(ctx, client, msg):
    if not is_accepted_voice(msg):
        reject_media(ctx, msg)
        return
    process_voice(ctx, client, msg)


def process_message(ctx, client, msg):
    if msg.has_media:
        process_media(ctx, client, msg)
        return

    text = (msg.text or "").strip()
    if not text:
        log(f"Ignored empty message id={msg.message_id}")
        return

    log(f"Message: {text}")

    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    handler = commands.COMMANDS.get(cmd)
    if handler:
        try:
            response = handler(ctx, args)
            if response:
                ctx.send(response)
        except Exception as e:
            log(f"Command error: {e}")
            traceback.print_exc()
            ctx.send(f"Error: {e}")
    else:
        # Freeform text becomes a life-notebook job, same as voice transcripts.
        handoff_to_agent(
            ctx,
            text,
            source="text",
            meta={
                "message_id": msg.message_id,
                "received_at": local_now_iso(),
            },
        )


def main():
    log("Starting home automation daemon...")
    log(f"STT model={transcribe.MODEL} device={transcribe.DEVICE} dtype={transcribe.DTYPE} " f"(lazy-load on first voice note)")
    log(f"Agent harness={HARNESS_RUN} life={LIFE_DIR} " f"PQ_MODEL={os.environ.get('PQ_MODEL', 'dsv4-flash')} " f"PQ_PLAYWRIGHT={os.environ.get('PQ_PLAYWRIGHT', '1')}")

    os.makedirs(MEDIA_DIR, exist_ok=True)
    log(f"Media directory: {os.path.abspath(MEDIA_DIR)}")
    ensure_life()

    if not os.path.isfile(HARNESS_RUN):
        log(f"ERROR: missing agent launcher {HARNESS_RUN}")
        sys.exit(1)

    client = TelegramClient(media_dir=MEDIA_DIR)
    if not client.is_registered():
        log("ERROR: Telegram not configured. Run telegram setup first.")
        sys.exit(1)

    ctx = Context(_client=client)

    log("Clearing old messages...")
    # Do not download any pending media on the drain pass.
    client.receive(timeout=0, download_media=False)

    log("Initializing scheduled tasks...")
    commands.init(ctx)

    log("Ready. Waiting for commands / voice notes / freeform notes...")
    ctx.send("Home daemon started. Send 'help' for house commands. " "Voice notes and freeform text go to the life helper agent.")

    while True:
        try:
            process_timers(ctx)
            poll_agent()

            # Media policy is enforced in process_media; no auto-download of photos/etc.
            messages = client.receive(timeout=POLL_TIMEOUT, download_media=False)

            for msg in messages:
                process_message(ctx, client, msg)

            poll_agent()

        except KeyboardInterrupt:
            log("Shutting down...")
            break
        except Exception as e:
            log(f"Error in main loop: {e}")
            traceback.print_exc()
            time.sleep(5)

    if _agent_proc is not None and _agent_proc.poll() is None:
        log(f"Leaving agent pid={_agent_proc.pid} running after daemon exit")
    log("Goodbye")


if __name__ == "__main__":
    main()
