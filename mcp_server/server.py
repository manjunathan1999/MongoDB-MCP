"""
mcp_server/server.py
--------------------
FastMCP-based MongoDB MCP server (v3.1.0).

Exposes full CRUD, index management, aggregation, and admin tools
so an LLM (or any MCP client) has complete control over a MongoDB instance.

Transport: stdio (default) — can be swapped to SSE via config.
"""

import sys
from pathlib import Path

# Ensure the project root is on sys.path when this script is spawned as a
# subprocess (e.g. via FastMCP StdioTransport) so that local packages like
# `config.settings` are importable regardless of the working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import json
import logging
from typing import Any

import motor.motor_asyncio
from fastmcp import FastMCP
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

from config.settings import settings

logger = logging.getLogger(__name__)

# ── FastMCP app ──────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="mongodb-controller",
    instructions=(
        "You are a MongoDB controller. "
        "Use the available tools to manage databases, collections, and documents. "
        "Always confirm destructive operations before executing them."
    ),
)

# ── Async Motor client (lazy — created on first use) ────────────────────────
_client: motor.motor_asyncio.AsyncIOMotorClient | None = None


def _get_client() -> motor.motor_asyncio.AsyncIOMotorClient:
    """Return a cached Motor async client, creating it on first call.

    Returns:
        AsyncIOMotorClient connected to the configured MongoDB URI.
    """
    global _client
    if _client is None:
        _client = motor.motor_asyncio.AsyncIOMotorClient(settings.mongodb_uri)
        logger.info("Motor client created → %s", settings.mongodb_uri)
    return _client


def _db(db_name: str | None = None) -> motor.motor_asyncio.AsyncIOMotorDatabase:
    """Resolve a Motor database object.

    Args:
        db_name: Database name. Falls back to ``settings.mongodb_default_db``.

    Returns:
        AsyncIOMotorDatabase instance.
    """
    return _get_client()[db_name or settings.mongodb_default_db]


def _serialize(doc: Any) -> Any:
    """Recursively convert non-JSON-serialisable MongoDB types.

    Converts ObjectId → str, datetime → ISO string, bytes → hex string.

    Args:
        doc: Any value returned from Motor / PyMongo.

    Returns:
        JSON-serialisable equivalent.
    """
    if isinstance(doc, dict):
        return {k: _serialize(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_serialize(i) for i in doc]
    # ObjectId, datetime, Decimal128, etc. → str
    type_name = type(doc).__name__
    if type_name in {"ObjectId", "Decimal128", "datetime", "Timestamp"}:
        return str(doc)
    if isinstance(doc, bytes):
        return doc.hex()
    return doc


# ════════════════════════════════════════════════════════════════════════════
#  DATABASE-LEVEL TOOLS
# ════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    description=(
        "List all databases on the MongoDB server. "
        "Returns a JSON array of database info objects (name, sizeOnDisk, empty)."
    )
)
async def list_databases() -> str:
    """List every database on the server.

    Returns:
        JSON string — list of database descriptors.
    """
    try:
        result = await _get_client().list_databases()
        dbs = [_serialize(db) async for db in result]
        return json.dumps(dbs, indent=2)
    except PyMongoError as exc:
        logger.exception("list_databases failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Drop an entire database and all its collections. "
        "DESTRUCTIVE — cannot be undone. "
        "Requires: db_name."
    )
)
async def drop_database(db_name: str) -> str:
    """Drop a database by name.

    Args:
        db_name: Name of the database to drop.

    Returns:
        JSON confirmation or error.
    """
    try:
        await _get_client().drop_database(db_name)
        logger.warning("Dropped database: %s", db_name)
        return json.dumps({"dropped": db_name})
    except PyMongoError as exc:
        logger.exception("drop_database failed")
        return json.dumps({"error": str(exc)})


