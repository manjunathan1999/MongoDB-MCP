"""
rag/engine.py
-------------
Lightweight RAG engine — no native C extensions required.

Pipeline:
    1. DocumentLoader splits text into fixed-size chunks with overlap.
    2. Each chunk is embedded via ollama.embeddings() using a small
       embedding model (default: nomic-embed-text).
    3. Embeddings are stored in a plain numpy matrix (in-process).
    4. At query time, the question is embedded and cosine-similarity is
       used to retrieve the top-k most relevant chunks.
    5. Retrieved chunks are injected into the LLM prompt as context.

No external vector-DB daemon is required — everything lives in RAM and is
rebuilt each time docs are (re)loaded, which is fast enough for typical
documentation sets (< 10 MB).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import ollama

logger = logging.getLogger(__name__)

# ── Supported doc extensions (mirrors main.py) ────────────────────────────────
_DOC_EXTENSIONS = {".txt", ".md", ".pdf", ".rst"}

# ── Default chunking parameters ───────────────────────────────────────────────
_DEFAULT_CHUNK_SIZE = 500  # characters per chunk
_DEFAULT_CHUNK_OVERLAP = 100  # overlap between adjacent chunks
_TOP_K = 5  # chunks retrieved per query

# ── RAG system prompt ─────────────────────────────────────────────────────────
_RAG_SYSTEM = """\
You are a helpful documentation assistant.
Answer questions STRICTLY using the retrieved context passages below.
Do NOT use outside knowledge. If the answer is not in the context, say:
"I couldn't find that in the loaded documentation."
Cite the source file name when possible.

──────────────── RETRIEVED CONTEXT ────────────────
{context}
────────────────────────────────────────────────────
"""

# ── Preferred embedding models in priority order ─────────────────────────────
_EMBED_MODEL_PRIORITY = [
    "nomic-embed-text",
    "mxbai-embed-large",
    "all-minilm",
    "snowflake-arctic-embed",
    "bge-m3",
]


def resolve_embed_model(preferred: str) -> str:
    """Return the best available embedding model.

    Checks locally installed Ollama models. If *preferred* is installed,
    uses it. Otherwise falls back through the priority list, then uses
    whatever chat model is available that can produce embeddings.

    Args:
        preferred: The configured embed model name (from settings).

    Returns:
        Model name string that can be passed to ``ollama.embeddings()``.
    """
    try:
        response = ollama.list()
        installed = {
            str(getattr(m, "model", m)).split(":")[0].lower()
            for m in getattr(response, "models", [])
        }
        installed_full = [
            str(getattr(m, "model", m)) for m in getattr(response, "models", [])
        ]

        # Check preferred first (exact or base match)
        pref_base = preferred.split(":")[0].lower()
        if pref_base in installed:
            # Return the full name with tag
            for name in installed_full:
                if name.split(":")[0].lower() == pref_base:
                    return name

        # Check priority list
        for candidate in _EMBED_MODEL_PRIORITY:
            cand_base = candidate.split(":")[0].lower()
            if cand_base in installed:
                for name in installed_full:
                    if name.split(":")[0].lower() == cand_base:
                        logger.info(
                            "Embed model '%s' not found, using '%s' instead",
                            preferred,
                            name,
                        )
                        return name

        # Fall back to first available model that supports embeddings
        if installed_full:
            fallback = installed_full[0]
            logger.warning(
                "No dedicated embed model found. Using '%s' for embeddings "
                "(quality may be lower). Pull nomic-embed-text for best results.",
                fallback,
            )
            return fallback

    except Exception as exc:
        logger.warning("Could not query Ollama model list: %s", exc)

    # Last resort — return preferred and let Ollama error naturally
    return preferred


# ─────────────────────────────────────────────────────────────────────────────
#  DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Chunk:
    """A single text chunk with its source file name."""

    text: str
    source: str  # filename, e.g. "README.md"
    index: int  # position within the source file's chunks


@dataclass
class RAGIndex:
    """In-memory vector index over all loaded chunks."""

    chunks: list[Chunk] = field(default_factory=list)
    embeddings: np.ndarray | None = None  # shape (N, dim)
    model: str = ""
    sources: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
#  CHUNKING
# ─────────────────────────────────────────────────────────────────────────────


def _split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split *text* into overlapping character-level chunks.

    Tries to break at paragraph boundaries first, then sentence boundaries,
    then falls back to hard character splits.

    Args:
        text:       Full document text.
        chunk_size: Target characters per chunk.
        overlap:    Characters to repeat at the start of the next chunk.

    Returns:
        List of non-empty text chunks.
    """
    # Normalise whitespace a bit
    text = re.sub(r"\n{3,}", "\n\n", text.strip())

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))

        # Try to snap end to a paragraph / sentence / word boundary
        if end < len(text):
            para_break = text.rfind("\n\n", start, end)
            if para_break != -1 and para_break > start + chunk_size // 2:
                end = para_break
            else:
                sent_break = max(
                    text.rfind(". ", start, end),
                    text.rfind("! ", start, end),
                    text.rfind("? ", start, end),
                )
                if sent_break != -1 and sent_break > start + chunk_size // 2:
                    end = sent_break + 1  # include the punctuation

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start = end - overlap if end < len(text) else len(text)

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
#  DOCUMENT LOADING (mirrors main.py DocumentLoader, no Rich printing)
# ─────────────────────────────────────────────────────────────────────────────


