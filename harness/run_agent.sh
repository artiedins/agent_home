#!/usr/bin/env bash

# run_agent.sh - launch agent.py inside a bubblewrap sandbox
#
# usage: bash run_agent.sh [project_dir]
#        PQ_MODEL=dsv4-nitro PQ_PLAYWRIGHT=1 bash run_agent.sh [project_dir]
#
# if project_dir is omitted, the current working directory is used.
#
# env vars:
#   PQ_MODEL             - which model to use (default: dsv4-nitro)
#   PQ_API_KEY           - single API key for any model that needs auth
#   PQ_PLAYWRIGHT        - 1 to enable web search via headed Chrome, 0 to disable
#   TELEGRAM_BOT_TOKEN   - optional; enables send_telegram tool in agent
#   TELEGRAM_CHAT_ID     - optional; fixed destination for send_telegram
#
# the agent code lives here (read-only inside sandbox at /agent)
# the project dir is where the agent reads and writes (read-write at /workspace)
#
# protects: $HOME entirely invisible, only project dir is writable
# .pq is shadowed with an empty tmpfs so the agent cannot see harness files
# allows: full network (playwright needs it, local models need localhost)
# playwright browser cache read-only

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

PW_CACHE="${HOME}/.cache/ms-playwright"

if ! command -v bwrap &>/dev/null; then
    echo "error: bwrap not found. install with: sudo apt install bubblewrap" >&2
    exit 1
fi

# env vars to pass into the sandbox
ENV_ARGS=()
if [ -n "${PQ_MODEL:-}" ]; then
    ENV_ARGS+=(--setenv PQ_MODEL "$PQ_MODEL")
fi
if [ -n "${PQ_API_KEY:-}" ]; then
    ENV_ARGS+=(--setenv PQ_API_KEY "$PQ_API_KEY")
fi
if [ -n "${PQ_PLAYWRIGHT:-}" ]; then
    ENV_ARGS+=(--setenv PQ_PLAYWRIGHT "$PQ_PLAYWRIGHT")
fi
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    ENV_ARGS+=(--setenv TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN")
fi
if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    ENV_ARGS+=(--setenv TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID")
fi

echo "agent dir  : $AGENT_DIR"
echo "project dir: $PROJECT_DIR"
echo "model      : ${PQ_MODEL:-(default)}"
echo "playwright : ${PQ_PLAYWRIGHT:-1 (default)}"
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
  --tmpfs /dev/shm \
  --tmpfs /tmp \
  --tmpfs /home \
  --tmpfs /root \
  --tmpfs /run \
  --ro-bind-try /run/systemd/resolve /run/systemd/resolve \
  --ro-bind "$AGENT_DIR" /agent \
  --bind "$PROJECT_DIR" /workspace \
  --tmpfs /workspace/.pq \
  --ro-bind-try "$PW_CACHE" /pw-cache \
  --ro-bind-try "${HOME}/.cache/huggingface" /hf-cache \
  --unshare-pid \
  --unshare-ipc \
  --unshare-uts \
  --die-with-parent \
  --new-session \
  --clearenv \
  --setenv PATH /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  --setenv HOME /tmp \
  --setenv TMPDIR /tmp \
  --setenv PLAYWRIGHT_BROWSERS_PATH /pw-cache \
  --setenv AGENT_DIR /agent \
  --setenv HF_HOME /hf-cache \
  --setenv HUGGINGFACE_HUB_CACHE /hf-cache/hub \
  --setenv TRANSFORMERS_CACHE /hf-cache/hub \
  --setenv HF_HUB_OFFLINE 1 \
  --setenv HF_MODULES_CACHE /tmp/hf_modules \
  "${ENV_ARGS[@]}" \
  --chdir /workspace \
  -- \
  python3 -u /agent/agent.py

