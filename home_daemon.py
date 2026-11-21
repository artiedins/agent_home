#!/usr/bin/env python3

import os
import queue
import shutil
import sys
import threading
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
    # override=True so a stray whitespace/placeholder PQ_API_KEY inherited from
    # the launching shell cannot shadow the real key in .env (dotenv's default
    # override=False keeps an already-set var, even a garbage one).
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except ImportError:
    pass

from telegram import TelegramClient
from telegram.client import TelegramClientError, TelegramPermanentError

import timers
import rheem_api
import commands
import transcribe
import weather_station

# How often to poll telegram (seconds). Longer = less battery/cpu but slower response.
POLL_TIMEOUT = 10

# Telegram iPhone/Android push-to-talk notes arrive as message.voice (Ogg Opus),
# are transcribed locally, and are unlinked before handoff. Photos, documents,
# audio, and video attachments land directly in life/inbox_media/ for the agent
# to inspect, file into /workspace, or delete.
VOICE_MEDIA_TYPE = "voice"
VOICE_MIME_MARKERS = ("ogg", "opus")
ATTACHMENT_MEDIA_TYPES = ("photo", "document", "audio", "video", "video_note")

# A phone photo arrives over a home uplink that stalls for tens of seconds at a
# time. daemon.log 2026-11-20 06:19: two photos, sent right after a voice note,
# each died on a single 30 s read timeout from getFile; nothing retried, the
# update carrying them had already been confirmed to Telegram, so both were
# gone and the waiting agent sat 30 min on an empty inbox_media/. So the fetch
# runs on its own thread with spaced retries, and the daemon keeps polling
# Telegram meanwhile instead of going deaf for the length of the stall.
# Delays between attempts; the sum (~9 min) is the retry window.
ATTACHMENT_RETRY_SEC = (0, 5, 20, 60, 180, 300)
_download_jobs = queue.Queue()
_attachment_results = queue.Queue()

ROOT = os.path.dirname(os.path.abspath(__file__))
LIFE_DIR = os.path.join(ROOT, "life")
INBOX_MEDIA_DIR = os.path.join(LIFE_DIR, "inbox_media")
HARNESS_RUN = "/home/artie/Projects/pq_agent/run_agent.sh"
# Agent stdout/stderr capture, one file per run appended. It sits in the
# daemon's own tree, outside the notebook the model reads (life/ is mounted at
# /workspace, this file is not), and beside daemon.log so one command follows
# both: `tail -f daemon.log agent.log`. It was life/agent.log, then
# logs/agent.log; the 2026-11-08 move back to the root was Artie's call.
# No cap and no rotation: he reads it, then deletes or truncates it by hand.
AGENT_LOG = os.path.join(ROOT, "agent.log")
LOCAL_TZ_NAME = "America/Los_Angeles"

# Drop box for notes pushed into a live agent run: the harness drains one file
# per note while a run is alive, so a text or voice transcript bypasses the
# queue. The daemon and the bwrap sandbox share this directory (the life dir is
# bind-mounted at /workspace), and the dot name keeps it out of the workspace
# snapshot the model sees at startup.
INBOX_DIR = os.path.join(LIFE_DIR, ".inbox")
# A note with no live agent to receive it is reclaimed as a queued job after
# this long. Guards the case where the agent died between the write and the
# drain.
INBOX_STALE_SEC = 20

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

    @property
    def log(self):
        # Resolved at call time so commands can log lines into nohup.out without
        # importing this module (which imports commands).
        return log

    def send(self, msg, parse_mode=None):
        self._client.send(msg, parse_mode=parse_mode)

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
            f.write(
                "You help the user with personal notes and next actions. This directory (/workspace) is yours to organize; "
                "move durable files out of inbox_media/ into organized folders or delete disposable ones after handling them. "
                "Use python3 skills/telegram/telegram.py send for short replies.\n"
            )
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


def write_inbox_message(text, source, meta):
    # atomic drop for a live run: write beside the target name, then rename, so
    # the agent never reads a partial file. The nanosecond prefix keeps drain
    # order deterministic without depending on mtime resolution.
    os.makedirs(INBOX_DIR, exist_ok=True)
    message_id = meta.get("message_id", "")
    received = meta.get("received_at") or local_now_iso()
    name = "msg-{0:019d}-{1}.md".format(time.time_ns(), str(message_id or "0"))
    final = os.path.join(INBOX_DIR, name)
    tmp = final + ".tmp"
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
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, final)
    return final


