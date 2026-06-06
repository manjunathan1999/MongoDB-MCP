"""
tui/widgets.py
--------------
Reusable widgets for the Claude Code-style TUI.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Label, Markdown, Static


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
        color: $warning;
        text-style: bold;
    }
    ChatMessage.assistant > .role-label {
        color: $success;
    }
    ChatMessage.tool > .role-label {
        color: $accent;
    }
    ChatMessage.system > .role-label {
        color: $text-muted;
        text-style: italic;
    }
    ChatMessage > .bubble {
        height: auto;
        padding: 0 0 0 2;
        color: $text;
    }
    ChatMessage > Markdown {
        height: auto;
        padding: 0 0 0 2;
        margin: 0;
        background: transparent;
    }
    ChatMessage.user > .bubble {
        color: $text;
    }
    ChatMessage.system > .bubble {
        color: $text-muted;
    }
    ChatMessage.tool > .bubble {
        color: $accent;
    }
    """

    def __init__(self, role: str, content: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._role = role
        self._content = content
        self.add_class(role)

    def compose(self) -> ComposeResult:
        labels = {
            "user": "You",
            "assistant": "🍃 Assistant",
            "tool": "⚙ Tool",
            "system": "ℹ",
        }
        yield Label(labels.get(self._role, self._role), classes="role-label")
        if self._role in ("assistant",):
            yield Markdown(self._content or " ", classes="bubble")
        else:
            yield Static(self._content or " ", classes="bubble")

    def update_content(self, content: str) -> None:
        """Update content in-place for streaming."""
        self._content = content
        try:
            bubble = self.query_one(".bubble")
            bubble.update(content or " ")
        except Exception:
            pass


class ChatView(VerticalScroll):
    """Scrollable, borderless chat history."""

    DEFAULT_CSS = """
    ChatView {
        height: 1fr;
        width: 1fr;
        background: $surface;
        border: none;
        padding: 1 0;
        scrollbar-size: 1 1;
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
