# ChennAI Labs

> Engineering the next generation of AI builders.

A behavioral AI recommendation platform for a technical learning catalog — DSA/MAANG interview prep, math for ML, and the full applied-AI ladder (ML, DL, NLP, CV, RL, LLMs end-to-end, agentic AI, RAG, fine-tuning, building products). Built for the SmartReco 2026 challenge; see the Stage 0 architecture document for the full design.

**Status: Stage 5 of 20 — behavioral tracking. Interest profiling, real retrieval, and the recommendation engine are not built yet.**

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

## Running tests

```bash
pytest
```

69 tests as of Stage 5: Stage 4's suite (53) plus event-tracking coverage (16) — batched ingestion, **duplicate-event handling** (both within one batch and across two separate requests, simulating a resent beacon), rejecting unknown event types and product-required events with no valid product without failing the rest of the batch, session-to-user reconciliation on register/login, and confirming the debug view is gated by `require_admin` like everything else under `/admin`.

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
│   ├── retrieval/
│   │   ├── embeddings.py        # EmbeddingProvider interface + local placeholder (Stage 9 swaps this)
│   │   ├── vector_store.py      # thin Chroma wrapper: upsert/delete/query/health_check
│   │   └── sync.py              # dual-write mechanics: sync/remove/reconcile/detect_drift
│   ├── templates/               # Jinja2 (auth/, courses/, admin/products/, admin/events/, partials/)
│   └── static/
│       ├── css/                  # brand tokens + site nav/catalog/admin styling
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
