#!/usr/bin/env python3
# Music API server - the one process that plays the family music library.
#
# Runs on the host (outside the agent sandbox) and owns mpv, so playback is
# never tied to the agent's lifetime. The agent talks to it over localhost
# HTTP; the server translates the agent-facing /music paths to the real
# library location on disk.
#
# Run it from anywhere:
#
#     nohup ./music_server.py &
#
# Nothing personal lives in this file: the library path comes from
# $MUSIC_ROOT (default ~/Music), the port from $MUSIC_API_PORT (default
# 8765), state/log files go under $MUSIC_STATE (default
# ~/.local/state/music-server), and $MUSIC_AO=null silences mpv for smoke
# tests. It binds 127.0.0.1 only and does no auth - keep it on this machine.
#
# API (JSON, GET and POST):
#
#   GET  /health            liveness + config summary
#   GET  /status            what's playing, position, volume, stop_in
#   GET  /list              collections and playlists in the library
#   GET  /find?q=...        fuzzy song search
#   GET  /                  endpoint listing (for humans and agents)
#   POST /play              {"target": "...", "duration": seconds?}
#   POST /stop /skip /pause /resume
#   POST /volume            {"value": N}  (0-100)
#
# Targets are library names ("ella-jazz", "just dance 2025"), /music paths
# ("/music/ella"), or song queries ("mack the knife") which fuzzy-match file
# names. Playlists and folders shuffle and repeat; a single song plays once.
# Every play starts at 100% volume (Artie's standing preference). A
# "duration" makes the server stop the music after that many seconds, even
# if no agent is around by then.
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = int(os.environ.get("MUSIC_API_PORT", "8765"))
AGENT_ROOT = "/music"
REAL_ROOT = os.path.realpath(os.path.expanduser(os.environ.get("MUSIC_ROOT", "~/Music")))
STATE_DIR = os.environ.get("MUSIC_STATE", os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "music-server"))
SOCKFILE = os.path.join(STATE_DIR, "mpv.sock")
LOGFILE = os.path.join(STATE_DIR, "music.log")
EXTS = (".m4a", ".mp3", ".opus", ".webm", ".flac", ".aac", ".ogg", ".wav")
DEFAULT_VOLUME = 100  # Artie's preference (msg 1420): every play starts here
MIN_DURATION = 10
MAX_DURATION = 7 * 86400
START_TIME = time.time()

# Audio: the mpv child needs the logged-in desktop session's PipeWire/Pulse
# sockets. When the server is started from a terminal they are already in the
# environment; when started headless we point at /run/user/<uid>.
_runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
if os.path.isdir(_runtime):
    os.environ.setdefault("XDG_RUNTIME_DIR", _runtime)
    _pulse = os.path.join(_runtime, "pulse", "native")
    if os.path.exists(_pulse):
        os.environ.setdefault("PULSE_SERVER", "unix:" + _pulse)

# Everything the server remembers about playback. Only the server writes
# these; nothing survives a server restart (Artie re-runs the script himself).
LOCK = threading.RLock()
state = {
    "target": None,  # target as the caller gave it, e.g. "ella-jazz"
    "kind": None,  # "file" | "dir" | "m3u"
    "path": None,  # real path on disk that mpv is (or should be) playing
    "proc": None,  # Popen of the current mpv, for poll()/kill()
    "seen_player": False,  # player was up at least once for the current target
    "changed_at": 0.0,  # last time the player was (re)started
    "timer": None,  # threading.Timer for the stop-after-duration feature
    "stop_deadline": None,  # epoch seconds, or None
}


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOGFILE, "a") as f:
            f.write(line)
    except OSError:
        pass
    print(line, end="", flush=True)


# --- mpv control over its JSON IPC socket -------------------------------


