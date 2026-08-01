"""
Turning a product into text, and text into a vector.

STAGE 4 vs STAGE 9 — read this before touching this file.

This stage builds and tests the *dual-write mechanism*: does a product
write land in both SQL and the vector store, does update skip
unnecessary re-embedding, does a vector-store failure get flagged and
survive reconciliation, does archive remove the vector, etc. None of
that depends on the embeddings being semantically meaningful — it only
depends on `embed(text)` being a deterministic function from text to a
fixed-size vector.

The challenge requires every AI call to go through Mesh, and Mesh
integration is explicitly Stage 9's job (a centralized, tested,
retry-aware client). Building the real Mesh-backed embedding call here,
three stages early, would mean either duplicating that work or having
an untested AI client smuggled in ahead of schedule — both worse than
the alternative: implement embed() against a small interface
(`EmbeddingProvider`), ship a deterministic *local, non-AI* placeholder
implementation now, and swap in a `MeshEmbeddingProvider` behind the
same interface at Stage 9 without touching any dual-write code.

`LocalHashEmbeddingProvider` is a bag-of-words feature-hashing vector
(hash each token into one of N buckets, count, L2-normalize). It is
NOT semantic — "RAG" and "retrieval-augmented generation" will not be
recognized as related. It IS a real vector space with a real distance
metric (shared vocabulary between two texts increases cosine
similarity), which is exactly what's needed to test that the vector
store's upsert/query/delete plumbing works, without pretending to be
an AI capability this stage doesn't have yet.

At Stage 9, get_embedding_provider() below is the one line that
changes.
"""

import hashlib
import math
import re
from typing import Protocol

from app.db.models.product import Product

EMBEDDING_DIMENSIONS = 256


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


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """
    The one function everything else in the codebase calls. Swapping
    this to return a Mesh-backed provider at Stage 9 is the entire
    migration — app/retrieval/sync.py and app/products/service.py never
    import LocalHashEmbeddingProvider directly.
    """
    global _provider
    if _provider is None:
        _provider = LocalHashEmbeddingProvider()
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
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
