"""
main.py
-------
Single entry point for the MongoDB MCP Controller.

Behaves like Claude Code — a conversational TUI that answers questions
strictly from user-provided documentation. Routes to /mongo or /forecast
when the user asks for those capabilities.

Usage:
    python main.py                        # start with README as default docs
    python main.py --docs ./my_docs/      # load a folder of documents
    python main.py --docs product.pdf     # load a single file

Session slash commands:
    /mongo      — enter MongoDB natural-language chat
    /clear      — clear the terminal screen + scroll-back buffer
    /help       — show all commands
    /exit       — quit

Optional flags:
    --model NAME           Override Ollama model (default: from .env)
    --show-tool-args       Show MCP tool call arguments inline (mongo mode)
    --server-only          Start as MCP server only (stdio)

Model recommendations by RAM:
    4-6 GB  : qwen2.5:3b  | llama3.2:3b
    8-16 GB : qwen2.5:7b  (default — best tool-use accuracy)
    16+ GB  : llama3.1:8b | mistral-nemo:12b
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import ollama
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from config.settings import settings

logger = logging.getLogger(__name__)
console = Console()

# ── Slash commands ────────────────────────────────────────────────────────────
_COMMANDS: dict[str, str] = {
    "/mongo":    "Enter MongoDB natural-language chat",
    "/clear":    "Clear the terminal screen and scroll-back buffer",
    "/help":     "Show this help message",
    "/exit":     "Quit the application",
}

# ── prompt-toolkit style ──────────────────────────────────────────────────────
_PT_STYLE = Style.from_dict({
    "prompt": "bold ansiyellow",
    "":       "ansiwhite",
})

_HISTORY_FILE = ".main_history"

# ── Supported doc extensions ──────────────────────────────────────────────────
_DOC_EXTENSIONS = {".txt", ".md", ".pdf", ".rst"}

# ── System prompt for the doc-bounded assistant ───────────────────────────────
_DOC_SYSTEM_TEMPLATE = """You are a helpful documentation assistant.

The user has loaded documentation about their product and/or the MongoDB MCP Controller project.
Your job is to answer questions STRICTLY from that documentation — nothing more.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR ABSOLUTE RULES — NEVER BREAK THESE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RULE 1 — DOCS ONLY:
  Answer ONLY from the documentation provided below.
  Do NOT use any outside knowledge, training data, or assumptions.
  Do NOT make up information that is not explicitly in the docs.

RULE 2 — NO CODE GENERATION:
  Do NOT write, generate, or suggest any code unless
  it appears verbatim in the documentation.

RULE 3 — OUT OF SCOPE RESPONSE:
  If the answer cannot be found in the documentation, respond with:
  "I can only answer from the provided documentation, and I couldn't
   find information about that there."
  Then optionally add one of:
  - "If you want to query MongoDB directly, type /mongo."
  Only suggest /mongo or /forecast if the question is genuinely related.

RULE 4 — SMART ROUTING:
  If the user asks about querying data, collections, databases,
  or MongoDB operations → suggest /mongo after your answer.
  Do NOT suggest both at the same time unless both are truly relevant.

RULE 5 — CLARIFY WHEN NEEDED:
  If the user's question is vague, ask ONE short clarifying question
  to understand what they need before answering.

