#!/usr/bin/env bash

# run_agent.sh - launch agent.py inside a bubblewrap sandbox (home daemon build)
#
# usage: bash run_agent.sh [project_dir]
#
# if project_dir is omitted, the current working directory is used.
#
# env vars:
#   PQ_API_KEY           - single API key for the model provider (required)
#   TELEGRAM_BOT_TOKEN   - enables the send_telegram tool in agent
#   TELEGRAM_CHAT_ID     - fixed destination for send_telegram
#
# The model and browser tools are pinned in agent.py; PQ_MODEL and
# PQ_PLAYWRIGHT are deliberately not passed through, so a stray value in the
# daemon's inherited environment cannot change them.
#
# the agent code lives here (read-only inside sandbox at /agent)
# the project dir is where the agent reads and writes (read-write at /workspace)
#
# protects: $HOME entirely invisible, only project dir is writable
# .pq is shadowed with an empty tmpfs so the agent cannot see harness files
# allows: full network (the browser MCP connects to the host Chrome over CDP,
# and the host music server on localhost:8765 is reachable)
# no playwright browser-cache bind: the MCP server connects over CDP and does
# not launch a downloaded browser
#
# host capabilities, always on for the home music box (no opt-in toggles):
#   sound     - /dev/snd (direct ALSA) plus the user session's PipeWire /
#              PulseAudio sockets (/run/user/UID) so the agent's players are
#              heard through the machine's speakers/headphones
#   music     - host ~/Music read-only at /music; the music server translates
#              /music paths to the real library and serves its HTTP API on
#              localhost:8765
#   HF cache  - host ~/.cache/huggingface read-only at /hf-cache with
#              HF_HUB_OFFLINE=1, so local models load without any download

set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -eq 0 ]; then
    PROJECT_DIR="$(pwd)"
elif [ $# -eq 1 ]; then
    PROJECT_DIR="$(cd "$1" && pwd)"
else
    echo "usage: bash run_agent.sh [project_dir]" >&2
    echo "  if project_dir is omitted, the current working directory is used." >&2
    exit 1
fi

if ! command -v bwrap &>/dev/null; then
    echo "error: bwrap not found. install with: sudo apt install bubblewrap" >&2
    exit 1
fi

# audio: the logged-in desktop session's runtime dir holds the PipeWire and
# PulseAudio sockets (plus the dbus session bus). Binding it lets mpv/pactl
# reach the same sound server the user hears through. This also exposes the
# microphone to the sandbox - acceptable on a home music box. When no desktop
# session is logged in the dir is absent and --bind-try skips it; mpv then
# falls back to direct ALSA via the /dev/snd bind.
RUNTIME_DIR="/run/user/$(id -u)"

# env vars to pass into the sandbox. whitespace-only keys are truthy but
# produce an empty Bearer token (OpenRouter 401 "Missing Authentication
# header"); treat them as unset so agent.py's own "PQ_API_KEY is required"
# error fires instead. the :- default matters: this script runs with set -u.
ENV_ARGS=()
if [ -n "${PQ_API_KEY:-}" ] && [ -n "${PQ_API_KEY//[[:space:]]/}" ]; then
    ENV_ARGS+=(--setenv PQ_API_KEY "$PQ_API_KEY")
fi
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    ENV_ARGS+=(--setenv TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN")
fi
if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    ENV_ARGS+=(--setenv TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID")
fi

echo "agent dir  : $AGENT_DIR"
echo "project dir: $PROJECT_DIR"
echo "model      : pinned in agent.py"
echo "playwright : always on (CDP to host Chrome)"
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "telegram   : configured"
else
    echo "telegram   : not set"
fi
echo ""

exec bwrap \
  --ro-bind /usr /usr \
  --ro-bind-try /bin /bin \
  --ro-bind-try /lib /lib \
  --ro-bind-try /lib64 /lib64 \
  --ro-bind-try /sbin /sbin \
  --ro-bind /etc /etc \
  --proc /proc \
  --dev /dev \
  `# sound: direct ALSA access to the host audio hardware (headphones/speakers)` \
  --dev-bind-try /dev/snd /dev/snd \
  --tmpfs /dev/shm \
  --tmpfs /tmp \
  --tmpfs /home \
  --tmpfs /root \
  --tmpfs /run \
  `# sound: host user-session sockets (PipeWire/PulseAudio/dbus); absent when
   # no desktop session is logged in - mpv then falls back to ALSA` \
  --bind-try "$RUNTIME_DIR" "$RUNTIME_DIR" \
  --ro-bind-try /run/systemd/resolve /run/systemd/resolve \
  `# agent code is read-only - the agent cannot modify itself` \
  --ro-bind "$AGENT_DIR" /agent \
  `# project dir is the only writable location` \
  --bind "$PROJECT_DIR" /workspace \
  `# shadow .pq with empty tmpfs so agent cannot read harness files` \
  --tmpfs /workspace/.pq \
  `# HF caches commonly used by huggingface_hub / transformers` \
  --ro-bind-try "${HOME}/.cache/huggingface" /hf-cache \
  `# music library: host ~/Music is bound read-only at /music - the agent plays
   # from it but can never edit it, and the files stay out of the workdir` \
  --ro-bind-try "${HOME}/Music" /music \
  --unshare-pid \
  --unshare-ipc \
  --unshare-uts \
  --die-with-parent \
  --new-session \
  --clearenv \
  --setenv PATH /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  --setenv HOME /tmp \
  --setenv TMPDIR /tmp \
  --setenv XDG_RUNTIME_DIR "$RUNTIME_DIR" \
  --setenv HF_HOME /hf-cache \
  --setenv HUGGINGFACE_HUB_CACHE /hf-cache/hub \
  --setenv TRANSFORMERS_CACHE /hf-cache/hub \
  --setenv HF_HUB_OFFLINE 1 \
  --setenv HF_MODULES_CACHE /tmp/hf_modules \
  --setenv MUSIC_ROOT /music \
  "${ENV_ARGS[@]}" \
  --chdir /workspace \
  -- \
  python3 -u /agent/agent.py