def agent_env():
    # Inherit process env (dotenv-loaded). Leave PQ_MODEL unset so the unified
    # harness default (go-dsv41f) governs every run; scrub any inherited PQ_MODEL
    # and configure unified harness mode flags explicitly for the life daemon.
    env = os.environ.copy()
    env.pop("PQ_MODEL", None)
    env["PQ_PLAYWRIGHT"] = "1"
    env["PQ_BROWSE_ASSIST"] = "0"
    env["PQ_PREVIOUS_SESSIONS"] = "0"
    env["PQ_LIVE_INBOX"] = "1"
    # Normalize PQ_API_KEY: a whitespace-only value is truthy, so it sails past
    # run_agent.sh's -n test and agent.py's key check and yields an
    # "Authorization: Bearer " header with an empty token -> OpenRouter 401
    # "Missing Authentication header". Treat whitespace as unset and say so,
    # so the failure is loud instead of a confusing 401.
    key = (env.get("PQ_API_KEY") or "").strip()
    if not key:
        log("WARNING: PQ_API_KEY missing or blank in daemon env; the life agent will fail to authenticate")
    env["PQ_API_KEY"] = key
    return env


def open_agent_log():
    # Append only: a restart or a crash can never lose a line, and nothing here
    # caps or rotates the file. Artie reads it and trims it by hand once he is
    # done with the history.
    return open(AGENT_LOG, "a", encoding="utf-8")


def start_agent(job):
    global _agent_proc
    text = job["text"]
    source = job["source"]
    meta = job.get("meta") or {}
    p_path = write_p_md(text, source, meta)
    ensure_life()
    log_f = open_agent_log()
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


def reclaim_stranded_inbox(ctx):
    # At-least-once: a note pushed at the instant a run died has no reader, so
    # re-queue it instead of leaving it in the inbox forever. Only acts with no
    # live agent, so it cannot double-deliver against a running run (the harness
    # unlinks each file as it drains it).
    #
    # Not routed through handoff_to_agent on purpose: that would jump the
    # stranded note ahead of jobs already in _pending, send a second ack to the
    # phone, and pass the file's header block through as the p.md body.
    if _agent_proc is not None:
        return
    try:
        names = sorted(n for n in os.listdir(INBOX_DIR) if n.endswith(".md"))
    except OSError:
        return
    now = time.time()
    for name in names:
        path = os.path.join(INBOX_DIR, name)
        try:
            if now - os.path.getmtime(path) < INBOX_STALE_SEC:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            os.unlink(path)
        except OSError:
            continue
        # strip the header block so p.md does not get a second one
        body = content.split("\n---\n", 1)[1].strip() if "\n---\n" in content else content.strip()
        if not body:
            continue
        log(f"Reclaimed stranded inbox note {name} ({len(body)} chars); no live agent")
        job = {
            "text": body,
            "source": "text",
            "meta": {"received_at": local_now_iso(), "reclaimed": name},
        }
        if _pending or _agent_proc is not None:
            _pending.append(job)
            log(f"Reclaimed note queued (queue depth={len(_pending)})")
            continue
        try:
            start_agent(job)
        except Exception as e:
            log(f"Failed to start reclaimed agent: {e}")
            traceback.print_exc()
            _pending.append(job)


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
        try:
            path = write_inbox_message(text, source, meta)
            log(f"Pushed note to live agent inbox: {path}")
            # One-line ack so the phone confirms the push fired rather than the
            # queue; for a time-sensitive code that difference matters.
            ctx.send("Sent to the live agent.")
            return
        except OSError as e:
            log(f"Inbox write failed ({e}); falling back to the queue")
            _pending.append(job)
            log(f"Agent busy; queued job (queue depth={len(_pending)})")
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
            timers.reschedule_interval(t["id"], t["interval"])
            log(f"Rescheduled {name} for {t['interval']}s")
        else:
            timers.delete(t["id"])


def is_accepted_voice(msg):
    if (msg.media_type or "") != VOICE_MEDIA_TYPE:
        return False
    mime = (msg.mime_type or "").lower()
    if not mime:
        # Telegram nearly always sets audio/ogg for voice; allow empty only as fallback.
        return True
    return any(marker in mime for marker in VOICE_MIME_MARKERS)


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
    safe_unlink(msg.local_path)
    msg.local_path = ""
    ctx.send(f"Unsupported media ({kind}, mime={mime}).")


