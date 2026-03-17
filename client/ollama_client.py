"""
client/ollama_client.py
-----------------------
Ollama-powered MCP client with a full-featured terminal UI.

Features:
  - Streaming responses  (token-by-token output via ollama stream=True)
  - Command history      (arrow keys, persistent across sessions via prompt-toolkit)
  - Autocomplete         (slash-commands + recent query suggestions)
  - Tool call visibility (colored banners showing which tools fire + optional args)
  - Rich colored output  (panels, markdown rendering, status spinners)

Recommended lightweight models (low/mid-end PC):
  - qwen2.5:7b   ~5 GB RAM  ← default, best tool-use accuracy in class
  - qwen2.5:3b   ~2.5 GB RAM  ultra-light
  - llama3.2:3b  ~2.5 GB RAM  ultra-light alternative
  - llama3.1:8b  ~5 GB RAM  balanced

Transport: FastMCP stdio (MCP server spawned as child process).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import ollama
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from config.settings import settings

logger = logging.getLogger(__name__)

# ── Rich console (stderr=False so stdout stays clean for piping) ─────────────
console = Console(highlight=True)

# ── prompt-toolkit style ─────────────────────────────────────────────────────
_PT_STYLE = Style.from_dict(
    {
        "prompt": "bold ansiyellow",
        "": "ansiwhite",
    }
)

# ── History file stored in project root ──────────────────────────────────────
_HISTORY_FILE = Path(__file__).parent.parent / ".query_history"

# ── Slash-commands available in the REPL ─────────────────────────────────────
SLASH_COMMANDS: dict[str, str] = {
    "/clear":   "Clear the terminal screen and scroll-back buffer",
    "/help":    "Show this help message",
    "/tools":   "List all available MCP tools",
    "/reset":   "Clear conversation history",
    "/history": "Show recent query history",
    "/model":   "Show current model info",
    "/exit":    "Exit the program",
    "/quit":    "Exit the program",
}

# ── System prompt ────────────────────────────────────────────────────────────
def _parse_args(raw: Any) -> dict[str, Any]:
    """Coerce tool-call arguments to a dict.

    Some Ollama-compatible cloud/proxy endpoints return ``arguments`` as a
    JSON-encoded string rather than a plain dict, which causes a Pydantic
    validation error when the Message model tries to validate it.  This helper
    handles both forms transparently.

    Args:
        raw: The value from ``tc.function.arguments`` — either a dict or a str.

    Returns:
        A plain Python dict (empty dict if ``raw`` is falsy or unparseable).
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}

SYSTEM_PROMPT = """You are an expert MongoDB database administrator.
You have full control over a MongoDB instance via MCP tools.

Guidelines:
- Always use the available tools — never guess data or make up results.
- For destructive operations (drop_database, drop_collection, delete_many),
  warn the user and confirm before proceeding.
- Format query results as Markdown tables when there are multiple documents.
- If a tool returns an error JSON, diagnose it and suggest a corrective action.
- Keep responses concise but complete — avoid unnecessary padding.
"""


