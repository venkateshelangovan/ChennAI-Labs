"""
Stage 10: turning Stage 8's deterministic recommendation list into a
short, natural-language summary — the first place an LLM's output
reaches a user in this codebase, and the first place "grounding
validation" (Stage 0's explicit requirement) actually means something.

--- What the LLM is, and is not, allowed to do ---

The LLM NEVER decides what to recommend — that's Stage 8's job, and it
stays fully deterministic (novelty filter, diversity cap, popularity
fallback), unchanged by anything in this file. All the LLM does here is
write a couple of sentences ABOUT an already-final list. If Mesh is
unreachable, misconfigured, or returns something that fails grounding
validation, the caller (app/auth/routes.py's dashboard handler) simply
doesn't show a narration — every per-card `reason` (Stage 8's fixed,
non-generated templates) is still there and always was the ground
truth. Losing the narration is a cosmetic downgrade, never a functional
one.

--- Grounding validation: how, and why this specific mechanism ---

Free-text hallucination is hard to catch reliably — checking whether a
generated sentence "is about" a real course is a fuzzy, unreliable
string-matching problem. So the prompt doesn't ask for free reference
at all: it gives the model a NUMBERED list of the actual recommended
courses and instructs it to cite them only as `[1]`, `[2]`, etc. —
never by name, never inventing a number. Validating that is then a
simple, deterministic check with no ambiguity: extract every `[n]` in
the output via regex, and reject the whole thing if any `n` falls
outside `1..len(recommendations)`. This is the same idea the platform's
own RAG course material describes (grounding generated text in cited
sources) applied to the one place this app actually generates text.

This validates that citations *could* be real — it does not (and
cannot, without another AI call) verify that every *sentence* is
accurate. That's an intentionally narrow definition of "grounded" here:
enough to guarantee the model can't invent a course that doesn't exist
in the list it was given, which is the specific failure mode that would
make a "recommendation explanation" actively misleading.

Once validated, every `[n]` is substituted with that course's REAL
title (from our own data, not the model's text) before the narration
is ever shown — a user should never see raw citation-bracket syntax in
their UI, and this substitution is also a second, independent proof
that a passed-validation citation really does resolve to a real course.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.core.config import settings
from app.mesh import client as mesh_client
from app.recommendations.schemas import Recommendation

logger = logging.getLogger("chennai_labs.recommendations.narration")

CITATION_PATTERN = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT = (
    "You write a short, friendly 2-3 sentence summary introducing a list of recommended "
    "online courses. You will be given a numbered list of the ONLY courses that exist for "
    "this purpose. Rules, all mandatory: "
    "1) Reference a course ONLY as its bracketed number, e.g. [1] or [2] — never by title, "
    "never inventing a number not in the list. "
    "2) Do not mention, imply, or invent any course, topic, or fact not present in the list. "
    "3) Do not address the reader by name or invent anything about their history beyond what "
    "the category labels given to you say. "
    "4) Keep it to 2-3 sentences, warm but not gushing."
)


@dataclass
class NarrationResult:
    text: str | None  # None whenever narration isn't shown — always a safe, valid state
    grounded: bool
    fallback_reason: str | None  # "no_recommendations" | "mesh_error" | "ungrounded_citation" | None


def _build_messages(recommendations: list[Recommendation]) -> list[dict]:
    lines = []
    for i, rec in enumerate(recommendations, start=1):
        lines.append(f"[{i}] {rec.product.title} — category: {rec.product.category}, level: {rec.product.level}")
    user_prompt = "Recommended courses:\n" + "\n".join(lines)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _validate_grounding(text: str, recommendations: list[Recommendation]) -> bool:
    citations = [int(n) for n in CITATION_PATTERN.findall(text)]
    return all(1 <= n <= len(recommendations) for n in citations)


def _substitute_citations(text: str, recommendations: list[Recommendation]) -> str:
    """Replaces every validated [n] with that course's real title — a user
    should never see raw citation-bracket syntax, and this substitution is
    itself a second, independent proof that the citation resolves to a
    real course rather than trusting the model's own rendering of it."""

    def _replace(match: re.Match) -> str:
        index = int(match.group(1))
        return recommendations[index - 1].product.title

    return CITATION_PATTERN.sub(_replace, text)


def generate_narration(recommendations: list[Recommendation]) -> NarrationResult:
    if not recommendations:
        return NarrationResult(text=None, grounded=False, fallback_reason="no_recommendations")

    try:
        raw_text = mesh_client.chat(_build_messages(recommendations), model=settings.mesh_chat_model)
    except mesh_client.MeshAPIError as exc:
        logger.warning("narration_mesh_error", extra={"error": str(exc)})
        return NarrationResult(text=None, grounded=False, fallback_reason="mesh_error")

    text = raw_text.strip()
    if not _validate_grounding(text, recommendations):
        logger.warning("narration_ungrounded", extra={"text": text})
        return NarrationResult(text=None, grounded=False, fallback_reason="ungrounded_citation")

    display_text = _substitute_citations(text, recommendations)
    return NarrationResult(text=display_text, grounded=True, fallback_reason=None)
