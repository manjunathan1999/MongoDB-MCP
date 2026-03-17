"""
tests/test_server.py
--------------------
Unit tests for the MongoDB MCP server tools.

Uses pytest-asyncio and mongomock (or a real MongoDB if available).
Tests run against a mocked Motor client so no live MongoDB is required.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_motor_client():
    """Reset the cached Motor client before each test."""
    import mcp_server.server as srv

    original = srv._client
    srv._client = None
    yield
    srv._client = original


def _make_async_cursor(docs: list[dict]):
    """Create an async iterator mock that yields documents.

    Args:
        docs: List of documents to yield.

    Returns:
        AsyncMock that behaves like an async Motor cursor.
    """

    class _FakeCursor:
        def __init__(self, items):
            self._items = items[:]

        def sort(self, *_args, **_kwargs):
            return self

        def skip(self, _n):
            return self

        def limit(self, _n):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    return _FakeCursor(docs)


# ── list_databases ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_databases_success():
    """list_databases should return a JSON array of database objects."""
    from mcp_server.server import list_databases

    mock_cursor = _make_async_cursor([{"name": "admin"}, {"name": "test"}])

    with patch("mcp_server.server._get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.list_databases = AsyncMock(return_value=mock_cursor)
        mock_get_client.return_value = mock_client

        result = await list_databases()
        data = json.loads(result)

    assert isinstance(data, list)
    assert any(d["name"] == "admin" for d in data)


# ── ping_server ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ping_server_success():
    """ping_server should return ok=True and a version string."""
    from mcp_server.server import ping_server

    with patch("mcp_server.server._db") as mock_db:
        mock_db_inst = MagicMock()
        mock_db_inst.command = AsyncMock(
            side_effect=[
                {"ok": 1},
                {"version": "7.0.0", "gitVersion": "abc123"},
            ]
        )
        mock_db.return_value = mock_db_inst

        result = await ping_server()
        data = json.loads(result)

    assert data["ok"] is True
    assert data["version"] == "7.0.0"


# ── insert_one ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_one_success():
    """insert_one should return the inserted_id as a string."""
    from bson import ObjectId

    from mcp_server.server import insert_one

    fake_id = ObjectId()

    with patch("mcp_server.server._db") as mock_db:
        mock_col = MagicMock()
        mock_col.insert_one = AsyncMock(return_value=MagicMock(inserted_id=fake_id))
        mock_db.return_value.__getitem__.return_value = mock_col

        result = await insert_one("users", '{"name": "Alice", "age": 30}')
        data = json.loads(result)

    assert "inserted_id" in data
    assert data["inserted_id"] == str(fake_id)


@pytest.mark.asyncio
async def test_insert_one_invalid_json():
    """insert_one should return an error for malformed JSON."""
    from mcp_server.server import insert_one

    result = await insert_one("users", "not-json")
    data = json.loads(result)
    assert "error" in data


# ── find_documents ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_documents_returns_list():
    """find_documents should return a JSON array of documents."""
    from mcp_server.server import find_documents

    docs = [{"_id": "1", "name": "Alice"}, {"_id": "2", "name": "Bob"}]

    with patch("mcp_server.server._db") as mock_db:
        mock_col = MagicMock()
        mock_col.find.return_value = _make_async_cursor(docs)
        mock_db.return_value.__getitem__.return_value = mock_col

        result = await find_documents("users", filter='{"name": "Alice"}')
        data = json.loads(result)

    assert isinstance(data, list)


# ── count_documents ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_documents():
    """count_documents should return the correct count."""
    from mcp_server.server import count_documents

    with patch("mcp_server.server._db") as mock_db:
        mock_col = MagicMock()
        mock_col.count_documents = AsyncMock(return_value=42)
        mock_db.return_value.__getitem__.return_value = mock_col

        result = await count_documents("users")
        data = json.loads(result)

    assert data["count"] == 42


# ── delete_one ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_one():
    """delete_one should return deleted_count."""
    from mcp_server.server import delete_one

    with patch("mcp_server.server._db") as mock_db:
        mock_col = MagicMock()
        mock_col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mock_db.return_value.__getitem__.return_value = mock_col

        result = await delete_one("users", '{"name": "Alice"}')
        data = json.loads(result)

    assert data["deleted_count"] == 1


# ── update_one ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_one():
    """update_one should return matched_count and modified_count."""
    from mcp_server.server import update_one

    with patch("mcp_server.server._db") as mock_db:
        mock_col = MagicMock()
        mock_col.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1, modified_count=1, upserted_id=None)
        )
        mock_db.return_value.__getitem__.return_value = mock_col

        result = await update_one(
            "users",
            '{"name": "Alice"}',
            '{"$set": {"age": 31}}',
        )
        data = json.loads(result)

    assert data["matched_count"] == 1
    assert data["modified_count"] == 1


# ── aggregate ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregate():
    """aggregate should return a JSON array of pipeline results."""
    from mcp_server.server import aggregate

    agg_results = [{"_id": "London", "count": 5}]

    with patch("mcp_server.server._db") as mock_db:
        mock_col = MagicMock()
        mock_col.aggregate.return_value = _make_async_cursor(agg_results)
        mock_db.return_value.__getitem__.return_value = mock_col

        result = await aggregate(
            "users",
            '[{"$group": {"_id": "$city", "count": {"$sum": 1}}}]',
        )
        data = json.loads(result)

    assert isinstance(data, list)
    assert data[0]["_id"] == "London"


# ── list_collections ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_collections():
    """list_collections should return a JSON array of collection names."""
    from mcp_server.server import list_collections

    with patch("mcp_server.server._db") as mock_db:
        mock_db_inst = MagicMock()
        mock_db_inst.list_collection_names = AsyncMock(return_value=["users", "orders"])
        mock_db.return_value = mock_db_inst

        result = await list_collections("test")
        data = json.loads(result)

    assert "users" in data
    assert "orders" in data


# ── drop_collection ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drop_collection():
    """drop_collection should confirm the dropped collection name."""
    from mcp_server.server import drop_collection

    with patch("mcp_server.server._db") as mock_db:
        mock_db_inst = MagicMock()
        mock_db_inst.drop_collection = AsyncMock(return_value=None)
        mock_db.return_value = mock_db_inst

        result = await drop_collection("temp_data", "test")
        data = json.loads(result)

    assert data["dropped"] == "temp_data"