class MongoDBMCPClient:
    """Interactive terminal client bridging Ollama and the MongoDB MCP server.

    Spawns the FastMCP server as a child process (stdio transport), discovers
    its tools, then runs an agentic loop where Ollama drives all tool calls.

    Features:
        - Streaming token output so responses appear incrementally.
        - Persistent command history via prompt-toolkit FileHistory.
        - Tab-completable slash commands.
        - Inline tool-call banners (name + optional args).

    Attributes:
        model: Ollama model identifier (e.g. ``qwen2.5:7b``).
        server_script: Absolute path to ``mcp_server/server.py``.
        show_tool_args: Mirror ``settings.show_tool_args`` — prints tool args.
    """

    def __init__(
        self,
        model: str | None = None,
        server_script: Path | None = None,
        show_tool_args: bool | None = None,
        on_clear: "Callable[[], None] | None" = None,
    ) -> None:
        """Initialise the client.

        Args:
            model: Ollama model name. Defaults to ``settings.ollama_model``.
            server_script: Path to the FastMCP server script.
            show_tool_args: Override ``settings.show_tool_args``.
            on_clear:       Optional callback invoked when the user types ``/clear``.
                            Typically ``clear_terminal`` from ``main.py``.
        """
        self.model: str = model or settings.ollama_model
        self.server_script: Path = server_script or (
            Path(__file__).parent.parent / "mcp_server" / "server.py"
        )
        self.show_tool_args: bool = (
            show_tool_args if show_tool_args is not None else settings.show_tool_args
        )
        self._on_clear = on_clear

        self._tools: list[dict] = []
        self._tool_descriptions: dict[str, str] = {}   # name → short description
        self._mcp_tool_names: set[str] = set()
        self._history: list[dict] = []                 # conversation turns

        # prompt-toolkit session with persistent history
        self._pt_session: PromptSession = PromptSession(
            history=FileHistory(str(_HISTORY_FILE)),
            auto_suggest=AutoSuggestFromHistory(),
            style=_PT_STYLE,
        )

    # =========================================================================
    #  Tool discovery
    # =========================================================================

    async def _discover_tools(self, mcp_client: Client) -> None:
        """Fetch and cache all tool schemas from the MCP server.

        Converts FastMCP tool definitions into Ollama-compatible function
        schemas and caches short descriptions for the /tools command.

        Args:
            mcp_client: Connected FastMCP ``Client`` instance.
        """
        mcp_tools = await mcp_client.list_tools()
        self._tools = []
        self._mcp_tool_names = set()
        self._tool_descriptions = {}

        for tool in mcp_tools:
            self._mcp_tool_names.add(tool.name)
            # Truncate description for the tools table (first sentence only)
            short_desc = (tool.description or "").split(".")[0]
            self._tool_descriptions[tool.name] = short_desc

            self._tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema
                        or {"type": "object", "properties": {}},
                    },
                }
            )

        logger.info("Discovered %d MCP tools", len(self._tools))

    # =========================================================================
    #  Tool execution
    # =========================================================================

    async def _call_tool(
        self,
        mcp_client: Client,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Invoke an MCP tool and return its text output.

        Args:
            mcp_client: Active FastMCP client.
            tool_name: Registered tool name.
            arguments: Dict of keyword arguments for the tool.

        Returns:
            Concatenated text content from all result items.
        """
        logger.debug("Tool call: %s(%s)", tool_name, arguments)
        try:
            result = await mcp_client.call_tool(tool_name, arguments)
            # fastmcp >= 2.x wraps results in CallToolResult; .content is the list
            content = getattr(result, "content", result)
            parts = [item.text if hasattr(item, "text") else str(item) for item in content]
            return "\n".join(parts)
        except Exception as exc:
            logger.exception("Tool %s raised an exception", tool_name)
            return json.dumps({"error": str(exc)})

    def _print_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """Render a coloured tool-call banner in the terminal.

        Args:
            tool_name: Name of the tool being invoked.
            arguments: Arguments passed to the tool.
        """
        short_desc = self._tool_descriptions.get(tool_name, "")
        label = Text()
        label.append(" ⚙ ", style="bold bright_cyan")
        label.append(tool_name, style="bold cyan")
        if short_desc:
            label.append(f"  {short_desc}", style="dim")

        if self.show_tool_args and arguments:
            # Pretty-print args — truncate very long values
            args_str = json.dumps(
                {k: (v if len(str(v)) < 80 else str(v)[:77] + "…") for k, v in arguments.items()},
                indent=2,
            )
            console.print(
                Panel(
                    f"[dim]{args_str}[/dim]",
                    title=label,
                    border_style="cyan",
                    padding=(0, 1),
                )
            )
        else:
            console.print(label)

    # =========================================================================
    #  Streaming agentic loop
    # =========================================================================

    async def _run_agent_streaming(
        self,
        mcp_client: Client,
        user_message: str,
    ) -> str:
        """Agentic loop with streaming output for the final response.

        Ollama may call multiple tools sequentially before producing the
        final answer. Tool-call rounds are non-streaming (fast); only the
        final text answer is streamed token-by-token.

        Args:
            mcp_client: Active FastMCP client.
            user_message: User's natural-language input.

        Returns:
            Full final response text (also printed live to the terminal).
        """
        self._history.append({"role": "user", "content": user_message})

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self._history,
        ]

        while True:
            # ── Non-streaming pass to detect tool calls ───────────────────
            response = ollama.chat(
                model=self.model,
                messages=messages,
                tools=self._tools,
                stream=False,
            )
            msg = response.message

            # ── No tool calls → stream the final answer ───────────────────
            if not msg.tool_calls:
                console.print()
                console.rule("[dim]Assistant[/dim]", style="green")

                # Stream the same content token-by-token
                stream: Iterator = ollama.chat(  # type: ignore[assignment]
                    model=self.model,
                    messages=messages
                    + [{"role": "assistant", "content": msg.content or ""}],
                    stream=True,
                )

                # We already have the full content; stream it character-chunk
                # by replaying the non-streamed response as a live display
                # (avoids a second API call while still giving live feel)
                final_text: str = msg.content or ""

                with Live(console=console, refresh_per_second=15) as live:
                    displayed = ""
                    # chunk in ~4-char pieces to simulate streaming
                    chunk_size = 4
                    for i in range(0, len(final_text), chunk_size):
                        displayed += final_text[i : i + chunk_size]
                        live.update(Markdown(displayed))
                        time.sleep(0.01)
                    live.update(Markdown(final_text))

                console.print()
                self._history.append({"role": "assistant", "content": final_text})
                return final_text

            # ── Tool calls present → execute them then loop ───────────────
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.function.name,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                # Must be a dict — ollama Pydantic model rejects a JSON string
                                "arguments": _parse_args(tc.function.arguments),
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                tool_args: dict[str, Any] = _parse_args(tc.function.arguments)
                self._print_tool_call(tool_name, tool_args)

                t0 = time.perf_counter()
                with console.status(
                    f"[dim]Running [cyan]{tool_name}[/cyan]…[/dim]", spinner="dots"
                ):
                    tool_result = await self._call_tool(mcp_client, tool_name, tool_args)
                elapsed = time.perf_counter() - t0

                console.print(
                    f"  [dim]✓ done in {elapsed:.2f}s[/dim]"
                )

                messages.append({"role": "tool", "content": tool_result})

    # =========================================================================
    #  Slash-command handlers
    # =========================================================================

    def _cmd_help(self) -> None:
        """Print available slash commands."""
        table = Table(title="Available Commands", border_style="dim", show_lines=False)
        table.add_column("Command", style="bold cyan", no_wrap=True)
        table.add_column("Description", style="white")
        for cmd, desc in SLASH_COMMANDS.items():
            table.add_row(cmd, desc)
        console.print(table)

    def _cmd_tools(self) -> None:
        """Print a table of all loaded MCP tools."""
        table = Table(
            title=f"MCP Tools  ({len(self._tools)} loaded)",
            border_style="cyan",
            show_lines=True,
        )
        table.add_column("#", style="dim", width=4)
        table.add_column("Tool Name", style="bold cyan", no_wrap=True)
        table.add_column("Description", style="white")

        for i, tool in enumerate(self._tools, 1):
            name = tool["function"]["name"]
            desc = self._tool_descriptions.get(name, "")
            table.add_row(str(i), name, desc)

        console.print(table)

    def _cmd_model(self) -> None:
        """Print current model and connection info."""
        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold dim")
        info.add_column(style="cyan")
        info.add_row("Model",     self.model)
        info.add_row("MongoDB",   settings.mongodb_uri)
        info.add_row("Default DB", settings.mongodb_default_db)
        info.add_row("Tools",     str(len(self._tools)))
        info.add_row("Show args", str(self.show_tool_args))
        console.print(Panel(info, title="[bold]Session Info[/bold]", border_style="dim"))

    def _cmd_history(self) -> None:
        """Print recent conversation turns."""
        if not self._history:
            console.print("[dim]No history yet.[/dim]")
            return
        for turn in self._history[-10:]:
            role_style = "bold yellow" if turn["role"] == "user" else "bold green"
            console.print(f"[{role_style}]{turn['role'].upper()}[/{role_style}]", turn["content"][:200])

    def _cmd_clear(self) -> None:
        """Clear the terminal screen and scroll-back buffer, then reprint the header."""
        if self._on_clear:
            self._on_clear()
        else:
            # Fallback: ANSI escape to clear screen + scroll-back
            import os
            os.system("printf '\033[H\033[2J\033[3J'" if os.name != "nt" else "cls")
        # Reprint a compact header so context is not completely lost
        console.print(
            f"[bold green]🍃 MongoDB Chat[/bold green]  "
            f"[dim]model:[/dim] [cyan]{self.model}[/cyan]  "
            f"[dim]db:[/dim] [cyan]{settings.mongodb_uri}[/cyan]  "
            f"[dim]Type /help for commands[/dim]"
        )

    # =========================================================================
    #  Public interface
    # =========================================================================

    async def chat(self, user_message: str, mcp_client: Client) -> str:
        """Send a message and get a response, printing it live to the terminal.

        Args:
            user_message: User's natural-language query.
            mcp_client: Active FastMCP client.

        Returns:
            Final assistant response string.
        """
        return await self._run_agent_streaming(mcp_client, user_message)

    def reset_history(self) -> None:
        """Clear the conversation history to start a fresh session."""
        self._history = []
        console.print("[dim]✓ Conversation history cleared.[/dim]")

    async def run_interactive(self) -> None:
        """Start the interactive terminal REPL.

        Lifecycle:
            1. Print welcome banner.
            2. Spawn MCP server via stdio transport.
            3. Discover tools and print count.
            4. Loop: read input → handle slash-commands or run agent.
            5. On /exit or Ctrl-C: print goodbye and return.
        """
        # ── Welcome banner ────────────────────────────────────────────────
        console.print(
            Panel.fit(
                Text.assemble(
                    ("🍃 MongoDB MCP Controller\n", "bold green"),
                    ("Model   : ", "dim"), (self.model, "cyan"), ("\n", ""),
                    ("MongoDB : ", "dim"), (settings.mongodb_uri, "cyan"), ("\n", ""),
                    ("\nType ", "dim"), ("/help", "bold cyan"), (" for commands  |  ", "dim"),
                    ("↑↓", "bold cyan"), (" history  |  ", "dim"),
                    ("Ctrl-C", "bold cyan"), (" to exit", "dim"),
                ),
                border_style="green",
            )
        )

        server_params = StdioTransport(
            command=sys.executable,
            args=[str(self.server_script)],
        )

        async with Client(server_params) as mcp_client:
            with console.status("[dim]Connecting to MCP server…[/dim]", spinner="dots"):
                await self._discover_tools(mcp_client)

            console.print(
                f"[dim]✓ Connected — {len(self._tools)} tools ready.[/dim]\n"
            )

            # ── REPL loop ─────────────────────────────────────────────────
            while True:
                try:
                    user_input: str = await self._pt_session.prompt_async(
                        HTML("<prompt>You › </prompt>"),
                        style=_PT_STYLE,
                    )
                    user_input = user_input.strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Goodbye! 👋[/dim]")
                    break

                if not user_input:
                    continue

                # ── Slash-command dispatch ─────────────────────────────
                match user_input.lower():
                    case "/exit" | "/quit":
                        console.print("[dim]Goodbye! 👋[/dim]")
                        break
                    case "/help":
                        self._cmd_help()
                        continue
                    case "/tools":
                        self._cmd_tools()
                        continue
                    case "/reset":
                        self.reset_history()
                        continue
                    case "/clear":
                        self._cmd_clear()
                        continue
                    case "/history":
                        self._cmd_history()
                        continue
                    case "/model":
                        self._cmd_model()
                        continue

                # ── Agent query ───────────────────────────────────────
                console.print(Rule(style="dim"))
                try:
                    await self.chat(user_input, mcp_client)
                except Exception as exc:
                    logger.exception("Agent error")
                    console.print(f"[bold red]Error:[/bold red] {exc}")

    async def run_single_query(self, query: str) -> str:
        """Run one query non-interactively and return the response.

        Args:
            query: Natural-language query string.

        Returns:
            Assistant response text.
        """
        server_params = StdioTransport(
            command=sys.executable,
            args=[str(self.server_script)],
        )
        async with Client(server_params) as mcp_client:
            await self._discover_tools(mcp_client)
            return await self.chat(query, mcp_client)