# ════════════════════════════════════════════════════════════════════════════
#  COLLECTION-LEVEL TOOLS
# ════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    description=(
        "List all collections inside a database. "
        "Requires: db_name."
    )
)
async def list_collections(db_name: str | None = None) -> str:
    """Return collection names for a given database.

    Args:
        db_name: Target database. Defaults to ``settings.mongodb_default_db``.

    Returns:
        JSON array of collection names.
    """
    try:
        names = await _db(db_name).list_collection_names()
        return json.dumps(names)
    except PyMongoError as exc:
        logger.exception("list_collections failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Create a new collection (optionally with validator/options). "
        "Requires: collection_name. Optional: db_name, options (JSON object)."
    )
)
async def create_collection(
    collection_name: str,
    db_name: str | None = None,
    options: str = "{}",
) -> str:
    """Create a collection, optionally with schema validator or capped options.

    Args:
        collection_name: Name for the new collection.
        db_name: Target database. Defaults to configured default.
        options: JSON string of creation options (e.g. ``{"capped": true, "size": 1000}``).

    Returns:
        JSON confirmation or error.
    """
    try:
        opts: dict = json.loads(options)
        await _db(db_name).create_collection(collection_name, **opts)
        return json.dumps({"created": collection_name, "db": db_name or settings.mongodb_default_db})
    except PyMongoError as exc:
        logger.exception("create_collection failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Drop a collection and all its documents. "
        "DESTRUCTIVE — cannot be undone. "
        "Requires: collection_name. Optional: db_name."
    )
)
async def drop_collection(
    collection_name: str,
    db_name: str | None = None,
) -> str:
    """Drop a collection.

    Args:
        collection_name: Collection to drop.
        db_name: Target database. Defaults to configured default.

    Returns:
        JSON confirmation or error.
    """
    try:
        await _db(db_name).drop_collection(collection_name)
        logger.warning("Dropped collection %s.%s", db_name, collection_name)
        return json.dumps({"dropped": collection_name})
    except PyMongoError as exc:
        logger.exception("drop_collection failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Rename a collection within the same database. "
        "Requires: old_name, new_name. Optional: db_name."
    )
)
async def rename_collection(
    old_name: str,
    new_name: str,
    db_name: str | None = None,
) -> str:
    """Rename a collection.

    Args:
        old_name: Current collection name.
        new_name: New collection name.
        db_name: Target database.

    Returns:
        JSON confirmation or error.
    """
    try:
        await _db(db_name)[old_name].rename(new_name)
        return json.dumps({"renamed": {"from": old_name, "to": new_name}})
    except PyMongoError as exc:
        logger.exception("rename_collection failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Return statistics for a collection (document count, storage size, indexes). "
        "Requires: collection_name. Optional: db_name."
    )
)
async def collection_stats(
    collection_name: str,
    db_name: str | None = None,
) -> str:
    """Retrieve collection stats via the collStats command.

    Args:
        collection_name: Target collection.
        db_name: Target database.

    Returns:
        JSON stats object or error.
    """
    try:
        stats = await _db(db_name).command("collStats", collection_name)
        return json.dumps(_serialize(stats), indent=2)
    except PyMongoError as exc:
        logger.exception("collection_stats failed")
        return json.dumps({"error": str(exc)})


