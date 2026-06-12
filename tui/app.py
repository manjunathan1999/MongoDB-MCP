"""
tui/app.py
----------
Claude Code-style TUI for the MongoDB MCP Controller.

Layout:
  +-- mode bar (top, 1 line) -----------------------------------+
  |  MongoDB MCP Controller  .  model  .  db                    |
  +-- chat area (fills remaining height) -----------------------+
  |  scrollable messages                                        |
  +-- input bar (bottom, 3 lines) ------------------------------+
  |  >  [type here and press Enter]                             |
  +-- footer hint (bottom, 1 line) -----------------------------+
  |  ? for shortcuts  .  /help  .  Ctrl+Q to quit               |
  +-------------------------------------------------------------+
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
from tui.widgets import ChatView, ModelSelector, SlashMenu

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

    /* -- top mode bar -- */
    #mode-bar {
        dock: top;
        height: 1;
        background: #2d2d2d;
        color: #e08000;
        padding: 0 2;
    }

    /* -- bottom composer panel -- */
    #bottom-panel {
        dock: bottom;
        height: auto;
        max-height: 14;
        background: #1e1e1e;
    }

    #slash-menu {
        display: none;
        height: auto;
        max-height: 6;
        background: #2a2a2a;
        border: solid #555555;
        padding: 0 1;
        margin: 0 2 0 2;
    }

    #slash-menu.--visible {
        display: block;
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

    /* -- chat fills the rest -- */
    ChatView {
        background: #1e1e1e;
        border: none;
        padding: 0 2;
        scrollbar-size: 0 0;
    }

    /* -- messages -- */
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
    ChatMessage.tool > .bubble    { color: #50a0d0; }

    /* -- model selector -- */
    ModelSelector {
        height: auto;
        width: 1fr;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
        Binding("escape", "cancel_req", "Cancel", show=False),
    ]

    _busy: reactive[bool] = reactive(False)

    def __init__(self, model: str | None = None, show_tool_args: bool = False) -> None:
        super().__init__()
        self._model = model  # None means "ask at startup"
        self._show_tool_args = show_tool_args

        self._mcp_context = None
        self._mcp_client = None
        self._mongo_history: list[dict] = []
        self._model_choices: list[str] = []
        self._mcp_ready = False
        self._cancel_event = asyncio.Event()
        self._startup_done = False
        self._selecting_model_inline = False

    # -- compose ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(
            " MongoDB MCP Controller   model: (selecting...)   "
            f"db: {settings.mongodb_uri}",
            id="mode-bar",
        )

        yield ChatView(id="chat")

        with Vertical(id="bottom-panel"):
            yield SlashMenu(id="slash-menu")
            with Horizontal(id="input-row"):
                yield Label(">", id="input-prefix")
                yield Input(
                    placeholder="Ask anything... MongoDB tools are used when connected",
                    id="chat-composer",
                )

            yield Static(
                " ? for shortcuts  .  /help for commands  .  Ctrl+Q to quit",
                id="footer-hint",
            )

    # -- startup ---------------------------------------------------------------

    def on_mount(self) -> None:
        if self._model:
            # Model was provided via --model flag, skip selector
            self._finish_startup(self._model)
        else:
            # Show model picker at startup
            self._show_startup_model_picker()

    def _show_startup_model_picker(self) -> None:
        """Fetch Ollama models and show the arrow-key selector."""
        self._fetch_models_for_startup()

    @work(thread=True)
    def _fetch_models_for_startup(self) -> None:
        """Fetch model list in background thread, then mount selector."""
        try:
            response = ollama.list()
            models = sorted(
                response.models if hasattr(response, "models") else [],
                key=lambda m: str(getattr(m, "model", m)),
            )
        except Exception as exc:
            self.call_from_thread(self._startup_model_fetch_failed, str(exc))
            return

        if not models:
            self.call_from_thread(self._startup_no_models_found)
            return

        model_list: list[tuple[str, str]] = []
        for m in models:
            name = str(getattr(m, "model", m))
            size = getattr(m, "size", None)
            if size:
                gb = size / (1024**3)
                size_str = f"{gb:.1f} GB" if gb >= 1 else f"{size / (1024**2):.0f} MB"
            else:
                size_str = ""
            model_list.append((name, size_str))

        self.call_from_thread(self._mount_model_selector, model_list)

    def _startup_model_fetch_failed(self, error: str) -> None:
        """Ollama unreachable at startup — use the default from settings."""
        chat = self.query_one("#chat", ChatView)
        chat.append_message(
            "assistant",
            f"Could not reach Ollama: {error}\n\n"
            f"Using default model: `{settings.ollama_model}`\n"
            "Make sure Ollama is running with `ollama serve`.",
        )
        self._finish_startup(settings.ollama_model)

    def _startup_no_models_found(self) -> None:
        """No models downloaded — use settings default."""
        chat = self.query_one("#chat", ChatView)
        chat.append_message(
            "assistant",
            "No Ollama models found. Run `ollama pull <model>` first.\n\n"
            f"Using default: `{settings.ollama_model}`",
        )
        self._finish_startup(settings.ollama_model)

    def _mount_model_selector(self, model_list: list[tuple[str, str]]) -> None:
        """Mount the interactive model selector widget."""
        chat = self.query_one("#chat", ChatView)
        selector = ModelSelector(model_list, id="startup-selector")
        chat.mount(selector)
        chat.scroll_end(animate=False)
        selector.focus()

    @on(ModelSelector.ModelChosen)
    def _on_model_chosen(self, event: ModelSelector.ModelChosen) -> None:
        """Handle model selection from the startup selector."""
        event.stop()
        chosen = event.model_name
        # Remove the selector widget
        try:
            selector = self.query_one("#startup-selector", ModelSelector)
            selector.remove()
        except Exception:
            pass

        if self._selecting_model_inline:
            # This is from /model command, not startup
            self._selecting_model_inline = False
            if chosen == self._model:
                self.query_one("#chat", ChatView).append_message(
                    "assistant", f"Already using `{chosen}`."
                )
            else:
                self._model = chosen
                self._refresh_mode_bar()
                self.query_one("#chat", ChatView).append_message(
                    "assistant",
                    f"Switched to `{chosen}`. The next response will use this model.",
                )
            self._focus_composer()
        else:
            # Startup selection
            self._finish_startup(chosen)

    def _finish_startup(self, model: str) -> None:
        """Complete startup after model is selected."""
        self._model = model
        self._startup_done = True
        self._refresh_mode_bar()

        chat = self.query_one("#chat", ChatView)
        chat.append_message(
            "assistant",
            f"**Ready!** Using model `{model}`.\n\n"
            "Type a question in plain English to query your database.\n"
            "Type `/help` to see all available commands.",
        )

        # Connect to MCP in background
        self._connect_mcp()

        # Focus composer
        self.set_timer(0.1, self._focus_composer)

    # -- submit ----------------------------------------------------------------

    async def _submit_text(self, text: str) -> None:
        if self._busy:
            return

        if not self._startup_done:
            return

        if not text:
            return

        cmd = text.lower().strip()
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
                "assistant", "History cleared."
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
        if not text:
            return
        composer.value = ""
        await self._submit_text(text)

    def _focus_composer(self) -> None:
        self.query_one("#chat-composer", Input).focus()

    def _refresh_mode_bar(self) -> None:
        model_display = self._model or "(none)"
        self.query_one("#mode-bar", Static).update(
            f" MongoDB MCP Controller   "
            f"model: {model_display}   "
            f"db: {settings.mongodb_uri}"
        )

    def _format_model_size(self, model) -> str:
        size = getattr(model, "size", None)
        if size is None:
            return ""
        gb = size / (1024**3)
        return f"{gb:.1f} GB" if gb >= 1 else f"{size / (1024**2):.0f} MB"

    async def _cmd_model_picker(self) -> None:
        """Show inline model picker using arrow keys (same as startup)."""
        chat = self.query_one("#chat", ChatView)
        try:
            response = await _in_thread(ollama.list)
            models = sorted(
                response.models if hasattr(response, "models") else [],
                key=lambda m: str(getattr(m, "model", m)),
            )
        except Exception as exc:
            chat.append_message(
                "assistant",
                f"Could not reach Ollama: {exc}\n"
                "Make sure Ollama is running with `ollama serve`.",
            )
            self._focus_composer()
            return

        if not models:
            chat.append_message(
                "assistant",
                "No downloaded Ollama models found. Run `ollama pull <model>` first.",
            )
            self._focus_composer()
            return

        model_list: list[tuple[str, str]] = []
        for m in models:
            name = str(getattr(m, "model", m))
            size = self._format_model_size(m)
            model_list.append((name, size))

        self._selecting_model_inline = True
        selector = ModelSelector(model_list, id="startup-selector")
        chat.mount(selector)
        chat.scroll_end(animate=False)
        selector.focus()

    @on(Input.Submitted, "#chat-composer")
    async def on_submit(self, event: Input.Submitted) -> None:
        event.stop()
        # If slash menu is visible and user presses enter, use the selected command
        slash_menu = self.query_one("#slash-menu", SlashMenu)
        if slash_menu.has_class("--visible"):
            selected = slash_menu.get_selected()
            if selected:
                composer = self.query_one("#chat-composer", Input)
                composer.value = selected
            slash_menu.remove_class("--visible")
            return
        await self._submit_composer()

    @on(Input.Changed, "#chat-composer")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Show/hide slash menu as user types."""
        text = event.value
        slash_menu = self.query_one("#slash-menu", SlashMenu)

        if text.startswith("/") and not text.endswith(" "):
            # Show and filter the slash menu
            slash_menu.add_class("--visible")
            slash_menu.update_filter(text)
        else:
            slash_menu.remove_class("--visible")

    def on_key(self, event) -> None:
        """Intercept up/down/tab keys when slash menu is visible."""
        slash_menu = self.query_one("#slash-menu", SlashMenu)
        if not slash_menu.has_class("--visible"):
            return

        if event.key == "up":
            event.stop()
            event.prevent_default()
            slash_menu.move_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            slash_menu.move_down()
        elif event.key == "tab":
            event.stop()
            event.prevent_default()
            selected = slash_menu.get_selected()
            if selected:
                composer = self.query_one("#chat-composer", Input)
                composer.value = selected
                composer.cursor_position = len(selected)
            slash_menu.remove_class("--visible")
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            slash_menu.remove_class("--visible")

    # -- actions ---------------------------------------------------------------

    def action_clear_chat(self) -> None:
        self.query_one("#chat", ChatView).clear_messages()
        self._focus_composer()

    def action_cancel_req(self) -> None:
        self._cancel_event.set()

    # -- MCP connect -----------------------------------------------------------

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
                "assistant",
                f"Connected to MongoDB — {len(tools)} tools available.",
            )
        except Exception as exc:
            logger.exception("MCP connect failed")
            self.call_from_thread(
                self.query_one("#chat", ChatView).append_message,
                "assistant",
                f"MongoDB connection failed: {exc}\n\n"
                "I'll still answer your questions as a normal AI assistant.\n"
                "Database queries won't work until MongoDB is available.",
            )

    # -- agent query -----------------------------------------------------------

    @work(exclusive=True)
    async def _handle_query(self, user_text: str) -> None:
        from client.ollama_client import SYSTEM_PROMPT, _parse_args

        chat = self.query_one("#chat", ChatView)
        self._busy = True
        chat.append_message("user", user_text)
        self._mongo_history.append({"role": "user", "content": user_text})

        if not self._mcp_ready:
            # MongoDB offline — answer as normal chat with streaming
            # Show thinking indicator
            thinking_bubble = chat.append_message("assistant", "_thinking..._")
            chat.scroll_end(animate=False)

            try:
                # Run the streaming chat in a background thread and collect tokens
                # We use a shared list to pass tokens from the thread to the UI
                tokens_buffer: list[str] = []
                stream_done = asyncio.Event()

                def _stream_chat():
                    """Run in thread: iterate Ollama stream and buffer tokens."""
                    try:
                        for chunk in ollama.chat(
                            model=self._model,
                            messages=[
                                {
                                    "role": "system",
                                    "content": (
                                        "You are a helpful AI assistant. MongoDB MCP tools "
                                        "are currently unavailable, so answer normally. If "
                                        "the user asks for live database data, explain that "
                                        "you cannot inspect MongoDB until the connection is fixed. "
                                        "Keep responses concise."
                                    ),
                                },
                                *self._mongo_history,
                            ],
                            stream=True,
                        ):
                            token = chunk.message.content or ""
                            if token:
                                tokens_buffer.append(token)
                    except Exception as e:
                        tokens_buffer.append(f"\n\nError: {e}")
                    finally:
                        stream_done.set()

                # Start the streaming thread
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, _stream_chat)

                # Poll for new tokens and update UI
                full_text = ""
                first_token = True
                consumed = 0

                while not stream_done.is_set() or consumed < len(tokens_buffer):
                    if self._cancel_event.is_set():
                        break
                    # Consume any new tokens
                    while consumed < len(tokens_buffer):
                        token = tokens_buffer[consumed]
                        consumed += 1
                        if first_token:
                            full_text = token
                            first_token = False
                        else:
                            full_text += token
                        thinking_bubble.update_content(full_text)
                        chat.scroll_end(animate=False)
                    await asyncio.sleep(0.02)

                # Final flush
                while consumed < len(tokens_buffer):
                    token = tokens_buffer[consumed]
                    consumed += 1
                    full_text += token
                thinking_bubble.update_content(full_text)

                if first_token:
                    full_text = (
                        "No response received. The model may be loading — try again."
                    )
                    thinking_bubble.update_content(full_text)

                self._mongo_history.append({"role": "assistant", "content": full_text})

            except Exception as exc:
                logger.exception("Offline chat error")
                error_msg = f"Error communicating with Ollama: {exc}"
                thinking_bubble.update_content(error_msg)

            self._busy = False
            self._focus_composer()
            return

        # Get tool list
        try:
            raw_tools = await self._mcp_client.list_tools()
        except Exception as exc:
            chat.append_message(
                "assistant",
                f"Tool list error: {exc}\n\n"
                "MongoDB tools are offline for this turn.",
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

                # Show thinking indicator while model reasons
                thinking_bubble = chat.append_message("assistant", "_thinking..._")
                chat.scroll_end(animate=False)

                response = await asyncio.wait_for(
                    _in_thread(
                        ollama.chat,
                        model=self._model,
                        messages=messages,
                        tools=ollama_tools,
                        stream=False,
                    ),
                    timeout=60.0,
                )
                msg = response.message

                if not msg.tool_calls:
                    # Remove thinking bubble, stream the final answer
                    thinking_bubble.remove()

                    # Stream the response token by token using thread + polling
                    stream_bubble = chat.append_message("assistant", "")
                    chat.scroll_end(animate=False)

                    tokens_buf: list[str] = []
                    done_evt = asyncio.Event()
                    _msgs = list(messages)  # snapshot

                    def _stream_final():
                        try:
                            for chunk in ollama.chat(
                                model=self._model,
                                messages=_msgs,
                                stream=True,
                            ):
                                t = chunk.message.content or ""
                                if t:
                                    tokens_buf.append(t)
                        except Exception:
                            pass
                        finally:
                            done_evt.set()

                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(None, _stream_final)

                    full_text = ""
                    consumed = 0
                    while not done_evt.is_set() or consumed < len(tokens_buf):
                        if self._cancel_event.is_set():
                            break
                        while consumed < len(tokens_buf):
                            full_text += tokens_buf[consumed]
                            consumed += 1
                            stream_bubble.update_content(full_text)
                            chat.scroll_end(animate=False)
                        await asyncio.sleep(0.02)

                    # Final flush
                    while consumed < len(tokens_buf):
                        full_text += tokens_buf[consumed]
                        consumed += 1
                    stream_bubble.update_content(full_text)

                    if not full_text:
                        full_text = msg.content or ""
                        stream_bubble.update_content(full_text)

                    self._mongo_history.append(
                        {"role": "assistant", "content": full_text}
                    )
                    break

                # Tool calls — replace thinking with tool info
                thinking_bubble.remove()

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
                    chat.append_message("tool", f"`{name}`{preview}")

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
                        "tool", f"  done in {time.perf_counter() - t0:.2f}s"
                    )
                    messages.append({"role": "tool", "content": tool_result})

        except asyncio.TimeoutError:
            # Remove thinking bubble if it's still there
            try:
                thinking_bubble.remove()
            except Exception:
                pass
            chat.append_message(
                "assistant",
                "The model timed out. Try a simpler query or switch to a faster model with `/model`.",
            )
        except Exception as exc:
            logger.exception("Agent error")
            try:
                thinking_bubble.remove()
            except Exception:
                pass
            chat.append_message("assistant", f"Error: {exc}")

        self._busy = False
        self._focus_composer()

    # -- cleanup ---------------------------------------------------------------

    async def on_unmount(self) -> None:
        if self._mcp_context is not None:
            try:
                await self._mcp_context.__aexit__(None, None, None)
            except Exception:
                pass


# -- help ----------------------------------------------------------------------
_HELP_TEXT = """\
**Commands**

| Command  | Description                    |
|----------|--------------------------------|
| `/clear` | Clear the screen               |
| `/reset` | Clear conversation history     |
| `/model` | Select downloaded Ollama model |
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
