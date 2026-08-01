"""
Turning a product into text, and text into a vector.

STAGE 4 vs STAGE 9 — history, kept for context.

Stage 4 built and tested the *dual-write mechanism*: does a product
write land in both SQL and the vector store, does update skip
unnecessary re-embedding, does a vector-store failure get flagged and
survive reconciliation, does archive remove the vector, etc. None of
that depended on the embeddings being semantically meaningful — only on
`embed(text)` being a deterministic function from text to a fixed-size
vector. So Stage 4 implemented `embed()` against a small interface
(`EmbeddingProvider`) with a deterministic *local, non-AI* placeholder,
`LocalHashEmbeddingProvider` — a bag-of-words feature-hashing vector
(hash each token into one of N buckets, count, L2-normalize). It was
never semantic — "RAG" and "retrieval-augmented generation" were never
recognized as related — but it was a real vector space with a real
distance metric, which was exactly what dual-write plumbing needed to
be provably correct without an untested AI client smuggled in three
stages early.

Stage 9 is that swap: `get_embedding_provider()` now returns
`MeshEmbeddingProvider`, and `LocalHashEmbeddingProvider` stays in the
codebase for two reasons, not one — it's still what the test suite
uses (see tests/conftest.py's `isolated_embedding_provider` fixture,
which keeps every test that creates/updates a product from making a
real network call), and it's a useful reference for what "a minimal
EmbeddingProvider implementation" looks like.

Nothing downstream of `get_embedding_provider()` changed —
app/retrieval/sync.py and app/products/service.py never imported
LocalHashEmbeddingProvider directly, which was the entire point of
building the interface this way back in Stage 4.
"""

import hashlib
import math
import re
from typing import Protocol

from app.core.config import settings
from app.db.models.product import Product
from app.mesh import client as mesh_client

EMBEDDING_DIMENSIONS = 256

# Bumped whenever the active embedding provider (or its model) changes
# in a way that changes the resulting vector space — Stage 9's swap
# from the 256-dim local hash placeholder to Mesh's real embedding
# model is the first such change. Folded into compute_content_hash so
# a provider swap naturally invalidates every previously-computed hash
# rather than silently leaving stale vectors marked "synced". This
# alone isn't a full migration, though — app/retrieval/sync.py's
# reconcile() only retries rows that AREN'T marked 'synced', so
# products embedded under the old version and still marked 'synced'
# need the explicit one-time sweep in scripts/reindex_all.py, which
# also resets the vector store outright (old and new embeddings are
# different dimensions — they can't coexist in one Chroma collection).
EMBEDDING_SCHEMA_VERSION = "mesh-v1"


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...


class LocalHashEmbeddingProvider:
    """Deterministic, dependency-free, non-AI placeholder. See module docstring."""

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            return vector
        return [v / norm for v in vector]


class MeshEmbeddingProvider:
    """
    Stage 9: the real embedding provider. Delegates the actual HTTP
    call to app/mesh/client.py — this class's only job is conforming to
    the EmbeddingProvider protocol and supplying the configured model
    name, so app/retrieval/sync.py and everything upstream of it never
    needs to know Mesh exists, let alone how to call it, retry it, or
    parse its response.
    """

    def embed(self, text: str) -> list[float]:
        return mesh_client.embed(text, model=settings.mesh_embedding_model)


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """
    The one function everything else in the codebase calls. This is
    the one line Stage 4's docstrings promised would change at Stage 9
    — and it's the only line that did.
    """
    global _provider
    if _provider is None:
        _provider = MeshEmbeddingProvider()
    return _provider


def build_embedding_text(product: Product) -> str:
    """
    The normalized text block that gets embedded — per Stage 0 Section
    8: title, category>subcategory, level, tags, and a truncated
    description (not the full free-text description, which would dilute
    the fields that actually discriminate between courses for retrieval
    purposes).
    """
    subcategory_part = f" > {product.subcategory}" if product.subcategory else ""
    tags_part = ", ".join(product.tags or [])
    summary = (product.description or "")[:300]
    return (
        f"Title: {product.title}\n"
        f"Category: {product.category}{subcategory_part}\n"
        f"Level: {product.level}\n"
        f"Tags: {tags_part}\n"
        f"Summary: {summary}"
    )


def compute_content_hash(text: str) -> str:
    versioned = f"{EMBEDDING_SCHEMA_VERSION}:{text}"
    return hashlib.sha256(versioned.encode("utf-8")).hexdigest()
