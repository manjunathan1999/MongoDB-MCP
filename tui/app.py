"""
tui/app.py
----------
Claude Code-style TUI for the MongoDB MCP Controller.

Layout:
  ┌─ mode bar (top, 1 line) ────────────────────────────────┐
  │  🍃 MongoDB MCP Controller  ·  model  ·  db             │
  ├─ chat area (fills remaining height) ────────────────────┤
  │  scrollable messages                                    │
  ├─ input bar (bottom, 3 lines) ───────────────────────────┤
  │  ›  [type here and press Enter]                         │
  ├─ footer hint (bottom, 1 line) ──────────────────────────┤
  │  ? for shortcuts  ·  /help  ·  Ctrl+Q to quit           │
  └─────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import ollama
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Input, Label, Static

from config.settings import settings
from tui.widgets import ChatView

logger = logging.getLogger(__name__)


async def _in_thread(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


class MongoTUIApp(App):
    """Claude Code-style MongoDB MCP TUI."""

    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: #1e1e1e;
        layers: base;
    }

    /* ── top mode bar ── */
    #mode-bar {
        dock: top;
        height: 1;
        background: #2d2d2d;
        color: #e08000;
        padding: 0 2;
    }

    /* ── bottom composer panel ── */
    #bottom-panel {
        dock: bottom;
        height: 4;
        background: #1e1e1e;
    }

    #input-row {
        height: 3;
        background: #242424;
        border: solid #777777;
        padding: 0 1;
        align: left middle;
    }
    #input-prefix {
        width: 2;
        color: #e08000;
        text-style: bold;
        content-align: left middle;
    }
    #chat-composer {
        width: 1fr;
        height: 1;
        background: #242424;
        color: #ffffff;
        border: none;
        padding: 0;
    }
    #chat-composer:focus {
        border: none;
        background: #242424;
    }
    #footer-hint {
        height: 1;
        background: #1e1e1e;
        color: #777777;
        padding: 0 2;
    }

    /* ── chat fills the rest ── */
    ChatView {
        background: #1e1e1e;
        border: none;
        padding: 0 2;
    }

    /* ── messages ── */
    ChatMessage {
        height: auto;
        width: 1fr;
        margin: 0 0 1 0;
        padding: 0;
    }
    ChatMessage > .role-label {
        height: 1;
        color: #e08000;
        text-style: bold;
    }
    ChatMessage.assistant > .role-label { color: #50c050; }
    ChatMessage.tool > .role-label      { color: #50a0d0; }
    ChatMessage.system > .role-label    { color: #666666; }
    ChatMessage > .bubble {
        height: auto;
        padding: 0 0 0 2;
        color: #cccccc;
    }
    ChatMessage > Markdown {
        height: auto;
        padding: 0 0 0 2;
        margin: 0;
        background: transparent;
        color: #cccccc;
    }
    ChatMessage.system > .bubble  { color: #666666; }
    ChatMessage.tool > .bubble    { color: #50a0d0; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
        Binding("escape", "cancel_req", "Cancel", show=False),
    ]

    _busy: reactive[bool] = reactive(False)

    def __init__(self, model: str, show_tool_args: bool = False) -> None:
        super().__init__()
        self._model = model
        self._show_tool_args = show_tool_args

        self._mcp_context = None
        self._mcp_client = None
        self._mongo_history: list[dict] = []
        self._model_choices: list[str] = []
        self._mcp_ready = False
        self._cancel_event = asyncio.Event()

    # ── compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(
            f" 🍃  MongoDB MCP Controller   "
            f"model: {self._model}   "
            f"db: {settings.mongodb_uri}",
            id="mode-bar",
        )

        yield ChatView(id="chat")

        with Vertical(id="bottom-panel"):
            with Horizontal(id="input-row"):
                yield Label("›", id="input-prefix")
                yield Input(
                    placeholder="Ask anything... MongoDB tools are used when connected",
                    id="chat-composer",
                )

            yield Static(
                " ? for shortcuts  ·  /help for commands  ·  Ctrl+Q to quit",
                id="footer-hint",
            )

    # ── startup ──────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        chat = self.query_one("#chat", ChatView)
        db = settings.mongodb_uri.replace("mongodb://", "")

        # Claude Code-style welcome splash
        chat.append_message(
            "system",
            f"  🍃  MongoDB MCP Controller\n\n"
            f"     model  {self._model}\n"
            f"     db     {db}",
        )
        chat.append_message(
            "assistant",
            "**Welcome!** Connecting to MongoDB MCP server…\n\n"
            "Type a question in plain English to query your database.\n"
            "Type `/help` to see all available commands.",
        )

        # Connect to MCP immediately
        self._connect_mcp()

        # Focus the composer
        self.set_timer(0.1, self._focus_composer)

    # ── submit ────────────────────────────────────────────────────────────────

    async def _submit_text(self, text: str) -> None:
        if self._busy:
            return

        if self._model_choices:
            self._handle_model_choice(text)
            return

        if not text:
            return

        cmd = text.lower()
        if cmd in ("/help", "/h", "?"):
            self.query_one("#chat", ChatView).append_message("assistant", _HELP_TEXT)
            self._focus_composer()
            return
        if cmd in ("/model", "/models"):
            await self._cmd_model_picker()
            return
        if cmd in ("/clear",):
            self.action_clear_chat()
            return
        if cmd in ("/reset",):
            self._mongo_history = []
            self.query_one("#chat", ChatView).append_message(
                "system", "✓ History cleared."
            )
            self._focus_composer()
            return

        self._cancel_event.clear()
        self._handle_query(text)

    async def _submit_composer(self) -> None:
        composer = self.query_one("#chat-composer", Input)
        text = composer.value.strip()
        if self._busy:
            return
        if not text and not self._model_choices:
            return
        composer.value = ""
        await self._submit_text(text)

    def _focus_composer(self) -> None:
        self.query_one("#chat-composer", Input).focus()

    def _refresh_mode_bar(self) -> None:
        self.query_one("#mode-bar", Static).update(
            f" 🍃  MongoDB MCP Controller   "
            f"model: {self._model}   "
            f"db: {settings.mongodb_uri}"
        )

    def _format_model_size(self, model) -> str:
        size = getattr(model, "size", None)
        if size is None:
            return ""
        gb = size / (1024**3)
        return f"{gb:.1f} GB" if gb >= 1 else f"{size / (1024**2):.0f} MB"

    async def _cmd_model_picker(self) -> None:
        chat = self.query_one("#chat", ChatView)
        try:
            response = await _in_thread(ollama.list)
            models = sorted(
                response.models if hasattr(response, "models") else [],
                key=lambda m: str(getattr(m, "model", m)),
            )
        except Exception as exc:
            chat.append_message(
                "system",
                f"⚠  Could not reach Ollama: {exc}\n"
                "Make sure Ollama is running with `ollama serve`.",
            )
            self._focus_composer()
            return

        if not models:
            chat.append_message(
                "system",
                "No downloaded Ollama models found. Run `ollama pull <model>` first.",
            )
            self._focus_composer()
            return

        self._model_choices = [str(getattr(model, "model", model)) for model in models]
        lines = ["**Select Ollama model**", ""]
        for idx, model in enumerate(models, 1):
            name = self._model_choices[idx - 1]
            size = self._format_model_size(model)
            active = "  _(current)_" if name == self._model else ""
            size_text = f" — {size}" if size else ""
            lines.append(f"{idx}. `{name}`{size_text}{active}")
        lines.append("")
        lines.append("Type a number to switch, or press Enter to cancel.")
        chat.append_message("assistant", "\n".join(lines))
        self._focus_composer()

    def _handle_model_choice(self, text: str) -> None:
        chat = self.query_one("#chat", ChatView)
        choice = text.strip()
        if not choice:
            self._model_choices = []
            chat.append_message("system", f"Keeping `{self._model}`.")
            self._focus_composer()
            return

        if not choice.isdigit():
            chat.append_message(
                "system",
                f"Invalid model selection. Type a number from 1 to {len(self._model_choices)}.",
            )
            self._focus_composer()
            return

        idx = int(choice) - 1
        if not 0 <= idx < len(self._model_choices):
            chat.append_message(
                "system",
                f"Invalid model selection. Type a number from 1 to {len(self._model_choices)}.",
            )
            self._focus_composer()
            return

        chosen = self._model_choices[idx]
        self._model_choices = []
        if chosen == self._model:
            chat.append_message("system", f"Already using `{chosen}`.")
        else:
            self._model = chosen
            self._refresh_mode_bar()
            chat.append_message(
                "system",
                f"✓ Switched to `{chosen}`. The next response will use this model.",
            )
        self._focus_composer()

    @on(Input.Submitted, "#chat-composer")
    async def on_submit(self, event: Input.Submitted) -> None:
        event.stop()
        await self._submit_composer()

    # ── actions ───────────────────────────────────────────────────────────────

    def action_clear_chat(self) -> None:
        self.query_one("#chat", ChatView).clear_messages()
        self._focus_composer()

    def action_cancel_req(self) -> None:
        self._cancel_event.set()

    # ── MCP connect ───────────────────────────────────────────────────────────

    @work(thread=True)
    def _connect_mcp(self) -> None:
        asyncio.run(self._async_connect_mcp())

    async def _async_connect_mcp(self) -> None:
        import sys
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        server_script = (
            Path(__file__).resolve().parent.parent / "mcp_server" / "server.py"
        )
        server_params = StdioTransport(
            command=sys.executable,
            args=[str(server_script)],
        )
        try:
            self._mcp_context = Client(server_params)
            self._mcp_client = await self._mcp_context.__aenter__()
            tools = await self._mcp_client.list_tools()
            self._mcp_ready = True
            self.call_from_thread(
                self.query_one("#chat", ChatView).append_message,
                "system",
                f"✓ Connected — {len(tools)} tools ready.",
            )
        except Exception as exc:
            logger.exception("MCP connect failed")
            self.call_from_thread(
                self.query_one("#chat", ChatView).append_message,
                "system",
                f"⚠  MCP connection failed: {exc}\n"
                f"Is MongoDB running at `{settings.mongodb_uri}`?",
            )

    # ── agent query ───────────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _handle_query(self, user_text: str) -> None:
        from client.ollama_client import SYSTEM_PROMPT, _parse_args

        chat = self.query_one("#chat", ChatView)
        self._busy = True
        chat.append_message("user", user_text)
        self._mongo_history.append({"role": "user", "content": user_text})

        if not self._mcp_ready:
            chat.append_message(
                "system",
                "MongoDB tools are offline, so I will answer without database access.",
            )
            try:
                response = await _in_thread(
                    ollama.chat,
                    model=self._model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a helpful AI assistant. MongoDB MCP tools "
                                "are currently unavailable, so answer normally. If "
                                "the user asks for live database data, explain that "
                                "you cannot inspect MongoDB until the connection is fixed."
                            ),
                        },
                        *self._mongo_history,
                    ],
                    stream=False,
                )
                final = response.message.content or ""
                bubble = chat.append_message("assistant", "▌")
                shown = ""
                for i in range(0, len(final), 4):
                    if self._cancel_event.is_set():
                        break
                    shown += final[i : i + 4]
                    bubble.update_content(shown + "▌")
                    chat.scroll_end(animate=False)
                    await asyncio.sleep(0.008)
                bubble.update_content(final)
                self._mongo_history.append({"role": "assistant", "content": final})
            except Exception as exc:
                logger.exception("Offline chat error")
                chat.append_message("system", f"⚠  AI chat error: {exc}")
            finally:
                self._busy = False
                self._focus_composer()
            return

        # Get tool list
        try:
            raw_tools = await self._mcp_client.list_tools()
        except Exception as exc:
            chat.append_message(
                "system",
                f"⚠  Tool list error: {exc}\n"
                "MongoDB tools are offline for this turn; ask again for normal AI chat.",
            )
            self._busy = False
            self._focus_composer()
            return

        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                },
            }
            for t in raw_tools
        ]
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self._mongo_history,
        ]

        try:
            while True:
                if self._cancel_event.is_set():
                    break

                response = await _in_thread(
                    ollama.chat,
                    model=self._model,
                    messages=messages,
                    tools=ollama_tools,
                    stream=False,
                )
                msg = response.message

                if not msg.tool_calls:
                    # Stream final answer
                    final = msg.content or ""
                    bubble = chat.append_message("assistant", "▌")
                    shown = ""
                    for i in range(0, len(final), 4):
                        if self._cancel_event.is_set():
                            break
                        shown += final[i : i + 4]
                        bubble.update_content(shown + "▌")
                        chat.scroll_end(animate=False)
                        await asyncio.sleep(0.008)
                    bubble.update_content(final)
                    self._mongo_history.append({"role": "assistant", "content": final})
                    break

                # Tool calls
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
                                    "arguments": _parse_args(tc.function.arguments),
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )

                for tc in msg.tool_calls:
                    name = tc.function.name
                    args = _parse_args(tc.function.arguments)

                    preview = ""
                    if self._show_tool_args and args:
                        import json

                        preview = (
                            "\n```json\n" + json.dumps(args, indent=2)[:300] + "\n```"
                        )
                    chat.append_message("tool", f"⚙  `{name}`{preview}")

                    t0 = time.perf_counter()
                    try:
                        result = await self._mcp_client.call_tool(name, args)
                        content = getattr(result, "content", result)
                        tool_result = "\n".join(
                            item.text if hasattr(item, "text") else str(item)
                            for item in content
                        )
                    except Exception as exc:
                        import json as _j

                        tool_result = _j.dumps({"error": str(exc)})

                    chat.append_message(
                        "tool", f"  ✓ done in {time.perf_counter() - t0:.2f}s"
                    )
                    messages.append({"role": "tool", "content": tool_result})

        except Exception as exc:
            logger.exception("Agent error")
            chat.append_message("system", f"⚠  Error: {exc}")

        self._busy = False
        self._focus_composer()

    # ── cleanup ───────────────────────────────────────────────────────────────

    async def on_unmount(self) -> None:
        if self._mcp_context is not None:
            try:
                await self._mcp_context.__aexit__(None, None, None)
            except Exception:
                pass


# ── help ──────────────────────────────────────────────────────────────────────
_HELP_TEXT = """\
**Commands**

| Command  | Description                    |
|----------|--------------------------------|
| `/clear` | Clear the screen               |
| `/reset` | Clear conversation history     |
| `/model` | Select downloaded Ollama model |
| `/models`| Alias for `/model`             |
| `/help`  | Show this message              |

**Keyboard shortcuts**

| Key     | Action                         |
|---------|--------------------------------|
| Ctrl+L  | Clear chat                     |
| Ctrl+Q  | Quit                           |
| Escape  | Cancel in-flight request       |

**Example queries**
- List all databases
- Show collections in mydb
- Count documents in orders
- Find users where active is true
- Insert a test document into logs
- Create a unique index on email in users
"""
