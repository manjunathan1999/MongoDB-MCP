"""
tui/widgets.py
--------------
Reusable widgets for the Claude Code-style TUI.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, Markdown, Static


# -- Slash commands available in the TUI ---------------------------------------
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "Show available commands and examples"),
    ("/model", "Select a different Ollama model"),
    ("/clear", "Clear the chat view"),
    ("/reset", "Clear conversation history"),
]


class ChatMessage(Widget):
    """A single chat message — no border, compact, text-only feel."""

    DEFAULT_CSS = """
    ChatMessage {
        height: auto;
        width: 1fr;
        padding: 0 2;
        margin: 0;
    }
    ChatMessage > .role-label {
        height: 1;
        color: #e08000;
        text-style: bold;
    }
    ChatMessage.assistant > .role-label {
        color: #50c050;
    }
    ChatMessage.tool > .role-label {
        color: #50a0d0;
    }
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
    }
    ChatMessage.user > .bubble {
        color: #cccccc;
    }
    ChatMessage.tool > .bubble {
        color: #50a0d0;
    }
    """

    def __init__(self, role: str, content: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._role = role
        self._content = content
        self.add_class(role)

    def compose(self) -> ComposeResult:
        labels = {
            "user": "> You",
            "assistant": "> Assistant",
            "tool": "  Tool",
        }
        yield Label(labels.get(self._role, self._role), classes="role-label")
        if self._role == "assistant":
            yield Markdown(self._content or " ", classes="bubble")
        else:
            yield Static(self._content or " ", classes="bubble")

    def update_content(self, content: str) -> None:
        """Update content in-place for streaming.

        For assistant messages (Markdown widget), updates the markdown.
        For other roles (Static widget), updates plain text.
        """
        self._content = content
        try:
            bubble = self.query_one(".bubble")
            if isinstance(bubble, Markdown):
                bubble.update(content or " ")
            else:
                bubble.update(content or " ")
        except Exception:
            pass


class ChatView(VerticalScroll):
    """Scrollable, borderless chat history — no scrollbar visible."""

    DEFAULT_CSS = """
    ChatView {
        height: 1fr;
        width: 1fr;
        background: $surface;
        border: none;
        padding: 1 0;
        scrollbar-size: 0 0;
    }
    ChatView > ChatMessage {
        height: auto;
        margin-bottom: 1;
    }
    """

    def append_message(self, role: str, content: str) -> "ChatMessage":
        msg = ChatMessage(role=role, content=content)
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg

    def clear_messages(self) -> None:
        for msg in self.query(ChatMessage):
            msg.remove()


class ModelSelector(Widget):
    """Arrow-key navigable model selector."""

    DEFAULT_CSS = """
    ModelSelector {
        height: auto;
        width: 1fr;
        padding: 0 2;
        margin: 0;
    }
    ModelSelector > .selector-title {
        height: 1;
        color: #50c050;
        text-style: bold;
        margin-bottom: 1;
    }
    ModelSelector > .model-item {
        height: 1;
        padding: 0 0 0 2;
        color: #999999;
    }
    ModelSelector > .model-item.--highlighted {
        color: #ffffff;
        background: #333333;
        text-style: bold;
    }
    ModelSelector > .selector-hint {
        height: 1;
        color: #666666;
        margin-top: 1;
        padding: 0 0 0 2;
    }
    """

    can_focus = True

    def __init__(self, models: list[tuple[str, str]], **kwargs) -> None:
        """
        Args:
            models: List of (model_name, size_str) tuples.
        """
        super().__init__(**kwargs)
        self._models = models
        self._selected = 0

    def compose(self) -> ComposeResult:
        yield Label("> Select a model to get started", classes="selector-title")
        for idx, (name, size) in enumerate(self._models):
            size_text = f"  ({size})" if size else ""
            label = Static(
                f"  {name}{size_text}", classes="model-item", id=f"model-{idx}"
            )
            yield label
        yield Static(
            "  Use arrow keys to navigate, Enter to select", classes="selector-hint"
        )

    def on_mount(self) -> None:
        self._highlight(self._selected)
        self.focus()

    def _highlight(self, idx: int) -> None:
        for i in range(len(self._models)):
            item = self.query_one(f"#model-{i}", Static)
            name, size = self._models[i]
            size_text = f"  ({size})" if size else ""
            if i == idx:
                item.add_class("--highlighted")
                item.update(f"> {name}{size_text}")
            else:
                item.remove_class("--highlighted")
                item.update(f"  {name}{size_text}")

    def on_key(self, event) -> None:
        if event.key == "up":
            event.stop()
            self._selected = (self._selected - 1) % len(self._models)
            self._highlight(self._selected)
        elif event.key == "down":
            event.stop()
            self._selected = (self._selected + 1) % len(self._models)
            self._highlight(self._selected)
        elif event.key == "enter":
            event.stop()
            chosen = self._models[self._selected][0]
            self.post_message(self.ModelChosen(chosen))

    class ModelChosen(Message):
        """Posted when the user selects a model."""

        def __init__(self, model_name: str) -> None:
            super().__init__()
            self.model_name = model_name


class SlashMenu(Widget):
    """Popup menu showing available slash commands when user types '/'."""

    DEFAULT_CSS = """
    SlashMenu {
        height: auto;
        max-height: 8;
        width: 1fr;
        background: #2a2a2a;
        border: solid #555555;
        padding: 0 1;
        margin: 0 2;
    }
    SlashMenu > .slash-item {
        height: 1;
        color: #999999;
        padding: 0 1;
    }
    SlashMenu > .slash-item.--highlighted {
        color: #ffffff;
        background: #444444;
        text-style: bold;
    }
    SlashMenu > .slash-item.--hidden {
        display: none;
    }
    """

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._filtered_indices: list[int] = list(range(len(SLASH_COMMANDS)))
        self._selected = 0

    def compose(self) -> ComposeResult:
        for idx, (cmd, desc) in enumerate(SLASH_COMMANDS):
            yield Static(f"  {cmd}  {desc}", classes="slash-item", id=f"slash-{idx}")

    def on_mount(self) -> None:
        self._highlight()

    def update_filter(self, text: str) -> None:
        """Show/hide items based on typed text. No widget removal needed."""
        self._filtered_indices = [
            i for i, (cmd, _) in enumerate(SLASH_COMMANDS) if cmd.startswith(text)
        ]
        if not self._filtered_indices:
            self._filtered_indices = list(range(len(SLASH_COMMANDS)))

        self._selected = 0

        # Show/hide items via CSS class
        for i in range(len(SLASH_COMMANDS)):
            try:
                item = self.query_one(f"#slash-{i}", Static)
                if i in self._filtered_indices:
                    item.remove_class("--hidden")
                else:
                    item.add_class("--hidden")
            except Exception:
                pass

        self._highlight()

    def _highlight(self) -> None:
        for pos, i in enumerate(self._filtered_indices):
            try:
                item = self.query_one(f"#slash-{i}", Static)
                cmd, desc = SLASH_COMMANDS[i]
                if pos == self._selected:
                    item.add_class("--highlighted")
                    item.update(f"> {cmd}  {desc}")
                else:
                    item.remove_class("--highlighted")
                    item.update(f"  {cmd}  {desc}")
            except Exception:
                pass

    def move_up(self) -> None:
        if self._filtered_indices:
            self._selected = (self._selected - 1) % len(self._filtered_indices)
            self._highlight()

    def move_down(self) -> None:
        if self._filtered_indices:
            self._selected = (self._selected + 1) % len(self._filtered_indices)
            self._highlight()

    def get_selected(self) -> str | None:
        """Return the currently highlighted command string."""
        if self._filtered_indices and 0 <= self._selected < len(self._filtered_indices):
            idx = self._filtered_indices[self._selected]
            return SLASH_COMMANDS[idx][0]
        return None