RULE 6 — CITE YOUR SOURCE:
  When answering, briefly mention which document or section the
  information comes from (e.g. "According to the API guide...").

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LOADED DOCUMENTATION:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{docs_content}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REMINDER: You exist only to help the user understand and use the
documentation above. Stay within its boundaries at all times."""


# ════════════════════════════════════════════════════════════════════════════
#  DOCUMENT LOADER
# ════════════════════════════════════════════════════════════════════════════

class DocumentLoader:
    """Loads documentation from a file or folder into a single text blob.

    Supported formats: .txt, .md, .rst (plain text), .pdf (via pypdf).

    Args:
        source: Path to a file or folder containing documentation.
    """

    def __init__(self, source: Path) -> None:
        self.source = source
        self._content: str = ""
        self._files_loaded: list[str] = []

    def load(self) -> str:
        """Load all documents from the source path.

        Scans recursively if source is a folder. Prints per-file progress
        to the terminal. Supported formats: .txt .md .rst .pdf

        Returns:
            Combined text content of all loaded documents.

        Raises:
            FileNotFoundError: If the source path does not exist.
        """
        if not self.source.exists():
            raise FileNotFoundError(f"Documentation path not found: {self.source}")

        if self.source.is_file():
            console.print(f"[dim]Loading documentation from [cyan]{self.source.name}[/cyan]...[/dim]")
            self._load_file(self.source)
        else:
            files = sorted(
                p for p in self.source.rglob("*")
                if p.is_file() and p.suffix.lower() in _DOC_EXTENSIONS
            )
            if not files:
                console.print(
                    f"[yellow]⚠  No supported files found in [cyan]{self.source}[/cyan]. "
                    f"Supported: {', '.join(sorted(_DOC_EXTENSIONS))}[/yellow]"
                )
                return self._content

            console.print(
                f"[dim]Loading [bold]{len(files)}[/bold] documentation "
                f"file{'s' if len(files) != 1 else ''} from "
                f"[cyan]{self.source}[/cyan]...[/dim]"
            )
            for path in files:
                self._load_file(path)

        if not self._content.strip():
            logger.warning("DocumentLoader: no content extracted from %s", self.source)
            console.print(
                f"[yellow]⚠  No content could be extracted from the documentation.[/yellow]"
            )
        else:
            total_chars = len(self._content)
            n_files = len(self._files_loaded)
            plural = "s" if n_files != 1 else ""
            msg = f"[green]✓[/green] Documentation ready — [bold]{n_files}[/bold] file{plural}, [bold]{total_chars:,}[/bold] total characters."
            console.print(msg)

        return self._content

    def _load_file(self, path: Path) -> None:
        """Load a single file and append its content.

        Adds a clearly marked file header before each document so the LLM
        can cite which file an answer comes from.

        Args:
            path: Path to the file to load.
        """
        try:
            ext = path.suffix.lower()
            if ext == ".pdf":
                text = self._load_pdf(path)
            else:
                text = path.read_text(encoding="utf-8", errors="ignore")

            if text.strip():
                # File header — LLM uses this to cite sources
                self._content += f"\n\n{'='*60}\n"
                self._content += f"DOCUMENT: {path.name}\n"
                self._content += f"PATH: {path}\n"
                self._content += f"{'='*60}\n"
                self._content += text.strip()
                self._files_loaded.append(path.name)
                char_count = len(text)
                logger.info("Loaded: %s  (%d chars)", path.name, char_count)
                console.print(
                    f"  [green]✓[/green] [cyan]{path.name}[/cyan]  "
                    f"[dim]{char_count:,} chars[/dim]"
                )
            else:
                console.print(
                    f"  [yellow]⚠[/yellow] [dim]{path.name} — empty, skipped[/dim]"
                )

        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            console.print(
                f"  [red]✗[/red] [dim]{path.name} — {exc}[/dim]"
            )

    @staticmethod
    def _load_pdf(path: Path) -> str:
        """Extract text from a PDF file using pypdf.

        Args:
            path: Path to the PDF file.

        Returns:
            Extracted text string. Empty string if extraction fails.
        """
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            pages = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            return "\n".join(pages)
        except Exception as exc:
            logger.warning("PDF extraction failed for %s: %s", path, exc)
            return ""

    @property
    def files_loaded(self) -> list[str]:
        """List of filenames successfully loaded."""
        return self._files_loaded

    @property
    def content(self) -> str:
        """Combined documentation content."""
        return self._content


# ════════════════════════════════════════════════════════════════════════════
#  DOCUMENTATION ASSISTANT
# ════════════════════════════════════════════════════════════════════════════

class DocAssistant:
    """Ollama-powered assistant strictly bounded to provided documentation.

    Maintains its own conversation history isolated from /mongo and /forecast
    modes. Uses the documentation as the sole source of truth.

    Args:
        model:        Ollama model name to use for completions.
        docs_content: Full text content of all loaded documentation.
    """

    def __init__(self, model: str, docs_content: str) -> None:
        self.model = model
        self._system = _DOC_SYSTEM_TEMPLATE.format(docs_content=docs_content)
        self._history: list[dict[str, str]] = []

    def reset(self) -> None:
        """Clear conversation history."""
        self._history = []

    def _build_messages(self, user_input: str) -> list[dict[str, str]]:
        """Build the full messages list for Ollama including history.

        Args:
            user_input: Current user message.

        Returns:
            List of message dicts ready for ollama.chat().
        """
        return [
            {"role": "system", "content": self._system},
            *self._history,
            {"role": "user", "content": user_input},
        ]

    def stream_response(self, user_input: str) -> str:
        """Send user input to Ollama and stream the response live.

        Streams token-by-token using Rich Live display. Appends both
        the user message and assistant response to conversation history.

        Args:
            user_input: The user's question or message.

        Returns:
            Full assistant response text.
        """
        messages = self._build_messages(user_input)

        console.print()
        console.rule("[dim]Assistant[/dim]", style="green")

        full_response = ""
        stream = ollama.chat(
            model=self.model,
            messages=messages,
            stream=True,
        )

        with Live(console=console, refresh_per_second=15) as live:
            for chunk in stream:
                delta = chunk.message.content or ""
                full_response += delta
                live.update(Markdown(full_response))

        console.print()

        # Append to history so context carries forward
        self._history.append({"role": "user",      "content": user_input})
        self._history.append({"role": "assistant",  "content": full_response})

        return full_response


# ════════════════════════════════════════════════════════════════════════════
#  TERMINAL UTILITIES
# ════════════════════════════════════════════════════════════════════════════

def clear_terminal() -> None:
    """Clear the terminal screen and scroll-back buffer.

    Works on Windows (cls) and Unix/macOS (ANSI escape sequences).
    Falls back to blank-line overflow if system command fails.
    """
    try:
        if os.name == "nt":
            os.system("cls")
        else:
            os.system("printf '\\033[H\\033[2J\\033[3J'")
    except Exception:
        lines = shutil.get_terminal_size(fallback=(80, 24)).lines
        console.print("\n" * lines)


def _print_banner(model: str, docs_info: str) -> None:
    """Print the application welcome banner.

    Args:
        model:     Ollama model name.
        docs_info: Short string describing loaded docs (e.g. "3 files loaded").
    """
    console.print(
        Panel(
            Text.assemble(
                ("  🍃  MongoDB MCP Controller\n\n",  "bold green"),
                ("  Model   : ", "dim"), (model,                       "cyan"), ("\n", ""),
                ("  MongoDB : ", "dim"), (settings.mongodb_uri,        "cyan"), ("\n", ""),
                ("  DB      : ", "dim"), (settings.mongodb_default_db, "cyan"), ("\n", ""),
                ("  Docs    : ", "dim"), (docs_info,                   "cyan"), ("\n\n", ""),
                ("  Ask me anything about the project, or type ", "dim"),
                ("/help", "bold cyan"),
                (" to see commands.", "dim"),
            ),
            border_style="green",
            padding=(0, 2),
        )
    )


def _print_compact_header(model: str) -> None:
    """Print a compact one-line header after /clear.

    Args:
        model: Ollama model name.
    """
    console.print(
        f"[bold green]🍃 MongoDB MCP Controller[/bold green]  "
        f"[dim]model:[/dim] [cyan]{model}[/cyan]  "
        f"[dim]db:[/dim] [cyan]{settings.mongodb_uri}[/cyan]  "
        f"[dim]Type /help for commands[/dim]\n"
    )


def _print_help() -> None:
    """Print the slash-command reference table."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("cmd",  style="bold cyan", width=14)
    table.add_column("desc", style="dim")
    for cmd, desc in _COMMANDS.items():
        table.add_row(cmd, desc)
    console.print(Rule("[dim]Commands[/dim]", style="dim"))
    console.print(table)
    console.print()