# ════════════════════════════════════════════════════════════════════════════
#  DOCUMENT CRUD TOOLS
# ════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    description=(
        "Insert one document into a collection. "
        "Requires: collection_name, document (JSON object). Optional: db_name."
    )
)
async def insert_one(
    collection_name: str,
    document: str,
    db_name: str | None = None,
) -> str:
    """Insert a single document.

    Args:
        collection_name: Target collection.
        document: JSON string representing the document to insert.
        db_name: Target database.

    Returns:
        JSON with the inserted_id or error.
    """
    try:
        doc: dict = json.loads(document)
        result = await _db(db_name)[collection_name].insert_one(doc)
        return json.dumps({"inserted_id": str(result.inserted_id)})
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("insert_one failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Insert multiple documents into a collection at once. "
        "Requires: collection_name, documents (JSON array). Optional: db_name."
    )
)
async def insert_many(
    collection_name: str,
    documents: str,
    db_name: str | None = None,
) -> str:
    """Insert multiple documents.

    Args:
        collection_name: Target collection.
        documents: JSON array string of documents to insert.
        db_name: Target database.

    Returns:
        JSON with list of inserted_ids or error.
    """
    try:
        docs: list[dict] = json.loads(documents)
        result = await _db(db_name)[collection_name].insert_many(docs)
        ids = [str(i) for i in result.inserted_ids]
        return json.dumps({"inserted_ids": ids, "count": len(ids)})
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("insert_many failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Find documents matching a filter. Supports projection, sort, skip, limit. "
        "Requires: collection_name. Optional: db_name, filter (JSON), "
        "projection (JSON), sort (JSON), skip (int), limit (int, default 20)."
    )
)
async def find_documents(
    collection_name: str,
    db_name: str | None = None,
    filter: str = "{}",
    projection: str | None = None,
    sort: str | None = None,
    skip: int = 0,
    limit: int = 20,
) -> str:
    """Query documents from a collection.

    Args:
        collection_name: Target collection.
        db_name: Target database.
        filter: JSON filter dict (default ``{}`` returns all).
        projection: JSON projection dict to include/exclude fields.
        sort: JSON list of ``[field, direction]`` pairs, e.g. ``[["age", 1], ["name", -1]]``.
        skip: Number of documents to skip (pagination).
        limit: Maximum documents to return (default 20).

    Returns:
        JSON array of matching documents.
    """
    try:
        flt: dict = json.loads(filter)
        proj: dict | None = json.loads(projection) if projection else None
        cursor = _db(db_name)[collection_name].find(flt, proj)

        if sort:
            sort_pairs = json.loads(sort)
            cursor = cursor.sort(
                [(k, ASCENDING if v >= 0 else DESCENDING) for k, v in sort_pairs]
            )

        cursor = cursor.skip(skip).limit(limit)
        docs = [_serialize(d) async for d in cursor]
        return json.dumps(docs, indent=2)
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("find_documents failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Find a single document matching a filter. "
        "Requires: collection_name. Optional: db_name, filter (JSON)."
    )
)
async def find_one(
    collection_name: str,
    db_name: str | None = None,
    filter: str = "{}",
) -> str:
    """Find the first document matching a filter.

    Args:
        collection_name: Target collection.
        db_name: Target database.
        filter: JSON filter dict.

    Returns:
        JSON document or ``{"result": null}`` if not found.
    """
    try:
        flt: dict = json.loads(filter)
        doc = await _db(db_name)[collection_name].find_one(flt)
        return json.dumps(_serialize(doc) if doc else {"result": None}, indent=2)
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("find_one failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Count documents matching a filter. "
        "Requires: collection_name. Optional: db_name, filter (JSON)."
    )
)
async def count_documents(
    collection_name: str,
    db_name: str | None = None,
    filter: str = "{}",
) -> str:
    """Count documents matching a filter.

    Args:
        collection_name: Target collection.
        db_name: Target database.
        filter: JSON filter dict.

    Returns:
        JSON with ``count`` field.
    """
    try:
        flt: dict = json.loads(filter)
        n = await _db(db_name)[collection_name].count_documents(flt)
        return json.dumps({"count": n})
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("count_documents failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Update a single document matching a filter. "
        "Requires: collection_name, filter (JSON), update (JSON $-operator). "
        "Optional: db_name, upsert (bool)."
    )
)
async def update_one(
    collection_name: str,
    filter: str,
    update: str,
    db_name: str | None = None,
    upsert: bool = False,
) -> str:
    """Update the first document matching a filter.

    Args:
        collection_name: Target collection.
        filter: JSON filter to select the document.
        update: JSON update operators, e.g. ``{"$set": {"field": "value"}}``.
        db_name: Target database.
        upsert: If ``True``, insert if no match found.

    Returns:
        JSON with matched_count and modified_count.
    """
    try:
        result = await _db(db_name)[collection_name].update_one(
            json.loads(filter), json.loads(update), upsert=upsert
        )
        return json.dumps(
            {
                "matched_count": result.matched_count,
                "modified_count": result.modified_count,
                "upserted_id": str(result.upserted_id) if result.upserted_id else None,
            }
        )
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("update_one failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Update all documents matching a filter. "
        "Requires: collection_name, filter (JSON), update (JSON $-operator). "
        "Optional: db_name, upsert (bool)."
    )
)
async def update_many(
    collection_name: str,
    filter: str,
    update: str,
    db_name: str | None = None,
    upsert: bool = False,
) -> str:
    """Update every document matching a filter.

    Args:
        collection_name: Target collection.
        filter: JSON filter to select documents.
        update: JSON update operators.
        db_name: Target database.
        upsert: If ``True``, insert if no match found.

    Returns:
        JSON with matched_count and modified_count.
    """
    try:
        result = await _db(db_name)[collection_name].update_many(
            json.loads(filter), json.loads(update), upsert=upsert
        )
        return json.dumps(
            {
                "matched_count": result.matched_count,
                "modified_count": result.modified_count,
            }
        )
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("update_many failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Replace an entire document matching a filter. "
        "Requires: collection_name, filter (JSON), replacement (JSON). "
        "Optional: db_name, upsert (bool)."
    )
)
async def replace_one(
    collection_name: str,
    filter: str,
    replacement: str,
    db_name: str | None = None,
    upsert: bool = False,
) -> str:
    """Replace the first document matching a filter with a new document.

    Args:
        collection_name: Target collection.
        filter: JSON filter to select the document.
        replacement: JSON document to replace with (no $-operators).
        db_name: Target database.
        upsert: If ``True``, insert if no match found.

    Returns:
        JSON with matched_count and modified_count.
    """
    try:
        result = await _db(db_name)[collection_name].replace_one(
            json.loads(filter), json.loads(replacement), upsert=upsert
        )
        return json.dumps(
            {
                "matched_count": result.matched_count,
                "modified_count": result.modified_count,
                "upserted_id": str(result.upserted_id) if result.upserted_id else None,
            }
        )
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("replace_one failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Delete a single document matching a filter. "
        "DESTRUCTIVE. "
        "Requires: collection_name, filter (JSON). Optional: db_name."
    )
)
async def delete_one(
    collection_name: str,
    filter: str,
    db_name: str | None = None,
) -> str:
    """Delete the first document matching a filter.

    Args:
        collection_name: Target collection.
        filter: JSON filter to select the document.
        db_name: Target database.

    Returns:
        JSON with deleted_count.
    """
    try:
        result = await _db(db_name)[collection_name].delete_one(json.loads(filter))
        return json.dumps({"deleted_count": result.deleted_count})
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("delete_one failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Delete all documents matching a filter. "
        "DESTRUCTIVE. "
        "Requires: collection_name, filter (JSON). Optional: db_name."
    )
)
async def delete_many(
    collection_name: str,
    filter: str,
    db_name: str | None = None,
) -> str:
    """Delete every document matching a filter.

    Args:
        collection_name: Target collection.
        filter: JSON filter to select documents.
        db_name: Target database.

    Returns:
        JSON with deleted_count.
    """
    try:
        result = await _db(db_name)[collection_name].delete_many(json.loads(filter))
        logger.warning(
            "delete_many: removed %d docs from %s", result.deleted_count, collection_name
        )
        return json.dumps({"deleted_count": result.deleted_count})
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("delete_many failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Atomically find and update a document, returning the original or updated version. "
        "Requires: collection_name, filter (JSON), update (JSON). "
        "Optional: db_name, return_updated (bool, default False)."
    )
)
async def find_one_and_update(
    collection_name: str,
    filter: str,
    update: str,
    db_name: str | None = None,
    return_updated: bool = False,
) -> str:
    """Atomic find-and-update operation.

    Args:
        collection_name: Target collection.
        filter: JSON filter to locate the document.
        update: JSON update operators.
        db_name: Target database.
        return_updated: Return the modified document instead of the original.

    Returns:
        JSON document (before or after update) or ``{"result": null}``.
    """
    from pymongo import ReturnDocument

    try:
        doc = await _db(db_name)[collection_name].find_one_and_update(
            json.loads(filter),
            json.loads(update),
            return_document=ReturnDocument.AFTER if return_updated else ReturnDocument.BEFORE,
        )
        return json.dumps(_serialize(doc) if doc else {"result": None}, indent=2)
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("find_one_and_update failed")
        return json.dumps({"error": str(exc)})


