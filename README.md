# ChennAI Labs

> Engineering the next generation of AI builders.

A behavioral AI recommendation platform for a technical learning catalog — DSA/MAANG interview prep, math for ML, and the full applied-AI ladder (ML, DL, NLP, CV, RL, LLMs end-to-end, agentic AI, RAG, fine-tuning, building products). Built for the SmartReco 2026 challenge; see the Stage 0 architecture document for the full design.

**Status: Stage 6 of 20 — deterministic interest profiling. Real (semantic) retrieval and the recommendation engine are not built yet.**

### A note on embeddings right now

Every product create/update/archive is dual-written to a Chroma vector store, with full failure handling and a repair sweep (see below). But the actual embedding vectors right now come from a deterministic, local, non-AI placeholder (`app/retrieval/embeddings.py`) — **not Mesh**. This is intentional, not a shortcut: Mesh integration is Stage 9's job (a proper centralized, tested, retry-aware AI client), and this stage's job is proving the dual-write mechanism itself — sync on create, skip unnecessary re-embeds on update, remove on archive, survive and recover from a simulated vector-store outage. Building an untested, ad-hoc Mesh call three stages early would be worse than a clearly-labeled placeholder behind a swappable interface. At Stage 9, `get_embedding_provider()` is the one function that changes.

## Stack

- **Backend:** FastAPI
- **Frontend:** Jinja2 server-rendered HTML + vanilla JS (behavioral tracking, added Stage 5)
- **Database:** SQLite locally → PostgreSQL in production, via SQLAlchemy + Alembic migrations
- **Vector store:** Chroma, embedded, persisted to `VECTOR_DB_PATH`
- **AI:** Mesh API only (`https://api.meshapi.ai/v1`), never a direct LLM provider (Stage 9+; embeddings are a local placeholder until then — see above)

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
- `http://127.0.0.1:8000/dashboard` — protected page; redirects to `/login` if you're not authenticated
- `http://127.0.0.1:8000/admin/products` — admin catalog management (requires an admin account — see below); redirects non-authenticated visitors to `/login`, returns 403 for a logged-in non-admin. Each row shows a "Vector sync" status (synced/pending/failed); a banner with a "Sync now" button appears if anything needs (re)syncing
- `http://127.0.0.1:8000/admin/events` — behavioral event debug view (admin-only): every captured view/search/click/category_view/time_spent event, newest first, filterable by type
- `http://127.0.0.1:8000/profile` — your own interest profile (requires login): category affinity, top engaged-with courses, topics, and recent searches, each traceable back to the formula that produced it

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

## Running tests

```bash
pytest
```

82 tests as of Stage 6: Stage 5's suite (69) plus interest-profile coverage (13) — event-type weighting (click outweighs view in the expected ratio), recency decay (halves at exactly one half-life), time_spent capping, category/tag normalization, per-user isolation (one user's events never leak into another's profile), search-term dedup/casing, the `top_products` limit, cold-start handling, and that `/profile` is gated by `require_user` and actually renders the computed scores.

## Environment variables

See `.env.example` for the full list with comments. `SESSION_SECRET` and `SESSION_TTL_DAYS` control auth session cookies; `DATABASE_URL` points at your database (SQLite by default); `MESH_API_KEY` isn't needed until Stage 9. `.env` is git-ignored and must never be committed.

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
│   │   └── models/             # User, AuthSession, Product, UserEvent
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
│   ├── retrieval/
│   │   ├── embeddings.py        # EmbeddingProvider interface + local placeholder (Stage 9 swaps this)
│   │   ├── vector_store.py      # thin Chroma wrapper: upsert/delete/query/health_check
│   │   └── sync.py              # dual-write mechanics: sync/remove/reconcile/detect_drift
│   ├── templates/               # Jinja2 (auth/, courses/, admin/products/, admin/events/, profile/, partials/)
│   └── static/
│       ├── css/                  # brand tokens + site nav/catalog/admin/profile styling
│       └── js/tracker.js         # non-blocking behavioral event capture (see below)
├── alembic/                    # migrations (env.py wired to app Settings)
├── scripts/
│   ├── create_admin.py         # out-of-band admin provisioning
│   └── seed_products.py        # realistic starting catalog (idempotent)
├── tests/
├── requirements.txt
├── .env.example
└── .gitignore
```

## Roadmap

This project is built one stage at a time, each requiring explicit approval before moving on. See the Stage 0 architecture document for the full 20-stage roadmap, database/vector design, dual-write strategy, and requirement mapping.