def tell_live_agent(text, meta=None):
    # Only a run that is already alive can act on this. Spawning a fresh run to
    # report a failed download would put an unasked-for message on the phone,
    # and the phone is told directly anyway.
    if _agent_proc is None or _agent_proc.poll() is not None:
        return False
    try:
        path = write_inbox_message(text, "attachment-error", meta or {})
    except OSError as e:
        log(f"Could not tell the live agent about the failed attachment: {e}")
        return False
    log(f"Told the live agent about the failed attachment: {path}")
    return True


def attachment_worker(client, jobs, results, log_fn):
    # One download at a time, in arrival order. Runs on its own TelegramClient
    # so nothing here touches the poller's update offset.
    while True:
        msg = jobs.get()
        if msg is None:
            return
        path = ""
        error = ""
        permanent = False
        for attempt, delay in enumerate(ATTACHMENT_RETRY_SEC, start=1):
            if delay:
                time.sleep(delay)
            try:
                path = client.download_media(msg)
                error = ""
                if attempt > 1:
                    log_fn(f"Attachment fetch recovered on attempt {attempt} ({msg.media_type} msg={msg.message_id})")
                break
            except TelegramPermanentError as e:
                # Telegram answered and refused (expired file_id, over the 20 MB
                # bot limit). Waiting changes nothing.
                error = str(e)
                permanent = True
                log_fn(f"Attachment fetch refused ({msg.media_type} msg={msg.message_id}): {e}")
                break
            except Exception as e:
                # Anything else (timeouts, dropped connections, a full disk) is
                # worth the next slot in the schedule; the worker must never die
                # on one bad file, or every later attachment queues forever.
                error = str(e)
                log_fn(
                    f"Attachment fetch attempt {attempt}/{len(ATTACHMENT_RETRY_SEC)} failed "
                    f"({msg.media_type} msg={msg.message_id}): {type(e).__name__}: {e}"
                )
        results.put((msg, path, error, permanent))


def process_attachment(ctx, client, msg):
    # The fetch is queued rather than done here: see ATTACHMENT_RETRY_SEC.
    if not msg.local_path:
        log(f"Queued {msg.media_type} download msg={msg.message_id}")
        _download_jobs.put(msg)
        return
    stage_attachment(ctx, msg, msg.local_path)


def drain_attachments(ctx):
    while True:
        try:
            msg, path, error, permanent = _attachment_results.get_nowait()
        except queue.Empty:
            return
        stage_attachment(ctx, msg, path, error, permanent)


def stage_attachment(ctx, msg, path, error="", permanent=False):
    os.makedirs(INBOX_MEDIA_DIR, exist_ok=True)
    if error or not path:
        # Telegram has already confirmed this update, so there is nothing left
        # to retry from: say so plainly and offer the resend.
        log(f"Attachment download gave up ({msg.media_type} msg={msg.message_id} permanent={permanent}): {error}")
        if permanent:
            phone = f"Telegram refused that {msg.media_type} ({error}). It did not reach the agent; send it again."
            note = (
                f"A {msg.media_type} you sent (Telegram message {msg.message_id}) never arrived: "
                f"{error}. It is not on disk, so do not go looking for it. "
                "Ask for a resend if it still matters."
            )
        else:
            minutes = sum(ATTACHMENT_RETRY_SEC) / 60
            phone = (
                f"Could not fetch that {msg.media_type} from Telegram after {minutes:.0f} min "
                "of retries (network timeouts). It did not reach the agent; send it again."
            )
            note = (
                f"A {msg.media_type} you sent (Telegram message {msg.message_id}) never arrived: "
                f"the download kept timing out ({minutes:.0f} min of retries). "
                "It is not on disk, so do not go looking for it. Ask for a resend if it still matters."
            )
        ctx.send(phone)
        tell_live_agent(note, {"message_id": msg.message_id, "received_at": local_now_iso()})
        return

    base = os.path.basename(path)
    staged_path = os.path.join(INBOX_MEDIA_DIR, base)
    try:
        if os.path.abspath(path) != os.path.abspath(staged_path):
            shutil.move(path, staged_path)
            msg.local_path = staged_path
        size = os.path.getsize(staged_path)
    except OSError as e:
        log(f"Failed placing attachment in {staged_path}: {e}")
        ctx.send(f"Failed placing {msg.media_type} attachment for agent: {e}")
        return

    rel_path = os.path.join("inbox_media", base)
    caption = (msg.caption or "").strip()
    details = [
        f"Attached {msg.media_type}: `{rel_path}`",
        f"MIME type: {msg.mime_type or 'application/octet-stream'}",
        f"Size: {size} bytes",
    ]
    if msg.duration:
        details.append(f"Duration: {msg.duration}s")
    if caption:
        details.append("")
        details.append(caption)
    else:
        details.append("")
        details.append(f"(No caption supplied with {msg.media_type}.)")

    log(f"Attachment staged type={msg.media_type} path={staged_path} bytes={size} mime={msg.mime_type}")
    handoff_to_agent(
        ctx,
        "\n".join(details),
        source=msg.media_type,
        meta={
            "message_id": msg.message_id,
            "received_at": local_now_iso(),
            "media_path": rel_path,
            "mime_type": msg.mime_type,
            "file_size": size,
        },
    )