# ════════════════════════════════════════════════════════════════════════════
#  AGGREGATION TOOLS
# ════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    description=(
        "Run a MongoDB aggregation pipeline and return results. "
        "Requires: collection_name, pipeline (JSON array of stage objects). "
        "Optional: db_name."
    )
)
async def aggregate(
    collection_name: str,
    pipeline: str,
    db_name: str | None = None,
) -> str:
    """Execute an aggregation pipeline.

    Args:
        collection_name: Source collection.
        pipeline: JSON array of aggregation stage documents,
            e.g. ``[{"$match": {"age": {"$gt": 18}}}, {"$group": {"_id": "$city", "count": {"$sum": 1}}}]``.
        db_name: Target database.

    Returns:
        JSON array of result documents.
    """
    try:
        stages: list = json.loads(pipeline)
        cursor = _db(db_name)[collection_name].aggregate(stages)
        results = [_serialize(doc) async for doc in cursor]
        return json.dumps(results, indent=2)
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("aggregate failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Return distinct values for a field, optionally filtered. "
        "Requires: collection_name, field. Optional: db_name, filter (JSON)."
    )
)
async def distinct(
    collection_name: str,
    field: str,
    db_name: str | None = None,
    filter: str = "{}",
) -> str:
    """Get all distinct values of a field within a collection.

    Args:
        collection_name: Target collection.
        field: Field name to get distinct values for.
        db_name: Target database.
        filter: Optional JSON filter to narrow the document set.

    Returns:
        JSON array of distinct values.
    """
    try:
        values = await _db(db_name)[collection_name].distinct(field, json.loads(filter))
        return json.dumps(_serialize(values), indent=2)
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("distinct failed")
        return json.dumps({"error": str(exc)})