# ════════════════════════════════════════════════════════════════════════════
#  FILTER PARSER
# ════════════════════════════════════════════════════════════════════════════

def _parse_filter_string(raw: str) -> dict[str, str]:
    """Parse a comma-separated ``field=value`` filter string into a dict.

    Accepts:
        ``location=20, deviceid=81``
        ``location=20 deviceid=81``

    Args:
        raw: Raw filter input from the user.

    Returns:
        Dict of field → value. Empty dict if input is blank.
    """
    filters: dict[str, str] = {}
    if not raw.strip():
        return filters

    for pair in re.split(r"[,\s]+", raw.strip()):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            console.print(
                f"  [yellow]⚠  Skipping '[bold]{pair}[/bold]' "
                f"— expected format: field=value[/yellow]"
            )
            continue
        key, _, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            filters[key] = value
        else:
            console.print(
                f"  [yellow]⚠  Skipping '[bold]{pair}[/bold]' "
                f"— empty field or value.[/yellow]"
            )
    return filters


# ════════════════════════════════════════════════════════════════════════════
#  SLASH-COMMAND HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def _handle_mongo(model: str | None, show_tool_args: bool) -> None:
    """Launch the MongoDB natural-language chat REPL.

    On /exit inside the chat, control returns to the main session prompt.

    Args:
        model:          Optional Ollama model override.
        show_tool_args: Whether to display MCP tool call arguments inline.
    """
    from client.ollama_client import MongoDBMCPClient

    client = MongoDBMCPClient(
        model=model,
        show_tool_args=show_tool_args,
        on_clear=clear_terminal,
    )
    await client.run_interactive()
    console.print("[dim]Returned to main session. Type /help for commands.[/dim]\n")