def process_voice(ctx, client, msg):
    # Download only after acceptance so junk never lands on disk.
    os.makedirs(INBOX_MEDIA_DIR, exist_ok=True)
    path = msg.local_path
    if not path:
        try:
            path = client.download_media(msg)
            msg.local_path = path
        except TelegramClientError as e:
            log(f"Voice download failed: {e}")
            ctx.send("Could not fetch that voice note from Telegram (network timeout). Send it again.")
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
    if is_accepted_voice(msg):
        process_voice(ctx, client, msg)
        return
    if (msg.media_type or "") in ATTACHMENT_MEDIA_TYPES:
        process_attachment(ctx, client, msg)
        return
    reject_media(ctx, msg)


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
    log(f"Agent harness={HARNESS_RUN} life={LIFE_DIR} " f"model default from unified harness, PQ_API_KEY from daemon env")
    log(f"Agent stdout capture: {AGENT_LOG} (appended, never rotated)")

    ensure_life()
    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(INBOX_MEDIA_DIR, exist_ok=True)
    log(f"Live-agent inbox: {INBOX_DIR} (media dir: {INBOX_MEDIA_DIR})")

    # Weather station ingest. In its own thread so the one command
    # `nohup python3 -u home_daemon.py &` covers Telegram and the roof station
    # both. A bind failure (usually a daemon that is already running) logs and
    # returns None; it must never take the daemon down with it.
    weather_station.start(log=log)

    if not os.path.isfile(HARNESS_RUN):
        log(f"ERROR: missing agent launcher {HARNESS_RUN}")
        sys.exit(1)

    client = TelegramClient(media_dir=INBOX_MEDIA_DIR, log=log)
    if not client.is_registered():
        log("ERROR: Telegram not configured. Run telegram setup first.")
        sys.exit(1)

    # Attachment fetches: own client, own thread, spaced retries. The main loop
    # keeps polling Telegram while a stalled download is retried.
    download_client = TelegramClient(media_dir=INBOX_MEDIA_DIR, log=log)
    threading.Thread(
        target=attachment_worker,
        args=(download_client, _download_jobs, _attachment_results, log),
        name="attachment-download",
        daemon=True,
    ).start()
    log(
        "Attachment downloads: background thread, "
        f"{len(ATTACHMENT_RETRY_SEC)} attempts over {sum(ATTACHMENT_RETRY_SEC) / 60:.0f} min"
    )

    ctx = Context(_client=client)

    log("Clearing old messages...")
    # Do not download any pending media on the drain pass.
    client.receive(timeout=0, download_media=False)

    log("Initializing scheduled tasks...")
    commands.init(ctx)

    log("Ready. Waiting for commands / voice notes / freeform notes...")
    ws_note = (
        f"Weather station ingest listening on port {weather_station.PORT}." if weather_station.LISTENER is not None else f"Weather station ingest NOT started (port {weather_station.PORT} busy?)."
    )
    ctx.send(f"Home daemon started. {ws_note} Send 'help' for house commands. Voice notes, photos, files, and freeform text go to the life helper agent.")

    while True:
        try:
            process_timers(ctx)
            poll_agent()
            reclaim_stranded_inbox(ctx)

            # Media policy is enforced in process_media; no auto-download of photos/etc.
            messages = client.receive(timeout=POLL_TIMEOUT, download_media=False)

            for msg in messages:
                process_message(ctx, client, msg)

            drain_attachments(ctx)
            poll_agent()
            reclaim_stranded_inbox(ctx)

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