def _load_file_text(path: Path) -> str:
    """Read a file to plain text. Supports .txt/.md/.rst and .pdf."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            import pypdf

            reader = pypdf.PdfReader(str(path))
            return "\n".join(p.extract_text() for p in reader.pages if p.extract_text())
        except Exception as exc:
            logger.warning("PDF extraction failed %s: %s", path, exc)
            return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _load_docs(source: Path) -> list[tuple[str, str]]:
    """Return list of (filename, text) pairs from a file or folder.

    Args:
        source: Path to a single file or a directory.

    Returns:
        List of (filename, raw_text) tuples for all supported files found.
    """
    if source.is_file():
        text = _load_file_text(source)
        return [(source.name, text)] if text.strip() else []

    pairs: list[tuple[str, str]] = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix.lower() in _DOC_EXTENSIONS:
            text = _load_file_text(path)
            if text.strip():
                pairs.append((path.name, text))
    return pairs


# ─────────────────────────────────────────────────────────────────────────────
#  EMBEDDING
# ─────────────────────────────────────────────────────────────────────────────


def _embed(texts: list[str], model: str) -> np.ndarray:
    """Embed a list of texts using Ollama and return a numpy matrix.

    Args:
        texts: List of strings to embed.
        model: Ollama embedding model name.

    Returns:
        Float32 numpy array of shape (len(texts), embedding_dim).
    """
    vectors: list[list[float]] = []
    for text in texts:
        resp = ollama.embeddings(model=model, prompt=text)
        vectors.append(resp["embedding"])
    arr = np.array(vectors, dtype=np.float32)
    # L2-normalise so cosine similarity == dot product
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return arr / norms


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────


def build_index(
    source: Path,
    embed_model: str,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
    on_progress: Any = None,  # optional callable(current, total, filename)
) -> RAGIndex:
    """Load documents, chunk them, and embed every chunk.

    Args:
        source:       Path to a file or folder of documentation.
        embed_model:  Ollama embedding model to use.
        chunk_size:   Target chunk size in characters.
        overlap:      Overlap between consecutive chunks.
        on_progress:  Optional progress callback ``fn(current, total, filename)``.

    Returns:
        Populated :class:`RAGIndex` ready for querying.
    """
    doc_pairs = _load_docs(source)
    if not doc_pairs:
        logger.warning("No documents found at %s", source)
        return RAGIndex(model=embed_model)

    all_chunks: list[Chunk] = []
    sources: list[str] = []

    for filename, text in doc_pairs:
        raw_chunks = _split_text(text, chunk_size, overlap)
        for i, c in enumerate(raw_chunks):
            all_chunks.append(Chunk(text=c, source=filename, index=i))
        if filename not in sources:
            sources.append(filename)

    logger.info(
        "RAG: %d chunks from %d files — embedding with %s",
        len(all_chunks),
        len(sources),
        embed_model,
    )

    texts = [c.text for c in all_chunks]
    total = len(texts)

    # Embed in one-shot batches of 32 (Ollama handles them sequentially)
    vectors: list[list[float]] = []
    batch_size = 32
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        for j, text in enumerate(batch):
            resp = ollama.embeddings(model=embed_model, prompt=text)
            vectors.append(resp["embedding"])
            if on_progress:
                on_progress(i + j + 1, total, all_chunks[i + j].source)

    arr = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    arr = arr / norms

    return RAGIndex(
        chunks=all_chunks, embeddings=arr, model=embed_model, sources=sources
    )


def retrieve(index: RAGIndex, query: str, top_k: int = _TOP_K) -> list[Chunk]:
    """Find the top-k most relevant chunks for *query*.

    Args:
        index:  A populated :class:`RAGIndex`.
        query:  User question or search string.
        top_k:  Number of chunks to return.

    Returns:
        List of :class:`Chunk` objects sorted by relevance (most relevant first).
    """
    if index.embeddings is None or len(index.chunks) == 0:
        return []

    q_vec = _embed([query], index.model)[0]  # shape (dim,)
    scores = index.embeddings @ q_vec  # cosine sim, shape (N,)
    top_indices = np.argsort(scores)[::-1][:top_k]
    return [index.chunks[i] for i in top_indices]


def build_context(chunks: list[Chunk]) -> str:
    """Format retrieved chunks into a context block for the LLM prompt.

    Args:
        chunks: Retrieved chunks from :func:`retrieve`.

    Returns:
        Formatted string with source attribution per chunk.
    """
    parts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        parts.append(f"[{i}] Source: {chunk.source}\n{chunk.text}")
    return "\n\n---\n\n".join(parts)


def rag_system_prompt(chunks: list[Chunk]) -> str:
    """Build the RAG system prompt with injected context.

    Args:
        chunks: Retrieved chunks for this query.

    Returns:
        Full system prompt string ready for ollama.chat().
    """
    return _RAG_SYSTEM.format(context=build_context(chunks))