def _handle_clear(model: str) -> None:
    """Clear the terminal and reprint the compact header.

    Args:
        model: Current model name to show in the compact header.
    """
    clear_terminal()
    _print_compact_header(model)


# ════════════════════════════════════════════════════════════════════════════
#  MAIN SESSION LOOP
# ════════════════════════════════════════════════════════════════════════════

async def _session(
    model: str,
    show_tool_args: bool,
    docs_content: str,
    docs_info: str,
) -> None:
    """Run the main persistent session loop.

    Behaviour:
        - Regular text input → answered strictly from loaded documentation
          using DocAssistant (streaming, with conversation history).
        - Slash commands → route to /mongo, /clear, /help, /exit.
        - Unknown commands → friendly hint.
        - Tab-completion for all slash commands.
        - Persistent input history across restarts.

    Args:
        model:          Ollama model name.
        show_tool_args: Whether to show MCP tool args in mongo mode.
        docs_content:   Full text of all loaded documentation.
        docs_info:      Short description of docs for the banner.
    """
    _print_banner(model, docs_info)
    _print_help()

    assistant = DocAssistant(model=model, docs_content=docs_content)

    completer = WordCompleter(list(_COMMANDS.keys()), ignore_case=True, sentence=True)
    session: PromptSession = PromptSession(
        history=FileHistory(_HISTORY_FILE),
        auto_suggest=AutoSuggestFromHistory(),
        completer=completer,
        style=_PT_STYLE,
        complete_while_typing=True,
    )

    while True:
        try:
            raw: str = await session.prompt_async(
                HTML("<prompt>› </prompt>"),
                style=_PT_STYLE,
            )
            user_input = raw.strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye! 👋[/dim]")
            break

        if not user_input:
            continue

        # ── Slash-command dispatch ─────────────────────────────────────────
        if user_input.startswith("/"):
            cmd = user_input.lower()

            match cmd:
                case "/mongo":
                    console.print(
                        Rule("[bold cyan]MongoDB Chat[/bold cyan]", style="cyan")
                    )
                    try:
                        await _handle_mongo(model, show_tool_args)
                    except KeyboardInterrupt:
                        console.print("\n[dim]Returned to main session.[/dim]\n")

                case "/clear":
                    _handle_clear(model)

                case "/help":
                    _print_help()

                case "/exit":
                    console.print("[dim]Goodbye! 👋[/dim]")
                    break

                case _:
                    console.print(
                        f"  [yellow]Unknown command [bold]{cmd}[/bold]. "
                        f"Type [bold cyan]/help[/bold cyan] to see available commands.[/yellow]\n"
                    )
            continue

        # ── Regular input → doc-bounded assistant ─────────────────────────
        try:
            assistant.stream_response(user_input)
        except Exception as exc:
            logger.exception("DocAssistant error")
            console.print(f"[bold red]Error:[/bold red] {exc}\n")


