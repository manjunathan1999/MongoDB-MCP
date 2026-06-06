"""
main.py
-------
Entry point for the MongoDB MCP Controller TUI.

Usage:
    python main.py                    # launch TUI
    python main.py --model qwen2.5:3b # override model
    python main.py --show-tool-args   # show MCP tool arguments inline
    python main.py --server-only      # MCP server only (no TUI)
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="MongoDB MCP Controller — Claude Code-style TUI",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=None,
        help=f"Ollama model (default: {settings.ollama_model})",
    )
    parser.add_argument(
        "--show-tool-args",
        action="store_true",
        default=False,
        help="Show MCP tool arguments inline in the chat",
    )
    parser.add_argument(
        "--server-only",
        action="store_true",
        help="Start as MCP server only (stdio, for Claude Desktop etc.)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.server_only:
        from mcp_server.server import mcp

        logger.info("Starting MCP server  transport=stdio")
        mcp.run(transport="stdio")
        return

    model: str = args.model or settings.ollama_model
    show_args: bool = args.show_tool_args or settings.show_tool_args

    from tui.app import MongoTUIApp

    MongoTUIApp(model=model, show_tool_args=show_args).run()


if __name__ == "__main__":
    main()
