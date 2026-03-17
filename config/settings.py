"""
config/settings.py
------------------
Centralised settings loaded from environment variables / .env file.
Uses python-dotenv so a .env file in the project root is auto-detected.
"""

import logging
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

# Load .env from project root (two levels up from this file)
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")


class Settings(BaseSettings):
    """Application-wide settings resolved from environment variables.

    All fields can be overridden via a .env file or shell environment.
    Copy .env.example to .env and fill in your values.
    """

    # -- MongoDB ---------------------------------------------------------------
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017",
        description="Full MongoDB connection URI",
    )
    mongodb_default_db: str = Field(
        default="admin",
        description="Default database to operate on when none is specified",
    )

    # -- Ollama ----------------------------------------------------------------
    ollama_host: str = Field(
        default="http://localhost:11434",
        description="Base URL of the Ollama API server",
    )
    ollama_model: str = Field(
        default="qwen2.5:7b",
        description=(
            "Ollama model used for tool-use tasks. "
            "Low-end: qwen2.5:3b / llama3.2:3b  "
            "Mid-range: qwen2.5:7b (default)  "
            "Higher: llama3.1:8b / mistral-nemo:12b"
        ),
    )

    # -- MCP -------------------------------------------------------------------
    mcp_transport: str = Field(
        default="stdio",
        description="MCP transport mode: 'stdio' or 'sse'",
    )

    # -- Terminal UI -----------------------------------------------------------
    show_tool_args: bool = Field(
        default=False,
        description="Print tool call arguments in the terminal alongside tool names",
    )

    # -- Logging ---------------------------------------------------------------
    log_level: str = Field(default="INFO", description="Python logging level")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


# Singleton instance used across the project
settings = Settings()

# ── Configure root logger — file only, keep terminal clean ───────────────────
_LOG_DIR = _ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)

from logging.handlers import RotatingFileHandler as _RFH  # noqa: E402

_file_handler = _RFH(
    _LOG_DIR / "app.log",
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=3,               # keep app.log, app.log.1, app.log.2, app.log.3
    encoding="utf-8",
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
)

# Root logger → file only (no StreamHandler → nothing printed to terminal)
_root_logger = logging.getLogger()
_root_logger.setLevel(settings.log_level.upper())
_root_logger.addHandler(_file_handler)
