# 🍃 MongoDB MCP Controller

> **Full-control MongoDB management**
> Built with **FastMCP 3.1.0** and **Ollama**.

A single terminal application that lets you manage MongoDB in plain English using natural-language queries.

---

## 📋 Table of Contents

- [Features](#-features)
- [Architecture](#-architecture)
- [Project Structure](#-project-structure)
- [Prerequisites](#-prerequisites)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [TUI Commands](#-tui-commands)
- [MCP Tools Reference](#-mcp-tools-reference-25-tools)
- [Model Recommendations](#-model-recommendations)
- [Running Tests](#-running-tests)
- [Dependencies](#-dependencies)
- [External MCP Clients](#-connecting-external-mcp-clients)

---

## ✨ Features

### MongoDB Control (MCP Layer)

- Natural-language MongoDB queries powered by Ollama + FastMCP
- 25 MCP tools covering common MongoDB administration and data operations
- Full CRUD, aggregation pipelines, index management, bulk writes
- Claude Code-style full-screen TUI with a persistent bottom composer
- Inline tool-call visibility, optional tool arguments, and audit logging

### AI Fallback

- If MongoDB or the MCP server is unavailable, the TUI still answers as a normal Ollama chat assistant
- Database-specific questions clearly explain that live MongoDB access is unavailable until the connection is fixed
- `/model` lets you switch between downloaded Ollama models without restarting

### Why a Hybrid Architecture?

Pure MCP lacks state management, orchestration, and error recovery. This project separates concerns clearly:

| Layer             | Technology      | Role                                      |
| ----------------- | --------------- | ----------------------------------------- |
| **Terminal TUI**  | Textual         | Chat layout, commands, model picker       |
| **LLM runtime**   | Ollama          | Normal chat + MongoDB tool-use reasoning  |
| **MCP server**    | FastMCP + Motor | MongoDB reads/writes via registered tools |
| **Audit logging** | JSONL logs      | Records MCP tool calls and outcomes       |

---

## 🏗️ Architecture

```
python main.py
      │
      ▼
Textual TUI  ── /model, /help, /clear, /reset
      │
      ├── MongoDB connected ──► Ollama tool-use loop ──► FastMCP Server ──► MongoDB
      │
      └── MongoDB offline ────► Ollama normal chat fallback
```

---

## 📁 Project Structure

```
mongodb-mcp-controller/
│
├── main.py                          # Single entry point — Textual TUI or MCP server-only mode
├── pyproject.toml                   # Project metadata + dependency specs
├── requirements.txt                 # pip-installable runtime dependencies
├── requirements-dev.txt             # Dev/test dependencies (pytest, ruff)
├── .env.example                     # Config template — copy to .env and fill in values
├── README.md
│
├── config/                          # Application configuration
│   ├── audit_logger.py              # JSON audit trail for MCP tool calls
│   └── settings.py                  # Pydantic v2 settings loaded from .env
│
├── mcp_server/                      # FastMCP MongoDB server
│   ├── __init__.py
│   └── server.py                    # 25 MCP tools — complete MongoDB control
│
├── client/
│   └── ollama_client.py             # Legacy prompt-toolkit REPL utilities
│
├── tui/                             # Claude Code-style Textual UI
│   ├── app.py                       # Main chat screen, composer, commands, model picker
│   └── widgets.py                   # Chat message and scroll view widgets
│
├── rag/                             # RAG helpers for documentation indexing experiments
│   └── engine.py
│
├── logs/                            # Auto-created app.log and audit.log
│
└── tests/                           # Test suite
    ├── __init__.py
    └── test_server.py               # Unit tests for MCP server behavior
```

---

## ⚙️ Prerequisites

| Requirement | Version   | Notes                      |
| ----------- | --------- | -------------------------- |
| Python      | **3.12+** | Uses `match/case`, PEP 695 |
| MongoDB     | 6.0+      | Local or remote            |
| Ollama      | latest    | https://ollama.com         |

---

## 🚀 Installation

### 1. Pull an Ollama model

```bash
# Recommended for 8–16 GB RAM (default)
ollama pull qwen2.5:7b

# Low-end (4–6 GB RAM)
ollama pull qwen2.5:3b

# Or use an Ollama cloud model (no local RAM needed)
ollama signin
ollama pull gpt-oss:20b-cloud
```

### 2. Clone and install

```bash
git clone <your-repo-url>
cd mongodb-mcp-controller

# Runtime only
pip install -r requirements.txt

# With dev/test tools
pip install -r requirements.txt -r requirements-dev.txt

# Or with uv (faster)
uv pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env — set MONGODB_URI, MONGODB_DEFAULT_DB, OLLAMA_MODEL
```

---

## 🔧 Configuration

All settings live in `.env` (copy from `.env.example`):

| Variable             | Default                     | Description                         |
| -------------------- | --------------------------- | ----------------------------------- |
| `MONGODB_URI`        | `mongodb://localhost:27017` | MongoDB connection string           |
| `MONGODB_DEFAULT_DB` | `admin`                     | Default database                    |
| `OLLAMA_HOST`        | `http://localhost:11434`    | Ollama API base URL                 |
| `OLLAMA_MODEL`       | `qwen2.5:7b`                | Startup model for chat + tool use   |
| `MCP_TRANSPORT`      | `stdio`                     | MCP transport (`stdio` or `sse`)    |
| `LOG_LEVEL`          | `INFO`                      | Python logging level                |
| `SHOW_TOOL_ARGS`     | `false`                     | Print MCP tool arguments inline     |
| `AUDIT_LOG_ENABLED`  | `true`                      | Write MCP tool calls to audit log   |

---

## 💻 Usage

```bash
# Start the full-screen TUI
python main.py

# Override model
python main.py --model qwen2.5:3b

# Show MCP tool arguments inline (mongo mode)
python main.py --show-tool-args

# MCP server only (for Claude Desktop, Cursor, etc.)
python main.py --server-only
```

On startup the application opens a full-screen terminal UI:

```
🍃 MongoDB MCP Controller   model: qwen2.5:7b   db: mongodb://localhost:27017

Assistant
Welcome! Connecting to MongoDB MCP server...

› Ask anything... MongoDB tools are used when connected
? for shortcuts · /help for commands · Ctrl+Q to quit
```

Ask MongoDB questions in plain English:

```
› List all databases
› Show collections in mydb
› Count documents in JournalEvents where location is 20
› Find all events from device 81 today
› Create a unique index on email field in users
```

If MongoDB is not connected, the same input box still works as a normal AI chat. It will not pretend to inspect live database data until the MCP connection is fixed.

---

## 🔑 TUI Commands

| Command   | Description                                      |
| --------- | ------------------------------------------------ |
| `/help`   | Show available commands and examples             |
| `/clear`  | Clear the chat view                              |
| `/reset`  | Clear conversation history                       |
| `/model`  | List downloaded Ollama models and switch model   |
| `/models` | Alias for `/model`                               |

Use `/model` to select from models already downloaded in Ollama. Type the number shown in the list to switch; the top bar updates immediately and the next response uses the selected model.

### Keyboard Shortcuts

| Key      | Action                   |
| -------- | ------------------------ |
| `Ctrl+L` | Clear chat               |
| `Ctrl+Q` | Quit the TUI             |
| `Escape` | Cancel in-flight request |

---

## 🔌 MCP Tools Reference (25 tools)

### Database Tools

| Tool             | Description                         |
| ---------------- | ----------------------------------- |
| `list_databases` | List all databases on the server    |
| `drop_database`  | Drop a database — destructive       |
| `ping_server`    | Check connectivity + server version |
| `run_command`    | Execute raw MongoDB admin command   |

### Collection Tools

| Tool                | Description                               |
| ------------------- | ----------------------------------------- |
| `list_collections`  | List all collections in a database        |
| `create_collection` | Create a collection with optional options |
| `drop_collection`   | Drop a collection — destructive           |
| `rename_collection` | Rename a collection                       |
| `collection_stats`  | Get storage + index statistics            |

### Document Tools

| Tool                  | Description                             |
| --------------------- | --------------------------------------- |
| `insert_one`          | Insert a single document                |
| `insert_many`         | Insert multiple documents at once       |
| `find_documents`      | Query with filter / sort / skip / limit |
| `find_one`            | Fetch first matching document           |
| `count_documents`     | Count matching documents                |
| `update_one`          | Update first matching document          |
| `update_many`         | Update all matching documents           |
| `replace_one`         | Fully replace a document                |
| `delete_one`          | Delete first matching document          |
| `delete_many`         | Delete all matching documents           |
| `find_one_and_update` | Atomic find + update                    |
| `bulk_write`          | Mixed bulk operations in one round-trip |

### Aggregation & Index Tools

| Tool           | Description                            |
| -------------- | -------------------------------------- |
| `aggregate`    | Run full aggregation pipeline          |
| `distinct`     | Get distinct field values              |
| `list_indexes` | List all indexes on a collection       |
| `create_index` | Create index (unique / sparse options) |
| `drop_index`   | Drop a named index                     |

---

## 🤖 Model Recommendations

| Model             | RAM     | Tool-Use | Speed        | Best For                  |
| ----------------- | ------- | -------- | ------------ | ------------------------- |
| **qwen2.5:3b**    | ~2.5 GB | ★★★★☆    | ⚡ Very fast | 4–6 GB RAM                |
| **llama3.2:3b**   | ~2.5 GB | ★★★★☆    | ⚡ Very fast | 4–6 GB RAM                |
| **qwen2.5:7b** ⭐ | ~5 GB   | ★★★★★    | Fast         | **8–16 GB RAM (default)** |
| llama3.1:8b       | ~5 GB   | ★★★★☆    | Fast         | 8–16 GB RAM               |
| mistral-nemo:12b  | ~8 GB   | ★★★★☆    | Moderate     | 16 GB RAM                 |
| qwen2.5:14b       | ~10 GB  | ★★★★★    | Moderate     | 24+ GB RAM                |

### Ollama Cloud Models (no local RAM required)

```bash
ollama signin
ollama pull gpt-oss:20b-cloud    # strong reasoning, cloud inference
python main.py -m gpt-oss:20b-cloud
```

Available: `gpt-oss:20b-cloud`, `gpt-oss:120b-cloud`,
`qwen3-coder:480b-cloud`, `deepseek-v3.1:671b-cloud`

---

## 🧪 Running Tests

```bash
# Set up test environment
pytest tests/ -v

# With coverage report
pytest tests/ -v --cov=. --cov-report=term-missing

# MCP server tests only
pytest tests/test_server.py -v
```

Tests use mocked Motor clients — no live MongoDB or Ollama required.

**Test coverage:**

- MCP server tool behavior with mocked Motor clients

---

## 📦 Dependencies

### Runtime (`requirements.txt`)

| Package             | Version | Role                                |
| ------------------- | ------- | ----------------------------------- |
| `fastmcp`           | ≥3.1.0  | MCP server framework                |
| `motor`             | ≥3.7.1  | Async MongoDB driver                |
| `pymongo`           | ≥4.16.0 | MongoDB sync operations             |
| `ollama`            | ≥0.6.1  | LLM client (local + cloud)          |
| `rich`              | ≥13.0.0 | Terminal UI + markdown rendering    |
| `textual`           | ≥0.80.0 | Full-screen terminal UI             |
| `prompt-toolkit`    | ≥3.0.0  | Legacy REPL utilities               |
| `python-dotenv`     | ≥1.0.0  | .env config loading                 |
| `pydantic`          | ≥2.0.0  | Data validation                     |
| `pydantic-settings` | ≥2.0.0  | Settings from environment           |
| `numpy`             | ≥1.26.0 | RAG vector utilities                |
| `pypdf`             | ≥4.0.0  | PDF text extraction for RAG helpers |

### Dev / Test (`requirements-dev.txt`)

| Package          | Version | Role               |
| ---------------- | ------- | ------------------ |
| `pytest`         | ≥8.0.0  | Test runner        |
| `pytest-asyncio` | ≥0.23.0 | Async test support |
| `pytest-mock`    | ≥3.12.0 | Mocking helpers    |
| `pytest-cov`     | ≥4.0.0  | Coverage reports   |
| `ruff`           | ≥0.4.0  | Linter + formatter |

---

## 🔌 Connecting External MCP Clients

Use this project as a MongoDB MCP server from Claude Desktop, Cursor, or any MCP-compatible client:

```json
{
  "mcpServers": {
    "mongodb-controller": {
      "command": "python",
      "args": [
        "/absolute/path/to/mongodb-mcp-controller/main.py",
        "--server-only"
      ],
      "env": {
        "MONGODB_URI": "mongodb://localhost:27017",
        "MONGODB_DEFAULT_DB": "mydb"
      }
    }
  }
}
```
---

<div align="center">

### 🚀 This Is Just the Beginning

Keep building, keep learning, and keep pushing boundaries. Every project is another step toward mastering your craft.

⭐ If you found this project useful, consider giving it a star.

</div>

---
---

## 📄 License

MIT
