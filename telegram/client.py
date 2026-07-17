import os
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from .config import Config, load_config

# Default dump directory for received media (relative to process cwd).
DEFAULT_MEDIA_DIR = "media"


@dataclass
class Message:
    chat_id: int
    text: str
    timestamp: datetime
    message_id: int
    raw: dict
    caption: str = ""
    media_type: str = ""
    file_id: str = ""
    mime_type: str = ""
    file_name: str = ""
    duration: int = 0
    file_size: int = 0
    local_path: str = ""

    @property
    def has_media(self):
        return bool(self.media_type and self.file_id)

    @classmethod
    def from_update(cls, update):
        msg = update.get("message") or {}
        if not msg:
            return None

        text = msg.get("text") or ""
        caption = msg.get("caption") or ""
        media_type = ""
        file_id = ""
        mime_type = ""
        file_name = ""
        duration = 0
        file_size = 0

        if "voice" in msg:
            f = msg["voice"]
            media_type = "voice"
            file_id = f.get("file_id", "")
            mime_type = f.get("mime_type", "audio/ogg")
            duration = f.get("duration", 0)
            file_size = f.get("file_size", 0)
            file_name = "voice.ogg"
        elif "audio" in msg:
            f = msg["audio"]
            media_type = "audio"
            file_id = f.get("file_id", "")
            mime_type = f.get("mime_type", "audio/mpeg")
            duration = f.get("duration", 0)
            file_size = f.get("file_size", 0)
            file_name = f.get("file_name") or "audio.mp3"
        elif "document" in msg:
            f = msg["document"]
            media_type = "document"
            file_id = f.get("file_id", "")
            mime_type = f.get("mime_type", "application/octet-stream")
            file_size = f.get("file_size", 0)
            file_name = f.get("file_name") or "document.bin"
        elif "video" in msg:
            f = msg["video"]
            media_type = "video"
            file_id = f.get("file_id", "")
            mime_type = f.get("mime_type", "video/mp4")
            duration = f.get("duration", 0)
            file_size = f.get("file_size", 0)
            file_name = f.get("file_name") or "video.mp4"
        elif "video_note" in msg:
            f = msg["video_note"]
            media_type = "video_note"
            file_id = f.get("file_id", "")
            mime_type = "video/mp4"
            duration = f.get("duration", 0)
            file_size = f.get("file_size", 0)
            file_name = "video_note.mp4"
        elif "photo" in msg:
            # Telegram sends several sizes; pick the largest.
            sizes = msg["photo"]
            f = sizes[-1] if sizes else {}
            media_type = "photo"
            file_id = f.get("file_id", "")
            mime_type = "image/jpeg"
            file_size = f.get("file_size", 0)
            file_name = "photo.jpg"

        # Skip non-text messages without media we know how to handle (stickers, etc.)
        if not text and not media_type:
            return None

        return cls(
            chat_id=msg["chat"]["id"],
            text=text,
            caption=caption,
            timestamp=datetime.fromtimestamp(msg.get("date", 0)),
            message_id=msg.get("message_id", 0),
            raw=update,
            media_type=media_type,
            file_id=file_id,
            mime_type=mime_type,
            file_name=file_name,
            duration=duration,
            file_size=file_size,
        )


class TelegramClientError(Exception):
    pass