# ════════════════════════════════════════════════════════════════════════════
#  INDEX TOOLS
# ════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    description=(
        "List all indexes on a collection. "
        "Requires: collection_name. Optional: db_name."
    )
)
async def list_indexes(
    collection_name: str,
    db_name: str | None = None,
) -> str:
    """Return all index definitions for a collection.

    Args:
        collection_name: Target collection.
        db_name: Target database.

    Returns:
        JSON array of index info objects.
    """
    try:
        indexes = []
        async for idx in _db(db_name)[collection_name].list_indexes():
            indexes.append(_serialize(dict(idx)))
        return json.dumps(indexes, indent=2)
    except PyMongoError as exc:
        logger.exception("list_indexes failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Create an index on a collection. "
        "Requires: collection_name, keys (JSON array of [field, direction] pairs). "
        "Optional: db_name, unique (bool), sparse (bool), name (str)."
    )
)
async def create_index(
    collection_name: str,
    keys: str,
    db_name: str | None = None,
    unique: bool = False,
    sparse: bool = False,
    name: str | None = None,
) -> str:
    """Create an index on a collection.

    Args:
        collection_name: Target collection.
        keys: JSON array of ``[field, direction]`` pairs,
            e.g. ``[["email", 1]]`` or ``[["last_name", 1], ["first_name", 1]]``.
        db_name: Target database.
        unique: Enforce uniqueness constraint.
        sparse: Only index documents that contain the field.
        name: Optional custom index name.

    Returns:
        JSON with the created index name.
    """
    try:
        key_list = json.loads(keys)
        idx_keys = [(k, ASCENDING if v >= 0 else DESCENDING) for k, v in key_list]
        opts: dict[str, Any] = {"unique": unique, "sparse": sparse}
        if name:
            opts["name"] = name
        idx_name = await _db(db_name)[collection_name].create_index(idx_keys, **opts)
        return json.dumps({"index_created": idx_name})
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("create_index failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Drop an index from a collection by its name. "
        "Requires: collection_name, index_name. Optional: db_name."
    )
)
async def drop_index(
    collection_name: str,
    index_name: str,
    db_name: str | None = None,
) -> str:
    """Drop a named index from a collection.

    Args:
        collection_name: Target collection.
        index_name: Name of the index to drop.
        db_name: Target database.

    Returns:
        JSON confirmation or error.
    """
    try:
        await _db(db_name)[collection_name].drop_index(index_name)
        return json.dumps({"dropped_index": index_name})
    except PyMongoError as exc:
        logger.exception("drop_index failed")
        return json.dumps({"error": str(exc)})


