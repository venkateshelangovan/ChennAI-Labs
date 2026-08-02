# ChennAI Labs

> Engineering the next generation of AI builders.

A behavioral AI recommendation platform for a technical learning catalog — DSA/MAANG interview prep, math for ML, and the full applied-AI ladder (ML, DL, NLP, CV, RL, LLMs end-to-end, agentic AI, RAG, fine-tuning, building products). Built for the SmartReco 2026 challenge; see the Stage 0 architecture document for the full design.

**Status: Stage 12 of 20 — trigger + caching rules are live. `/dashboard` no longer recomputes the full pipeline (retrieval, refinement, novelty/diversity, Mesh narration) on every page view — a persisted `RecommendationSnapshot` is served as-is until a deterministic trigger (no snapshot yet / TTL elapsed with real new signal / manual refresh, rate-limited via cooldown) says it's actually time to regenerate.**

### Mesh API integration (Stage 9)

Every embedding call in this codebase goes through `app/mesh/client.py` — the *only* module allowed to call an AI provider, and Mesh the *only* provider it ever calls (never OpenAI/Anthropic/etc. directly, per the project's constraint). `MeshEmbeddingProvider` (`app/retrieval/embeddings.py`) wraps it behind the same `EmbeddingProvider` interface Stage 4 built, which is why nothing in `app/retrieval/sync.py`, `app/products/service.py`, or any stage built on top of retrieval (6, 7, 8) needed to change — `get_embedding_provider()` really was the one line Stages 4 through 8 promised would move.

**An honest note on the wire contract.** `api.meshapi.ai` isn't a real, reachable service for this project — Mesh is this challenge's stand-in for "an internal/managed AI gateway," and nothing specifies its actual HTTP shape. `app/mesh/client.py` implements the contract almost every embeddings-compatible gateway uses (OpenAI's — Azure OpenAI and most proxies in front of either mirror it too): `POST {MESH_BASE_URL}/embeddings` with `{"model": ..., "input": text}`, expecting back `{"data": [{"embedding": [...]}]}`. If a real Mesh spec differs, only the request-building and response-parsing in that one file need to change.

**Retry policy.** Retries only what a retry can plausibly fix — connection errors, timeouts, and 5xx responses — with exponential backoff (0.5s, 1s). A 4xx is never retried (a human/config problem, not a transient one), and a missing `MESH_API_KEY` fails immediately with no network attempt at all, for the same reason. Every failure surfaces as one `MeshAPIError`, which Stage 4's existing dual-write failure handling already catches uniformly — `sync_product` doesn't need to know or care *why* Mesh failed to do the right thing (log it, mark `vector_sync_status='failed'`, never roll back the SQL write).

**Local dev without real Mesh credentials still works.** `.env.example` ships `MESH_API_KEY=` blank on purpose. Auth, the catalog, event tracking, the interest profile, and retrieval preview all function with no Mesh access at all; only product vector sync fails (gracefully, per Stage 4), and Stage 8's recommendations fall back to the popularity ranking. This emergent graceful degradation is the payoff of Stages 4–8's architecture, not something Stage 9 had to build specially.

**Reindexing after a provider swap.** `compute_content_hash` now folds in `EMBEDDING_SCHEMA_VERSION`, so a future provider/model change naturally invalidates every hash going forward — but rows already marked `'synced'` under the *old* version aren't automatically caught by `reconcile()` (which, by design, only retries non-`'synced'` rows), and an old embedding's dimension is incompatible with a new provider's in the same Chroma collection. `scripts/reindex_all.py` is the explicit, one-time migration: reset the vector store outright, mark every active product `'pending'`, and run `reconcile()` to re-embed everything fresh. Deliberately manual, not automatic on deploy — re-embedding a whole catalog is a real (possibly billed) operation that should always be a human's decision.

```bash
python -m scripts.reindex_all
```

**Health check.** `/health`'s `mesh` field reports `"configured"` / `"not_configured"` based on whether an API key is present — it does **not** make a live call to Mesh. Unlike the database and vector store (free, local, cheap to check on every hit), Mesh is an external, metered API; a liveness probe that calls a billed endpoint every few seconds is a known anti-pattern. A missing/unreachable Mesh also never drops the overall status to `503` — the app is genuinely still usable without it (see above).

### Recommendation narration + grounding validation (Stage 10)

`app/mesh/client.py` gains a second method, `chat()`, sharing `embed()`'s retry/backoff/error handling via one extracted `_post()` helper — same contract shape everywhere: `POST {MESH_BASE_URL}/chat/completions` with `{"model", "messages", "temperature"}`, expecting `{"choices": [{"message": {"content": ...}}]}` back.

**The LLM never ranks anything.** `app/recommendations/narration.py` takes Stage 8's already-final `list[Recommendation]` — the deterministic novelty/diversity/cold-start pipeline is completely untouched — and asks Mesh for one short sentence *about* that list, nothing more. The prompt numbers each recommendation (`[1] Course A`, `[2] Course B`, ...) and instructs the model to cite that way when referring to a specific course.

**Grounding validation, not trust.** Every `[N]` citation in the model's response is checked against the actual recommendation count before anything is shown: `_validate_grounding()` extracts every bracketed number via `CITATION_PATTERN` and rejects the whole response if even one citation falls outside `1..len(recommendations)` — a single bad citation invalidates the response, not just the offending sentence, because there's no reliable way to show "half a summary" without risking the reader assuming the missing half was also checked. A response with zero citations is still valid (the model just didn't need to point at anything specific). Passing citations are substituted with the real product title (`[1]` → "Data Structures & Algorithms for MAANG Interviews") before rendering, so the text a user reads never contains raw citation markers.

**Failure is always silent, never broken.** An empty recommendation list, a Mesh outage/error, or a hallucinated out-of-range citation all resolve to the same outcome: `NarrationResult(text=None, grounded=False, fallback_reason=...)`, and `/dashboard` simply doesn't render the narration callout — the recommendation grid itself is entirely unaffected, because narration is generated *after* recommendations are finalized and never gates them. This was live-verified against a local mock Mesh server across all three paths: a grounded response with real citations, a response citing an out-of-range index (rejected, no leak), and a simulated `chat/completions` outage (three retries, then a clean fallback — `/dashboard` still returns `200` with recommendations intact, confirmed from the server log).

## Stack

- **Backend:** FastAPI
- **Frontend:** Jinja2 server-rendered HTML + vanilla JS (behavioral tracking, added Stage 5)
- **Database:** SQLite locally → PostgreSQL in production, via SQLAlchemy + Alembic migrations
- **Vector store:** Chroma, embedded, persisted to `VECTOR_DB_PATH`
- **AI:** Mesh API only (`https://api.meshapi.ai/v1` — a stand-in; see "Mesh API integration" below), never a direct LLM provider. Live as of Stage 9 for embeddings; LLM calls (grounding, agentic workflow) arrive Stage 10+

## Local setup

```bash
cd chennai_labs
python -m venv .venv

# macOS/Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp .env.example .env   # edit values as needed; defaults work for local dev

# Create the database schema
alembic upgrade head

# Load a realistic starting catalog (16 courses, safe to re-run)
python -m scripts.seed_products
```

## Running the server

```bash
uvicorn app.main:app --reload
```

Then visit:

- `http://127.0.0.1:8000/` — homepage: hero + featured courses
- `http://127.0.0.1:8000/courses` — browse/search/filter the catalog (`?q=`, `?category=`, `?level=`)
- `http://127.0.0.1:8000/courses/<slug>` — course detail page
- `http://127.0.0.1:8000/health` — liveness check; confirms the DB and vector store are both reachable
- `http://127.0.0.1:8000/register` / `/login` — auth
- `http://127.0.0.1:8000/dashboard` — protected page; redirects to `/login` if you're not authenticated. As of Stage 8, this is the real "Recommended for you" list from `app/recommendations/service.py`, not a placeholder
- `http://127.0.0.1:8000/admin/products` — admin catalog management (requires an admin account — see below); redirects non-authenticated visitors to `/login`, returns 403 for a logged-in non-admin. Each row shows a "Vector sync" status (synced/pending/failed); a banner with a "Sync now" button appears if anything needs (re)syncing
- `http://127.0.0.1:8000/admin/events` — behavioral event debug view (admin-only): every captured view/search/click/category_view/time_spent event, newest first, filterable by type
- `http://127.0.0.1:8000/profile` — your own interest profile (requires login): category affinity, top engaged-with courses, topics, and recent searches, plus (Stage 7) a "retrieval preview" showing the exact query text and ranked candidates a similarity search over the catalog returns right now — everything traceable back to the formula that produced it

## Creating an admin account

Admin accounts are never created through the public registration form — that's a script run out-of-band, on purpose (see `scripts/create_admin.py` for why):

```bash
python -m scripts.create_admin admin@chennailabs.dev "Str0ng-Password!" "Admin Name"
```

## Database migrations

Schema changes go through Alembic from day one, even against SQLite, so there's no "figure out migrations later" cliff when moving to Postgres.

```bash
alembic upgrade head                              # apply all pending migrations
alembic revision --autogenerate -m "add products"  # after changing a model
```

`alembic/env.py` reads the database URL from the app's own `Settings` (i.e. your `.env`'s `DATABASE_URL`) rather than a hardcoded value in `alembic.ini`, so migrations always target whatever database the app itself is configured to use.

## Vector store & dual-write

Every product create/update/archive/restore writes to SQL first (source of truth) and then attempts a vector-store sync as a best-effort follow-up — SQL never rolls back if the vector write fails. Each product tracks `vector_sync_status` (`pending`/`synced`/`failed`) and `content_hash` (used to skip re-embedding when an update didn't touch the embedded fields). The admin catalog page (`/admin/products`) surfaces a banner with a manual **Sync now** action when anything isn't synced — this calls `app/retrieval/sync.py`'s `reconcile()`, which retries failed products and cleans up any orphaned vector entries. The vector store itself is Chroma, persisted to `VECTOR_DB_PATH` (default `./.chroma`, git-ignored).

## Behavioral tracking

`app/static/js/tracker.js` captures `view`, `search`, `click`, `category_view`, and `time_spent` events client-side, batches them in memory (flushed on a size threshold, a 5s timer, or immediately before any navigation-causing action like a product click), and delivers them via `navigator.sendBeacon` (falling back to a `keepalive` fetch with one bounded retry) so tracking never blocks the page or the user. Every event carries a client-generated `session_id` (persisted in `localStorage` + a first-party cookie) so behavior is captured even before someone registers; logging in or registering reconciles that session's prior anonymous events onto the new account. Each event also carries a client-generated `client_event_id`, which `POST /api/events` deduplicates on — a resent/duplicate beacon is silently absorbed, not double-counted. See `/admin/events` to watch it happening live.

## Interest profiling

`app/profile/service.py` turns the raw `user_events` table into a deterministic "interest profile" — no AI, no embeddings, just arithmetic that's fully reproducible from the same rows. Every qualifying event contributes `base_weight(event_type) * recency_decay(event)` to one or more targets:

- **Event weights**: click (2.0) > category_view (1.5) > view (1.0); `time_spent` scales with dwell time instead of a flat weight, capped at 240s so an idle open tab can't dominate the profile. `search` contributes no category/tag weight at all — mapping free text to a category without real semantic search would be a fake signal, so queries are preserved verbatim (`search_terms`) for Stage 7 to use once it can do that safely.
- **Recency decay**: an exponential half-life (14 days) — older behavior fades but is never hard-cut the way a fixed lookback window would, so a user who goes quiet for a month and comes back still gets a profile instead of a blank one.
- **Aggregation targets**: category (normalized to sum to 1.0 — "relative share of attention"), tag (same, weight split evenly across a product's tags), and per-product ("top products," normalized against the user's single strongest signal), plus a deduplicated, most-recent-first list of raw search queries.

See it for yourself at `/profile` once logged in — every number on that page traces back to this one function, run over your own event rows.

## Retrieval (Stage 7)

`app/retrieval/query.py` turns an interest profile into a ranked list of candidate products by querying the Stage 4 vector store — the real retrieval plumbing (profile → query text → embedding → vector search → ranked candidates), built and tested end to end, but still running on `LocalHashEmbeddingProvider`'s non-semantic placeholder embeddings until Stage 9. That means today's ranking is driven by shared vocabulary, not meaning — "RAG" and "retrieval-augmented generation" won't be recognized as related yet. This is deliberate: it proves every piece of the pipeline works in isolation, so swapping in Mesh at Stage 9 is a one-line change to `get_embedding_provider()`, not a simultaneous "does retrieval work AND does Mesh work" debugging session.

Because the placeholder embedder is bag-of-words (a token's weight is how often it appears), the profile is turned into a query by repeating each category/tag token a number of times proportional to its normalized score — the closest a frequency-counting embedder can get to "respect these weights." Similarity is computed as `1 - distance/2`, an exact conversion from Chroma's L2 distance given that both product and query embeddings are unit-normalized.

This is explicitly **not** the recommendation engine (Stage 8) — no ranking beyond raw distance, no novelty/diversity logic, no "already purchased" filtering. It's retrieval in isolation, visible at `/profile`'s "Retrieval preview" section (query text + ranked candidates, clearly labeled as not real recommendations yet) so each layer can be verified independently.

## Recommendation pipeline (Stage 8)

`app/recommendations/service.py` turns Stage 7's raw retrieval candidates into the actual list rendered on `/dashboard`. Three deterministic rules, no AI:

- **Novelty** — products the user has already viewed, clicked, or spent time on are excluded. Stage 7's query is built out of exactly that engagement, so without this the #1 "recommendation" is routinely the course the user just clicked — a correct nearest neighbor, not a useful suggestion.
- **Diversity** — no more than `MAX_PER_CATEGORY` (2) results from the same category, via a greedy re-rank that only relaxes the cap if there aren't enough other categories in the candidate pool to fill the list otherwise.
- **Cold start** — a user with no behavioral signal (or one whose only retrieval candidates are things they've already engaged with) gets a deterministic, non-personalized fallback: highest-rated active courses, diversified the same way. The same fallback correctly excludes already-engaged products too, so it never turns around and recommends the one course a user has clicked just because the personalized path came up empty.

Every card on `/dashboard` shows its `reason` — one of two fixed, non-generated templates ("Matches your recent interest in {category}" or the popularity-fallback explanation), never text written about the user. Compare `/dashboard`'s final list against `/profile`'s raw, unfiltered retrieval preview to see exactly what novelty + diversity changed.

## Agentic workflow — the retrieval-refinement loop, and the LangGraph decision (Stage 11)

The Stage 0 architecture doc deliberately deferred one call to this stage: the pipeline's flowchart has exactly one cyclic edge (retrieve → is the quality acceptable? → no → refine the query and retry), and whether that cyclic decision earns LangGraph or stays a plain Python loop was left an open question until there was a real loop to look at.

**The call: no LangGraph, at least not yet.** `app/recommendations/orchestrator.py` implements the loop as a bounded `for` loop, not a graph. The reasoning, in short: LangGraph earns its keep on multiple interacting branches, multi-agent handoff, or state that needs to survive a process boundary — none of which this workflow has. One conditional retry, capped at `MAX_REFINE_ATTEMPTS` (1), is a 5-line loop, and this codebase's existing structured JSON logging (Stage 0, Section 17) already answers "why did this happen" without a second tracing system. Adding graph machinery ahead of an actual need would repeat the exact anti-pattern this project has already declined for Redis and Celery elsewhere. Full reasoning — including what would change this call later — lives in the module's docstring.

**What "weak retrieval" means.** Two deterministic checks against Stage 7's raw candidates, before Stage 8's novelty/diversity filtering ever runs: fewer than `MIN_CANDIDATES` (5) hits came back at all, or even the single closest candidate's similarity is below `MIN_TOP_SIMILARITY` (0.15). Both are computed on the *raw* pool on purpose — a query that returns plenty of strong matches the user already engaged with isn't a retrieval-quality problem, that's exactly what Stage 8 already handles correctly.

**The refinement itself.** `_narrow_profile` rebuilds the same `InterestProfile` keeping only the single strongest category and tag (both lists are already sorted by score), dropping top-product titles and search terms entirely. Every token in the retried query still comes from the user's real profile — narrowing just removes the low-weight noise that can dilute a real signal in Stage 7's query-text synthesis, without inventing anything.

**Bounded, and always safe.** After `MAX_REFINE_ATTEMPTS`, the orchestrator hands off whichever attempt scored better (more candidates, then higher top similarity) rather than looping indefinitely. Stage 8's own fallback logic downstream is untouched and still runs on whatever comes back — Stage 11's job is only to give the deterministic pipeline the best shot at a real result, never to guarantee one exists. `RecommendationResult.retrieval_refined` surfaces whether refinement fired, for observability.

Live-verified against the real seeded catalog and a mock Mesh server: a normal user's retrieval passed the quality gate on the first attempt (`quality_ok: true`, no refinement — the 16-course catalog is comfortably above the bar). To exercise the refine path itself, the catalog was temporarily thinned to 3 active courses via the real `archive_product` path (proper dual-write, not a raw SQL shortcut): the first attempt logged `too_few_candidates`, `retrieval_refining` fired, the narrowed retry still logged `too_few_candidates` (3 active courses can never clear a floor of 5, no matter how the query is narrowed), and the loop stopped at the bound — `/dashboard` still returned `200` with recommendations rendered, exactly the "give up gracefully, never break" behavior this stage was built to guarantee.

## Trigger + caching (Stage 12)

Through Stage 11, every single `/dashboard` view recomputed the entire pipeline from scratch — Stage 7-11's retrieval and Stage 10's Mesh chat call included — on every page load. Correct, but proportional to page views rather than to actual behavior change: reloading the dashboard five times in a minute doesn't produce five times as much genuine signal. Stage 12 closes that gap with exactly the mechanism Stage 0's Section 14/15 describe: a persisted cache, and a deterministic trigger that decides whether it's still good enough to serve as-is.

**What's cached.** `RecommendationSnapshot` (`app/db/models/recommendation_snapshot.py`) — one row per user, upserted, storing only the ranking decision (product IDs, order, reason, similarity) plus the narration result. Display fields are deliberately never duplicated into it: `app/recommendations/cache.py` re-fetches the real `Product` rows by ID on every cache hit, so a price or rating change is never stale just because the recommendation itself hasn't changed. If a cached product was archived since the snapshot was generated, it's silently dropped (the familiar "vector index ahead of SQL" drift-safety pattern); if every cached product has vanished, the snapshot is treated as a cache miss and regenerated rather than shown as an honestly-empty list.

**The trigger, `app/recommendations/trigger.py`.** Three conditions, all deterministic DB queries, zero AI cost to evaluate:

- **No snapshot yet** — always regenerate.
- **TTL elapsed (`TTL_HOURS` = 24) *and* meaningful signal since** — at least `MIN_EVENTS_SINCE_REFRESH` (5) events, or even a single `search` event (strong explicit intent), landed after the snapshot was generated. Both conditions, not either: a snapshot that's merely old but nothing new happened isn't stale in any way that matters, and regenerating it would spend a Mesh call to produce the same answer.
- **Manual refresh** — a "Refresh recommendations" button on `/dashboard` (`POST /dashboard/refresh`, same CSRF-protected POST-then-redirect pattern as `/admin/products/sync`). Section 16 asks for this to be rate-limited; Stage 16's real rate limiting doesn't exist yet, so this reuses state that's already there instead of building ahead of that stage — a manual refresh within `MANUAL_REFRESH_COOLDOWN_SECONDS` (60) of the last generation is refused, using the snapshot's own `generated_at` as the cooldown marker rather than a second piece of state to keep in sync.

Everything else is a cache hit — including a merely-stale-but-signal-free snapshot. The asymmetry is deliberate: a false "still fresh" costs a mildly outdated recommendation, a false "stale" costs an unnecessary AI spend.

**Why generation is still synchronous, in-request.** Stage 0 describes a stale-while-revalidate pattern (serve the old value immediately, regenerate in the background via `BackgroundTasks`) — that's explicitly scoped to Stage 15's proactive digest. Through Stage 14, the same section says generation runs synchronously within the triggering request; Stage 12 only decides *whether* to pay that cost on a given request, not how to hide it.

Live-verified end to end against the real seeded catalog and a mock Mesh server, all six trigger outcomes, using the actual Mesh call count observed in the mock server's log as ground truth (not just HTTP status codes): first-ever dashboard view for a new user made exactly one Mesh call (`no_snapshot`); an immediate second view made zero (`fresh`); pushing the snapshot's `generated_at` back 25 hours with no new events still made zero (`stale_but_no_signal`); adding 5 events after that same old snapshot made one (`ttl_and_signal`); a manual refresh immediately after that made zero (`manual_refresh_on_cooldown`); and a manual refresh after simulating 90 elapsed seconds made one (`manual_refresh`) — confirmed by the dashboard's cache banner (`Freshly generated just now` vs. `...no AI calls made for this page view`) matching the Mesh call count in every case.

## Running tests

```bash
pytest
```

162 tests as of Stage 12: Stage 11's suite (143) plus 19 covering the trigger + cache — `tests/test_trigger.py`'s deterministic decision logic (no snapshot, fresh, stale-with/without-signal, manual refresh with/without cooldown, events before the snapshot don't count as new signal), and `tests/test_cache.py`'s end-to-end integration tests, which assert actual CALL COUNTS on `generate_recommendations`/`generate_narration` (not just final content) to prove a cache hit really does skip the expensive pipeline — plus a live-Product-refetch test (a cached recommendation reflects a price change made after the snapshot) and an all-cached-products-archived fallback test.

## Environment variables

See `.env.example` for the full list with comments. `SESSION_SECRET` and `SESSION_TTL_DAYS` control auth session cookies; `DATABASE_URL` points at your database (SQLite by default); `MESH_API_KEY` / `MESH_BASE_URL` / `MESH_EMBEDDING_MODEL` / `MESH_CHAT_MODEL` configure Mesh (Stage 9+) — leave `MESH_API_KEY` blank for local dev without real credentials (see "Mesh API integration" and "Recommendation narration" above for what still works without it). `.env` is git-ignored and must never be committed.

## Project structure

```
chennai_labs/
├── app/
│   ├── main.py              # FastAPI app, routes, middleware, exception handlers
│   ├── templating.py         # shared Jinja2Templates instance
│   ├── core/
│   │   ├── config.py          # typed Settings loaded from .env
│   │   ├── logging.py         # structured JSON logging
│   │   ├── security.py        # password hashing, session token generation
│   │   ├── csrf.py            # double-submit-cookie CSRF for form posts
│   │   ├── exceptions.py      # NotAuthenticated / NotAuthorized
│   │   └── time.py            # utcnow() — the one clock the whole app uses
│   ├── db/
│   │   ├── base.py            # declarative Base + register_models()
│   │   ├── session.py         # engine, SessionLocal, get_db dependency
│   │   └── models/             # User, AuthSession, Product, UserEvent, RecommendationSnapshot
│   ├── auth/
│   │   ├── service.py          # register/authenticate/session logic (no HTTP)
│   │   ├── dependencies.py     # get_current_user, require_user, require_admin
│   │   └── routes.py           # register/login/logout/dashboard HTTP handlers
│   │   │                        #   (also runs event reconciliation on register/login)
│   ├── products/
│   │   ├── service.py           # catalog CRUD, slug generation, search/filter,
│   │   │                         #   dual-write orchestration (calls app/retrieval)
│   │   └── routes.py            # public /courses browse + detail
│   ├── admin/
│   │   ├── routes.py            # /admin/products CRUD + manual "Sync now", gated by require_admin
│   │   └── events_routes.py     # /admin/events debug view, gated by require_admin
│   ├── events/
│   │   ├── schemas.py           # EventIn/EventBatchIn, batch size cap
│   │   ├── service.py           # ingest_events (validate/dedup/insert), reconcile_session
│   │   └── routes.py            # POST /api/events — the tracker's only backend endpoint
│   ├── profile/
│   │   ├── schemas.py            # InterestProfile value objects (CategoryScore, TagScore, ...)
│   │   ├── service.py            # build_interest_profile — deterministic event aggregation
│   │   └── routes.py             # GET /profile — the inspectable interest-profile page
│   ├── recommendations/
│   │   ├── schemas.py            # Recommendation (wraps a real Product) / RecommendationResult
│   │   ├── service.py            # generate_recommendations — novelty, diversity, popular fallback
│   │   ├── narration.py          # Stage 10: LLM summary over an already-final list + grounding validation
│   │   ├── orchestrator.py       # Stage 11: bounded retrieval quality-gate + refine/retry loop (no LangGraph — see docstring)
│   │   ├── trigger.py            # Stage 12: deterministic "should this regenerate right now" decision
│   │   └── cache.py              # Stage 12: serves the persisted RecommendationSnapshot or regenerates + persists
│   ├── retrieval/
│   │   ├── embeddings.py        # EmbeddingProvider interface, MeshEmbeddingProvider (default) +
│   │   │                         #   LocalHashEmbeddingProvider (kept as the test suite's stand-in)
│   │   ├── vector_store.py      # thin Chroma wrapper: upsert/delete/query/reset_collection/health_check
│   │   ├── sync.py              # dual-write mechanics: sync/remove/reconcile/detect_drift
│   │   └── query.py             # Stage 7: profile -> query text -> embedding -> ranked candidates
│   ├── mesh/
│   │   └── client.py             # the ONLY module allowed to call an AI provider —
│   │                              #   retry-aware Mesh HTTP client: embed() (Stage 9), chat() (Stage 10)
│   ├── templates/               # Jinja2 (auth/, courses/, admin/products/, admin/events/, profile/, partials/)
│   └── static/
│       ├── css/                  # brand tokens + site nav/catalog/admin/profile styling
│       └── js/tracker.js         # non-blocking behavioral event capture (see below)
├── alembic/                    # migrations (env.py wired to app Settings)
├── scripts/
│   ├── create_admin.py         # out-of-band admin provisioning
│   ├── seed_products.py        # realistic starting catalog (idempotent)
│   └── reindex_all.py          # Stage 9: one-time re-embed-everything migration (provider swap)
├── tests/
├── requirements.txt
├── .env.example
└── .gitignore
```

## Roadmap

This project is built one stage at a time, each requiring explicit approval before moving on. See the Stage 0 architecture document for the full 20-stage roadmap, database/vector design, dual-write strategy, and requirement mapping.
