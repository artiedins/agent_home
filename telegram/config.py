import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # dotenv optional, can use env vars directly


@dataclass
class Config:
    bot_token: str = ""
    chat_id: int = 0
    default_timeout: int = 300
    question_prefix: str = "[Home]"
    max_context_length: int = 500


def load_config():
    """Load configuration from environment variables (via .env or exported)."""
    config = Config()
    config.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "0")
    config.chat_id = int(chat_id) if chat_id else 0
    if os.environ.get("TELEGRAM_TIMEOUT"):
        config.default_timeout = int(os.environ.get("TELEGRAM_TIMEOUT"))
    return config