# ════════════════════════════════════════════════════════════════════════════
#  CLI PARSER
# ════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments.

    Returns:
        Parsed argparse Namespace.
    """
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="MongoDB MCP Controller + Event Forecasting Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py\n"
            "  python main.py --docs ./docs/\n"
            "  python main.py --docs product.pdf\n"
            "  python main.py --model qwen2.5:3b\n"
            "  python main.py --server-only\n"
        ),
    )
    parser.add_argument(
        "--docs", "-d", default=None, metavar="PATH",
        help="Path to a documentation file or folder (.txt, .md, .pdf, .rst)",
    )
    parser.add_argument(
        "--model", "-m", default=None,
        help=f"Ollama model to use (default: {settings.ollama_model})",
    )
    parser.add_argument(
        "--show-tool-args", action="store_true", default=False,
        help="Print MCP tool call arguments inline (mongo mode only)",
    )
    parser.add_argument(
        "--server-only", action="store_true",
        help="Start FastMCP MCP server in stdio mode (no TUI session)",
    )
    return parser.parse_args()


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Application entry point.

    Loads documentation, initialises the DocAssistant, then runs the
    persistent slash-command session loop.
    """
    args = _parse_args()

    # ── MCP server mode ───────────────────────────────────────────────────
    if args.server_only:
        from mcp_server.server import mcp
        logger.info("Starting MongoDB MCP server  transport=stdio")
        mcp.run(transport="stdio")
        return

    model: str = args.model or settings.ollama_model
    show_args: bool = args.show_tool_args or settings.show_tool_args

    # ── Load documentation ────────────────────────────────────────────────
    docs_content: str = ""
    docs_info:    str = "no docs loaded"

    if args.docs:
        # User specified a docs path
        docs_path = Path(args.docs)
        loader = DocumentLoader(docs_path)
        try:
            docs_content = loader.load()
            n = len(loader.files_loaded)
            docs_info = (
                f"{n} file{'s' if n != 1 else ''} loaded  "
                f"({', '.join(loader.files_loaded[:3])}"
                f"{'...' if n > 3 else ''})"
            )
            console.print(
                f"[green]✓[/green] Loaded [bold]{n}[/bold] documentation "
                f"file{'s' if n != 1 else ''}: "
                f"[dim]{', '.join(loader.files_loaded)}[/dim]"
            )
        except FileNotFoundError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            return
    else:
        # Default: use the project README
        readme = Path(__file__).parent / "README.md"
        if readme.exists():
            loader = DocumentLoader(readme)
            docs_content = loader.load()
            docs_info = "README.md (default)"
            console.print(
                "[dim]No --docs specified. Using README.md as default documentation.[/dim]"
            )
        else:
            docs_info = "no docs loaded"
            docs_content = (
                "No documentation is currently loaded. "
                "The user can start the app with --docs <path> to load documentation."
            )
            console.print(
                "[yellow]⚠  No documentation loaded. "
                "Use --docs <path> to load your documentation.[/yellow]"
            )

    # ── Start session ─────────────────────────────────────────────────────
    asyncio.run(_session(model, show_args, docs_content, docs_info))


if __name__ == "__main__":
    main()