def rpc(command):
    if not os.path.exists(SOCKFILE):
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        try:
            s.connect(SOCKFILE)
        except OSError:
            return None
        s.sendall((json.dumps({"command": command}) + "\n").encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
    try:
        return json.loads(buf.decode().splitlines()[0])
    except (ValueError, IndexError):
        return None


def get_prop(name):
    r = rpc(["get_property", name])
    return r.get("data") if r and r.get("error") == "success" else None


def player_responding():
    # Socket round-trip is the reliable liveness check (works regardless of
    # which process started the player, and pids are meaningless across the
    # sandbox/host PID namespaces anyway).
    if not os.path.exists(SOCKFILE):
        return False
    return rpc(["get_property", "core-idle"]) is not None


def start_player(path, kind):
    os.makedirs(STATE_DIR, exist_ok=True)
    args = ["mpv", "--no-video", "--really-quiet", "--gapless-audio=yes", "--input-ipc-server=%s" % SOCKFILE]
    ao = os.environ.get("MUSIC_AO")
    if ao:
        args.append("--ao=%s" % ao)
    if kind != "file":
        args += ["--shuffle", "--loop-playlist=inf"]
    args.append(path)
    if os.path.exists(SOCKFILE):
        os.remove(SOCKFILE)
    with open(LOGFILE, "ab") as logf:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    state["proc"] = proc
    for _ in range(40):
        if proc.poll() is not None:
            raise RuntimeError("mpv exited immediately - see %s" % LOGFILE)
        if os.path.exists(SOCKFILE):
            return
        time.sleep(0.25)
    raise RuntimeError("mpv started but no IPC socket appeared - see %s" % LOGFILE)


def stop_player():
    # polite quit over the socket, then SIGKILL only if it ignored us; socket
    # cleanup either way. Idempotent (callable when nothing is playing).
    if os.path.exists(SOCKFILE):
        rpc(["quit"])
        for _ in range(10):
            if not os.path.exists(SOCKFILE):
                break
            time.sleep(0.4)
    proc = state.get("proc")
    if proc is not None and proc.poll() is None and os.path.exists(SOCKFILE):
        try:
            proc.kill()
        except OSError:
            pass
    if os.path.exists(SOCKFILE):
        try:
            os.remove(SOCKFILE)
        except OSError:
            pass
    state["proc"] = None


# --- library resolution and path translation -----------------------------


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def to_real(agent_path):
    # /music or /music/... -> real path under REAL_ROOT; anything else None.
    if agent_path == AGENT_ROOT:
        return REAL_ROOT
    if agent_path.startswith(AGENT_ROOT + "/"):
        return os.path.join(REAL_ROOT, agent_path[len(AGENT_ROOT) + 1 :])
    return None


def to_agent(real_path):
    if not real_path:
        return None
    real_path = os.path.realpath(real_path)
    if real_path == REAL_ROOT:
        return AGENT_ROOT
    if real_path.startswith(REAL_ROOT + os.sep):
        return AGENT_ROOT + real_path[len(REAL_ROOT) :]
    return None


def all_audio_files():
    for ext in EXTS:
        for path in sorted(glob_all(REAL_ROOT, ext)):
            yield path


def glob_all(root, ext):
    # recursive glob without relying on glob's order guarantees across dirs
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(ext):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


STOPWORDS = {"the", "a", "an", "on", "in", "of", "for", "and", "with", "is", "at", "to", "my", "me", "be", "by", "or", "we", "it"}


def score_matches(query, min_hits=None):
    tokens = [norm(w) for w in query.split() if len(norm(w)) >= 3 and norm(w) not in STOPWORDS]
    if not tokens:
        return []
    if min_hits is None:
        # Strict by default: a multi-word query must hit at least two words,
        # so one stray token ("thing" in "zzz no such thing") can't hijack
        # the query and start an unrelated song. /find and the "closest"
        # suggestions pass min_hits=1 and stay permissive.
        min_hits = 2 if len(tokens) >= 2 else 1
    scored = []
    for path in all_audio_files():
        name = norm(os.path.basename(path))
        hits = sum(1 for t in tokens if t in name)
        if hits >= min_hits:
            scored.append((hits, -len(name), path))
    scored.sort(reverse=True)
    return [p for _, _, p in scored]


def resolve(target):
    # Returns (kind, real_path) or None. Only ever returns paths under
    # REAL_ROOT: /music paths map through to_real, names resolve under the
    # library, and absolute host paths are rejected outright.
    t = target.strip()
    if t.startswith(AGENT_ROOT):
        p = to_real(t)
        if p and os.path.exists(p):
            return ("m3u" if p.endswith(".m3u") else ("file" if os.path.isfile(p) else "dir"), p)
        return None
    if t.startswith("/") or (len(t) > 1 and t[1] == ":"):
        return None  # absolute host path - not part of the library
    direct = os.path.join(REAL_ROOT, t)
    if os.path.exists(direct):
        if direct.endswith(".m3u"):
            return ("m3u", direct)
        return ("file" if os.path.isfile(direct) else "dir", direct)
    nt = norm(t)
    pdir = os.path.join(REAL_ROOT, "playlists")
    if os.path.isdir(pdir):
        for f in sorted(os.listdir(pdir)):
            if f.endswith(".m3u") and norm(f[:-4]) == nt:
                return ("m3u", os.path.join(pdir, f))
    best_dir = None
    for root, dirs, _files in os.walk(REAL_ROOT):
        dirs.sort()
        if root == REAL_ROOT:
            continue
        rel = os.path.relpath(root, REAL_ROOT)
        if norm(rel) == nt and (best_dir is None or len(rel) < len(best_dir)):
            best_dir = rel
    if best_dir:
        return ("dir", os.path.join(REAL_ROOT, best_dir))
    matches = score_matches(t)
    if matches:
        return ("file", matches[0])
    return None


def audio_count(folder):
    return sum(1 for ext in EXTS for _ in glob_all(folder, ext))


# --- state transitions ----------------------------------------------------


def set_timer_locked(duration):
    if state["timer"]:
        state["timer"].cancel()
        state["timer"] = None
    state["stop_deadline"] = None
    if duration:
        state["stop_deadline"] = time.time() + duration
        timer = threading.Timer(duration, stop_expired)
        timer.daemon = True
        state["timer"] = timer
        timer.start()


def clear_target_locked():
    if state["timer"]:
        state["timer"].cancel()
    state.update(target=None, kind=None, path=None, timer=None, stop_deadline=None, seen_player=False)


def start_target_locked(target, kind, path, duration):
    # replaces whatever is playing; volume always resets to 100 per Artie
    if player_responding():
        stop_player()
    start_player(path, kind)
    rpc(["set_property", "volume", DEFAULT_VOLUME])
    state.update(target=target, kind=kind, path=path, seen_player=True, changed_at=time.time())
    set_timer_locked(duration)


def stop_expired():
    with LOCK:
        log("duration elapsed, stopping")
        clear_target_locked()
        stop_player()


def watchdog_loop():
    # Keeps playlists/folders alive across mpv crashes; treats a finished
    # single song as done and clears the target. Never touches the state
    # within the first 10s of a start, so a slow player boot can't be
    # mistaken for a death.
    while True:
        time.sleep(3)
        with LOCK:
            target = state["target"]
            kind = state["kind"]
            young = time.time() - state["changed_at"] < 10
        if not target:
            continue
        if young:
            continue
        if player_responding():
            continue
        with LOCK:
            if kind == "file":
                if state["seen_player"]:
                    log("single song finished; clearing target")
                    clear_target_locked()
            else:
                log("player died; restarting %s" % target)
                try:
                    start_player(state["path"], kind)
                    rpc(["set_property", "volume", DEFAULT_VOLUME])
                    state["changed_at"] = time.time()
                except RuntimeError as e:
                    log("restart failed: %s" % e)


# --- API responses ---------------------------------------------------------


def build_status():
    with LOCK:
        target = state["target"]
        kind = state["kind"]
        stop_in = max(0, int(state["stop_deadline"] - time.time())) if state["stop_deadline"] else None
    info = {
        "state": "stopped",
        "playing": False,
        "title": None,
        "position": None,
        "duration": None,
        "tracks": None,
        "volume": None,
        "target": target,
        "kind": kind,
        "playing_file": None,
        "stop_in": stop_in,
    }
    if target and player_responding():
        path = get_prop("path")
        pause = get_prop("pause")
        idle = get_prop("core-idle")
        title = get_prop("media-title")
        info.update(
            {
                "title": title,
                "position": get_prop("time-pos"),
                "duration": get_prop("duration"),
                "tracks": get_prop("playlist-count"),
                "volume": get_prop("volume"),
                "playing_file": to_agent(path) if path else None,
            }
        )
        if pause:
            info["state"] = "paused"
        elif idle:
            info["state"] = "idle"
        else:
            info["state"] = "playing"
        info["playing"] = title is not None and not pause and not idle
    return info


def list_library():
    if not os.path.isdir(REAL_ROOT):
        return {"collections": [], "playlists": [], "error": "MUSIC_ROOT %s not found" % REAL_ROOT}
    collections = []
    for name in sorted(os.listdir(REAL_ROOT)):
        full = os.path.join(REAL_ROOT, name)
        if not os.path.isdir(full) or name == "playlists":
            continue
        entry = {"name": name, "path": to_agent(full), "tracks": audio_count(full), "children": []}
        for sub in sorted(os.listdir(full)):
            subfull = os.path.join(full, sub)
            if os.path.isdir(subfull):
                entry["children"].append({"name": "%s/%s" % (name, sub), "path": to_agent(subfull), "tracks": audio_count(subfull)})
        collections.append(entry)
    playlists = []
    pdir = os.path.join(REAL_ROOT, "playlists")
    if os.path.isdir(pdir):
        for f in sorted(os.listdir(pdir)):
            if f.endswith(".m3u"):
                count = sum(1 for line in open(os.path.join(pdir, f)) if line.strip() and not line.startswith("#"))
                playlists.append({"name": f[:-4], "path": to_agent(os.path.join(pdir, f)), "tracks": count})
    return {"collections": collections, "playlists": playlists}


def root_doc():
    return {
        "service": "music server",
        "library": REAL_ROOT,
        "agent_view": AGENT_ROOT,
        "port": PORT,
        "endpoints": [
            "GET  /health",
            "GET  /status",
            "GET  /list",
            "GET  /find?q=...",
            'POST /play {"target": ..., ' '"duration": seconds?}',
            "POST /stop",
            "POST /skip",
            "POST /pause",
            "POST /resume",
            'POST /volume {"value": N}',
        ],
    }


def do_play(payload):
    target = (payload.get("target") or "").strip()
    if not target:
        return 400, {"error": "missing 'target'"}
    duration = payload.get("duration")
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return 400, {"error": "'duration' must be whole seconds"}
        if not (MIN_DURATION <= duration <= MAX_DURATION):
            return 400, {"error": "'duration' must be %d..%d seconds" % (MIN_DURATION, MAX_DURATION)}
    r = resolve(target)
    if r is None:
        return 404, {"error": "no match for %r" % target, "closest": [to_agent(p) for p in score_matches(target, min_hits=1)[:8]]}
    kind, path = r
    with LOCK:
        try:
            start_target_locked(target, kind, path, duration)
        except RuntimeError as e:
            return 500, {"error": str(e)}
        log("play %s (%s, %s%s)" % (target, kind, path, ", stop in %ds" % duration if duration else ""))
    return 200, {"ok": True, "target": target, "kind": kind, "path": to_agent(path), "status": build_status()}


def do_resume():
    with LOCK:
        if player_responding():
            rpc(["set_property", "pause", False])
            return 200, {"ok": True, "resumed": "unpaused", "status": build_status()}
        if state["target"]:
            try:
                start_player(state["path"], state["kind"])
                rpc(["set_property", "volume", DEFAULT_VOLUME])
                state["seen_player"] = True
                state["changed_at"] = time.time()
            except RuntimeError as e:
                return 500, {"error": str(e)}
            return 200, {"ok": True, "resumed": "restarted", "status": build_status()}
        return 409, {"error": "nothing to resume"}


class Handler(BaseHTTPRequestHandler):
    server_version = "MusicServer/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # per-request noise goes nowhere; state changes use log()

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise ValueError("body too large")
        if not length:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode())
        except ValueError:
            raise ValueError("body is not JSON")
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def do_GET(self):
        try:
            parts = urlparse(self.path)
            if parts.path in ("/", "/index"):
                self._send(200, root_doc())
            elif parts.path == "/health":
                self._send(200, {"ok": True, "pid": os.getpid(), "music_root": REAL_ROOT, "agent_root": AGENT_ROOT, "port": PORT, "uptime": int(time.time() - START_TIME)})
            elif parts.path == "/status":
                self._send(200, build_status())
            elif parts.path == "/list":
                self._send(200, list_library())
            elif parts.path == "/find":
                query = (parse_qs(parts.query).get("q") or [""])[0].strip()
                if not query:
                    self._send(400, {"error": "missing 'q' parameter"})
                else:
                    self._send(200, {"matches": [to_agent(p) for p in score_matches(query, min_hits=1)[:12]]})
            else:
                self._send(404, {"error": "unknown endpoint %s" % parts.path})
        except Exception as e:
            log("GET %s failed: %s" % (self.path, e))
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            payload = self._read_body()
            parts = urlparse(self.path)
            if parts.path == "/play":
                code, resp = do_play(payload)
            elif parts.path == "/stop":
                with LOCK:
                    if state["target"] or player_responding():
                        clear_target_locked()
                        stop_player()
                        log("stop")
                code, resp = 200, {"ok": True, "status": build_status()}
            elif parts.path == "/skip":
                if not player_responding():
                    code, resp = 409, {"error": "not playing"}
                else:
                    rpc(["playlist-next"])
                    time.sleep(1.2)  # let the next track actually load
                    code, resp = 200, {"ok": True, "status": build_status()}
            elif parts.path == "/pause":
                if not player_responding():
                    code, resp = 409, {"error": "not playing"}
                else:
                    rpc(["set_property", "pause", True])
                    code, resp = 200, {"ok": True, "status": build_status()}
            elif parts.path == "/resume":
                code, resp = do_resume()
            elif parts.path == "/volume":
                try:
                    value = int(payload.get("value"))
                except (TypeError, ValueError):
                    code, resp = 400, {"error": "'value' must be an integer 0-100"}
                else:
                    if not (0 <= value <= 100):
                        code, resp = 400, {"error": "'value' must be 0-100"}
                    elif not player_responding():
                        code, resp = 409, {"error": "not playing"}
                    else:
                        rpc(["set_property", "volume", value])
                        code, resp = 200, {"ok": True, "volume": value, "status": build_status()}
            else:
                code, resp = 404, {"error": "unknown endpoint %s" % parts.path}
            self._send(code, resp)
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            log("POST %s failed: %s" % (self.path, e))
            self._send(500, {"error": str(e)})


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    # If a player from a previous server life is still alive (server was
    # killed, mpv got orphaned), quit it so a fresh start never double-plays.
    # A stale socket from an already-dead player is just removed - no waiting.
    if os.path.exists(SOCKFILE):
        if rpc(["quit"]) is not None:
            for _ in range(10):
                if not os.path.exists(SOCKFILE):
                    break
                time.sleep(0.3)
            if os.path.exists(SOCKFILE):
                os.remove(SOCKFILE)
        else:
            os.remove(SOCKFILE)
    log("music server starting (pid %d, root %s, port %d)" % (os.getpid(), REAL_ROOT, PORT))
    if not os.path.isdir(REAL_ROOT):
        log("WARNING: %s not found - requests will fail until the library exists" % REAL_ROOT)
    threading.Thread(target=watchdog_loop, daemon=True).start()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("cannot bind %s:%d - is the server already running? (%s)" % (HOST, PORT, e), file=sys.stderr)
        sys.exit(1)

    def bye(_signum, _frame):
        log("shutting down")
        with LOCK:
            if state["target"] or player_responding():
                clear_target_locked()
                stop_player()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    print("music server on http://%s:%d  (library: %s)" % (HOST, PORT, REAL_ROOT), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