class TelegramAPI:
    BASE_URL = "https://api.telegram.org/bot{token}/{method}"
    FILE_URL = "https://api.telegram.org/file/bot{token}/{file_path}"

    def __init__(self, token):
        self.token = token
        self._last_update_id = 0

    def _url(self, method):
        return self.BASE_URL.format(token=self.token, method=method)

    def call(self, method, params=None, timeout=30):
        try:
            response = requests.post(
                self._url(method),
                json=params or {},
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                error_desc = data.get("description", "Unknown error")
                raise TelegramClientError(f"Telegram API error: {error_desc}")

            return data.get("result", {})

        except requests.RequestException as e:
            raise TelegramClientError(f"Request failed: {e}")

    def get_me(self):
        return self.call("getMe")

    def send_message(self, chat_id, text, parse_mode=None):
        params = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self.call("sendMessage", params)

    def get_updates(self, timeout=30):
        params = {
            "offset": self._last_update_id + 1,
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        updates = self.call("getUpdates", params, timeout=timeout + 5)

        if updates:
            self._last_update_id = max(u["update_id"] for u in updates)

        return updates

    def get_file(self, file_id):
        # Returns dict with file_path used for download.
        return self.call("getFile", {"file_id": file_id})

    def download_file(self, file_path, dest_path):
        url = self.FILE_URL.format(token=self.token, file_path=file_path)
        try:
            response = requests.get(url, timeout=120, stream=True)
            response.raise_for_status()
            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
            return dest_path
        except requests.RequestException as e:
            raise TelegramClientError(f"Download failed: {e}")


class TelegramClient:
    def __init__(self, config=None, media_dir=None):
        self.config = config or load_config()
        self._api = None
        self.media_dir = media_dir or DEFAULT_MEDIA_DIR

    @property
    def api(self):
        if self._api is None:
            self._api = TelegramAPI(self.config.bot_token)
        return self._api

    def health_check(self):
        try:
            self.api.get_me()
            return True
        except TelegramClientError:
            return False

    def is_registered(self):
        return bool(self.config.bot_token and self.config.chat_id)

    def send(self, message, chat_id=None):
        chat_id = chat_id or self.config.chat_id
        self.api.send_message(chat_id, message)
        return True

    def notify(self, message, prefix=True):
        if prefix and self.config.question_prefix:
            message = f"{self.config.question_prefix} {message}"
        return self.send(message)

    def _safe_name(self, name):
        # Keep basename only and strip path separators from Telegram-provided names.
        name = os.path.basename(name or "file")
        name = name.replace("\x00", "")
        if not name or name in (".", ".."):
            name = "file"
        return name

    def download_media(self, msg):
        if not msg.file_id:
            return ""

        info = self.api.get_file(msg.file_id)
        remote_path = info.get("file_path") or ""
        if not remote_path:
            raise TelegramClientError("getFile returned no file_path")

        ext = os.path.splitext(remote_path)[1]
        original = self._safe_name(msg.file_name)
        if original and not os.path.splitext(original)[1] and ext:
            original = original + ext
        if not original or original == "file":
            original = os.path.basename(remote_path) or f"file{ext or ''}"

        # message_id + epoch makes collisions unlikely across restarts.
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = f"{stamp}_m{msg.message_id}_{original}"
        dest = os.path.join(self.media_dir, base)

        self.api.download_file(remote_path, dest)
        return dest

    def receive(self, timeout=0, download_media=True):
        updates = self.api.get_updates(timeout=timeout)

        messages = []
        for update in updates:
            msg = Message.from_update(update)
            if not msg or msg.chat_id != self.config.chat_id:
                continue

            if download_media and msg.has_media:
                try:
                    msg.local_path = self.download_media(msg)
                except TelegramClientError as e:
                    # Leave local_path empty; caller can report failure.
                    msg.local_path = ""
                    msg.raw["_download_error"] = str(e)

            messages.append(msg)

        return messages

    def ask(self, question, context=None, timeout=None, options=None):
        timeout = timeout or self.config.default_timeout

        parts = []
        if self.config.question_prefix:
            parts.append(self.config.question_prefix)
        parts.append(question)

        if context:
            max_len = self.config.max_context_length
            if len(context) > max_len:
                context = context[: max_len - 3] + "..."
            parts.append(f"\n\nContext: {context}")

        if options:
            parts.append(f"\n\nOptions: {', '.join(options)}")

        if len(parts) <= 2:
            message = " ".join(parts)
        else:
            message = parts[0] + " " + parts[1] + "".join(parts[2:])

        # Clear pending messages
        self.receive(timeout=0)
        self.send(message)

        # Wait for response
        start_time = time.time()
        while time.time() - start_time < timeout:
            remaining = int(timeout - (time.time() - start_time))
            poll_time = min(remaining, 30)

            if poll_time <= 0:
                break

            messages = self.receive(timeout=poll_time)
            if messages:
                # Prefer textual content for ask(); fall back to caption text.
                reply = messages[0].text or messages[0].caption
                if reply:
                    return reply
                # Media-only reply: return a path marker so callers do not hang forever.
                if messages[0].local_path:
                    return f"[media:{messages[0].local_path}]"
                return messages[0].media_type or ""

        raise TelegramClientError(f"Timeout waiting for response after {timeout} seconds")

    def ask_yes_no(self, question, context=None, timeout=None, default=None):
        yes_words = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "true", "1"}
        no_words = {"no", "n", "nope", "nah", "false", "0"}

        response = self.ask(question + " (yes/no)", context=context, timeout=timeout)
        response_lower = response.lower().strip()

        if response_lower in yes_words:
            return True
        elif response_lower in no_words:
            return False
        elif default is not None:
            return default
        else:
            return self.ask_yes_no(
                f"I didn't understand '{response}'. Please respond yes or no.",
                timeout=timeout,
            )

    def ask_choice(self, question, choices, context=None, timeout=None):
        choice_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(choices))
        full_question = f"{question}\n{choice_text}\n\nReply with number or text:"

        response = self.ask(full_question, context=context, timeout=timeout)
        response = response.strip()

        try:
            idx = int(response) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        except ValueError:
            pass

        response_lower = response.lower()
        for choice in choices:
            if choice.lower() == response_lower or choice.lower().startswith(response_lower):
                return choice

        return response
