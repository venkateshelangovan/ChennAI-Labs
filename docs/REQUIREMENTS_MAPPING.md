# Requirement → implementation mapping

Stage 0's Section 22 sketched this table before any code existed. This is
the same table filled in against the actual, shipped codebase as of Stage
18 — real file paths, not stage numbers alone. Stage 20 (final audit) is
the formal, requirement-by-requirement sign-off pass over this same list;
this document is its input, not a replacement for it.

| Requirement | Where it lives | Notes |
|---|---|---|
| FastAPI backend | `app/main.py`, every `app/*/routes.py` | Entire backend; server-rendered via Jinja2, no separate API-only layer |
| Mesh API only, no direct provider calls | `app/mesh/client.py` | The *only* module in the codebase allowed to import `httpx` for an AI call — `embed()` and `chat()` are the two public functions every caller goes through |
| Authentication, roles | `app/auth/service.py`, `app/auth/dependencies.py`, `app/auth/routes.py` | Session-token auth (opaque DB-backed tokens, not JWT), `user` / `admin` roles, `require_admin` dependency gates every `/admin/*` route |
| Product CRUD | `app/products/service.py`, `app/admin/routes.py` | Public browse/search in `app/products/routes.py`; admin create/update/archive in `app/admin/routes.py` |
| SQL database | `app/db/`, `alembic/` | SQLite locally, Postgres in production via the same SQLAlchemy models — no SQLite-specific SQL used anywhere, so the swap is a `DATABASE_URL` change only |
| Vector database | `app/retrieval/vector_store.py` | Thin Chroma wrapper (`upsert`/`delete`/`query`/`health_check`) — every other module talks to this interface, never to `chromadb` directly |
| Dual-write + sync | `app/retrieval/sync.py`, `app/products/service.py` | Synchronous dual-write with a `vector_sync_status` flag + `content_hash`, plus `reconcile()`/`detect_drift()` for recovering from a mid-outage failure |
| Behavioral tracking, non-blocking | `app/static/js/tracker.js`, `app/events/routes.py` | `navigator.sendBeacon`-based client, batched `POST /api/events`, anonymous-session reconciliation on login |
| Event storage | `app/db/models/user_event.py`, `app/events/service.py` | Append-only `user_events` table; dedup by `client_event_id` |
| Agentic recommendation engine | `app/recommendations/service.py` (Stage 8), `app/recommendations/orchestrator.py` (Stage 11) | Deterministic pipeline first, then a bounded retrieval-refinement retry loop — see `orchestrator.py`'s docstring for why this didn't need a graph framework |
| RAG / grounded retrieval | `app/retrieval/query.py`, `app/recommendations/narration.py` | Profile → query text → embedding → ranked candidates (Stage 7); narration is validated to only cite product IDs actually present in that candidate list (Stage 10) |
| Personalized, persuasive messaging | `app/recommendations/narration.py` | Mesh `chat()` call over the *already-final* recommendation list, with citation-grounding validation and a deterministic fallback if generation fails or hallucinates an ID |
| Recommendation storage + history | `app/db/models/recommendation_snapshot.py` | One `RecommendationSnapshot` row per generation, with `trigger_reason` and a full `trace` (Stage 14) |
| Recommendation refresh | `app/recommendations/trigger.py`, `app/recommendations/cache.py` | Deterministic "should this regenerate right now" evaluator (new events, staleness, TTL) — see `evaluate_trigger()`'s docstring for the exact rule set |
| Efficient AI-call triggering | `app/recommendations/trigger.py` | Same module as above — this *is* the efficiency mechanism: no AI call happens without a real trigger firing |
| Caching | `app/recommendations/cache.py` | The persisted `RecommendationSnapshot` row is the cache; no Redis — see the README's "Caching" section for why that's a deliberate, documented choice, not an oversight |
| README | `README.md` (this repo, root) | Maintained incrementally every stage since Stage 1, not written retroactively |
| requirements.txt | `requirements.txt` | Pinned, updated as each stage adds a real dependency |
| .gitignore, secrets not committed | `.gitignore`, `.env.example` | `.env` itself is git-ignored; `.env.example` documents every variable without real values |
| MESH_API_KEY via env | `app/core/config.py`, `.env.example` | Loaded through the typed `Settings` object, never hardcoded; blank is a supported "local dev without credentials" state (see README's Mesh sections) |
| Automated tests | `tests/` | 207 tests as of Stage 18, one per stage as it shipped, audited against a coverage checklist in Stage 17 (see README's "Full test suite + eval framework") |
| Production hardening (rate limiting, security headers, secrets) | `app/core/rate_limit.py`, `app/main.py`'s `security_headers` middleware, `app/core/config.py` | Stage 16 — see README's "Production hardening" section |
| Behavioral quality evaluation | `evals/run_evals.py` | Not pytest — runs the real pipeline against Stage 0's three demo user journeys and reports behavioral metrics, not pass/fail assertions (Stage 17) |
| UI/UX polish, accessibility | `app/static/css/main.css`, `app/templates/` | Stage 13 (dashboard-scoped) and Stage 18 (whole-site pass) — see README's corresponding sections |

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full original design rationale (Sections 1–24), and the main [README](../README.md) for how each piece actually works today, stage by stage.
