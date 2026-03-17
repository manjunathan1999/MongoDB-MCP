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
- [Session Commands](#-session-commands)
- [MongoDB Chat — /mongo](#-mongodb-chat----mongo)
- [MCP Tools Reference](#-mcp-tools-reference-20-tools)
- [Model Recommendations](#-model-recommendations)
- [Running Tests](#-running-tests)
- [Dependencies](#-dependencies)
- [External MCP Clients](#-connecting-external-mcp-clients)

---

## ✨ Features

### MongoDB Control (MCP Layer)

- Natural-language MongoDB queries powered by Ollama + FastMCP
- 20 MCP tools covering every MongoDB operation
- Full CRUD, aggregation pipelines, index management, bulk writes
- Streaming responses, persistent command history, tab-completion

### Documentation Assistant (Main Session)

- Claude Code-style TUI — answers questions strictly from your loaded documentation
- Load a folder of `.txt`, `.md`, `.pdf`, `.rst` files as the knowledge base
- Never answers outside the docs — safe, bounded, grounded
- Routes to `/mongo` when the question is relevant

### Why a Hybrid Architecture?

Pure MCP lacks state management, orchestration, and error recovery. This project separates concerns clearly:

| Layer                       | Technology           | Role                               |
| --------------------------- | -------------------- | ---------------------------------- |
| **Documentation assistant** | Ollama (doc-bounded) | Answers questions from your docs   |
| **Intent + routing**        | Ollama LLM           | Parses natural language            |
| **Data access**             | FastMCP + Motor      | MongoDB reads/writes via MCP tools |

---

## 🏗️ Architecture

```
python main.py
      │
      │  (doc-bounded TUI — answers from loaded docs)
      │  /mongo ──────────────────────────────────────────────────────────┐
      │  /clear, /help, /exit                                             │
      │                                                                   ▼
      │                                                            MongoDBMCPClient
      │                                                            (Ollama: tool-use)
      │                                                                   │
      │                                                                   ▼
      │                                                             FastMCP Server
      │                                                             (20 MCP tools)
      │                                                                   │
      │                                                                   ▼
      │                                                                MongoDB
      └───────────────────────────────────────────────────────────────────┘
```

---

## 📁 Project Structure

```
mongodb-mcp-controller/
│
├── main.py                          # Single entry point — doc-bounded TUI + slash commands
├── pyproject.toml                   # Project metadata + dependency specs
├── requirements.txt                 # pip-installable runtime dependencies
├── requirements-dev.txt             # Dev/test dependencies (pytest, ruff)
├── .env.example                     # Config template — copy to .env and fill in values
├── .main_history                    # Auto-created: persistent session command history
├── README.md
│
├── config/                          # Application configuration
│   ├── __init__.py
│   └── settings.py                  # Pydantic v2 settings loaded from .env
│
├── mcp_server/                      # FastMCP MongoDB server
│   ├── __init__.py
│   └── server.py                    # 20 MCP tools — complete MongoDB control
│
├── client/                          # MongoDB chat client
│   ├── __init__.py
│   └── ollama_client.py             # Ollama agentic REPL
│                                    # streaming output, /clear, /tools, /history
│
└── tests/                           # Test suite
    ├── __init__.py
    └── test_server.py               # 11 unit tests — all 20 MCP server tools
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
| `OLLAMA_MODEL`       | `qwen2.5:7b`                | Model for tool-use + intent parsing |
| `MCP_TRANSPORT`      | `stdio`                     | MCP transport (`stdio` or `sse`)    |
| `LOG_LEVEL`          | `INFO`                      | Python logging level                |
| `SHOW_TOOL_ARGS`     | `false`                     | Print MCP tool arguments inline     |

---

## 💻 Usage

```bash
# Start with README.md as default documentation
python main.py

# Load your own documentation folder
python main.py --docs ./my_docs/

# Load a single PDF manual
python main.py --docs product_manual.pdf

# Override model
python main.py --model qwen2.5:3b

# Show MCP tool arguments inline (mongo mode)
python main.py --show-tool-args

# MCP server only (for Claude Desktop, Cursor, etc.)
python main.py --server-only
```

On startup the application loads your documentation, then shows the session prompt:

```
  🍃  MongoDB MCP Controller

  Model   : qwen2.5:7b
  MongoDB : mongodb://localhost:27017
  DB      : mydb
  Docs    : 3 files loaded (schema.md, api_guide.pdf, overview.txt)

  Ask me anything about the project, or type /help to see commands.

──────── Commands ────────
  /mongo      Enter MongoDB natural-language chat
  /clear      Clear the terminal screen and scroll-back buffer
  /help       Show this help message
  /exit       Quit the application

›
```

Any text you type (without a `/`) goes to the **documentation assistant** — strictly bounded to the docs you loaded. It will not answer outside the documentation.

```
› How does the MCP server work?
── Assistant ──────────────────────────────
According to README.md, the fastMCP server exposes
20 different tools for MongoDB operations...

› What is the capital of France?
── Assistant ──────────────────────────────
I can only answer from the provided documentation,
and I couldn't find information about that there.
```

---

## 🔑 Session Commands

| Command  | Description                                |
| -------- | ------------------------------------------ |
| `/mongo` | Enter MongoDB natural-language chat        |
| `/clear` | Clear terminal screen + scroll-back buffer |
| `/help`  | Show the command table                     |
| `/exit`  | Quit the application                       |

The `/mongo` command returns to the main `›` prompt when exited — no need to restart.

`/clear` also works **inside** the `/mongo` chat. After clearing, a compact header is reprinted so you always know which mode you are in.

---

## 🗄️ MongoDB Chat — `/mongo`

Type `/mongo` at the `›` prompt to enter the MongoDB chat REPL.

```
› /mongo
```

Then query your MongoDB in plain English:

```
You › List all databases
You › Show collections in mydb
You › Count documents in JournalEvents where location is 20
You › Find all events from device 81 today
You › Create a unique index on email field in users
You › Run an aggregation to count events by location
You › Insert a test document into the orders collection
```

### Chat Slash Commands (inside `/mongo`)

| Command    | Description                                      |
| ---------- | ------------------------------------------------ |
| `/clear`   | Clear the terminal screen and scroll-back buffer |
| `/help`    | Show all available commands                      |
| `/tools`   | List all 20 MCP tools with descriptions          |
| `/model`   | Show current model + connection info             |
| `/reset`   | Clear conversation history                       |
| `/history` | Show last 10 queries                             |
| `/exit`    | Exit mongo mode → return to main `›` prompt      |

### Keyboard Shortcuts

| Key       | Action                                             |
| --------- | -------------------------------------------------- |
| `↑` / `↓` | Navigate query history (persists between sessions) |
| `Tab`     | Autocomplete from history                          |
| `Ctrl-C`  | Exit immediately                                   |

---

## 🔌 MCP Tools Reference (20 tools)

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

- MCP server: all 20 tools tested

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
| `prompt-toolkit`    | ≥3.0.0  | History + autocomplete              |
| `python-dotenv`     | ≥1.0.0  | .env config loading                 |
| `pydantic`          | ≥2.0.0  | Data validation                     |
| `pydantic-settings` | ≥2.0.0  | Settings from environment           |
| `pypdf`             | ≥4.0.0  | PDF text extraction for doc loading |

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

## 📄 License

MIT