# ════════════════════════════════════════════════════════════════════════════
#  ADMIN / SERVER TOOLS
# ════════════════════════════════════════════════════════════════════════════


@mcp.tool(
    description=(
        "Run an arbitrary MongoDB admin command against a database. "
        "Requires: command (JSON object). Optional: db_name."
    )
)
async def run_command(
    command: str,
    db_name: str | None = None,
) -> str:
    """Execute a raw MongoDB command.

    Args:
        command: JSON object representing the command,
            e.g. ``{"ping": 1}`` or ``{"serverStatus": 1}``.
        db_name: Database to run the command against.

    Returns:
        JSON response from MongoDB.
    """
    try:
        cmd: dict = json.loads(command)
        result = await _db(db_name).command(cmd)
        return json.dumps(_serialize(result), indent=2)
    except (PyMongoError, json.JSONDecodeError) as exc:
        logger.exception("run_command failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description="Ping the MongoDB server and return server info including version."
)
async def ping_server() -> str:
    """Check MongoDB connectivity and return basic server info.

    Returns:
        JSON with ok status and server build info.
    """
    try:
        ping = await _db("admin").command("ping")
        info = await _db("admin").command("buildInfo")
        return json.dumps(
            {
                "ok": ping.get("ok") == 1,
                "version": info.get("version"),
                "gitVersion": info.get("gitVersion"),
            }
        )
    except PyMongoError as exc:
        logger.exception("ping_server failed")
        return json.dumps({"error": str(exc)})


@mcp.tool(
    description=(
        "Bulk write operations (insert, update, delete) in a single round-trip. "
        "Requires: collection_name, operations (JSON array of operation objects). "
        "Optional: db_name, ordered (bool, default True)."
        "Operation format: {\"type\": \"insert\"|\"update_one\"|\"update_many\"|\"delete_one\"|\"delete_many\", ...}"
    )
)
async def bulk_write(
    collection_name: str,
    operations: str,
    db_name: str | None = None,
    ordered: bool = True,
) -> str:
    """Execute a bulk write operation.

    Args:
        collection_name: Target collection.
        operations: JSON array of operation dicts. Each dict must have a ``type`` key
            and the relevant ``filter``/``update``/``document`` keys.
        db_name: Target database.
        ordered: If ``True``, stop on first error. If ``False``, continue.

    Returns:
        JSON summary of the bulk write result.
    """
    from pymongo import (
        DeleteMany,
        DeleteOne,
        InsertOne,
        UpdateMany,
        UpdateOne,
    )

    try:
        ops_raw: list[dict] = json.loads(operations)
        requests = []
        for op in ops_raw:
            match op["type"]:
                case "insert":
                    requests.append(InsertOne(op["document"]))
                case "update_one":
                    requests.append(
                        UpdateOne(op["filter"], op["update"], upsert=op.get("upsert", False))
                    )
                case "update_many":
                    requests.append(
                        UpdateMany(op["filter"], op["update"], upsert=op.get("upsert", False))
                    )
                case "delete_one":
                    requests.append(DeleteOne(op["filter"]))
                case "delete_many":
                    requests.append(DeleteMany(op["filter"]))
                case _:
                    return json.dumps({"error": f"Unknown operation type: {op['type']}"})

        result = await _db(db_name)[collection_name].bulk_write(requests, ordered=ordered)
        return json.dumps(
            {
                "inserted_count": result.inserted_count,
                "matched_count": result.matched_count,
                "modified_count": result.modified_count,
                "deleted_count": result.deleted_count,
                "upserted_count": result.upserted_count,
            }
        )
    except (PyMongoError, json.JSONDecodeError, KeyError) as exc:
        logger.exception("bulk_write failed")
        return json.dumps({"error": str(exc)})


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    mcp.run(transport=settings.mcp_transport)
