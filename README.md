# ChennAI Labs

> Engineering the next generation of AI builders.

A behavioral AI recommendation platform for a technical learning catalog — DSA/MAANG interview prep, math for ML, and the full applied-AI ladder (ML, DL, NLP, CV, RL, LLMs end-to-end, agentic AI, RAG, fine-tuning, building products). Built for the SmartReco 2026 challenge; see the Stage 0 architecture document for the full design.

**Status: Stage 15 of 20 (bonus) — a proactive daily digest. An APScheduler job now regenerates every user's `RecommendationSnapshot` once a day (`DIGEST_HOUR_UTC`, default 06:00 UTC), calling the exact same generate-and-persist path `/dashboard` uses in-request, tagged `trigger_reason="scheduled_digest"`. No email/notification infrastructure — see "Proactive digest" below for the scope decision. An admin "Run digest now" button on `/admin/recommendations` triggers it on demand.**

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
- `http://127.0.0.1:8000/admin/recommendations` — behavior & recommendations view (admin-only, Stage 14): every user with a generated recommendation, linking to a per-user full trace (retrieval attempts, candidate pool with scores, raw narration output, rejected citations, recent events); a "Run digest now" button (Stage 15) regenerates every user's recommendation on demand, same function the scheduled daily job calls
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

Live-verified end to end against the real seeded catalog and a mock Mesh server, all six trigger outcomes, using the actual Mesh call count observed in the mock server's log as ground truth (not just HTTP status codes): first-ever dashboard view for a new user made exactly one Mesh call (`no_snapshot`); an immediate second view made zero (`fresh`); pushing the snapshot's `generated_at` back 25 hours with no new events still made zero (`stale_but_no_signal`); adding 5 events after that same old snapshot made one (`ttl_and_signal`); a manual refresh immediately after that made zero (`manual_refresh_on_cooldown`); and a manual refresh after simulating 90 elapsed seconds made one (`manual_refresh`) — confirmed by the dashboard's cache banner (`Updated just now` vs. `...served from cache, no AI calls made`) matching the Mesh call count in every case.

## Recommendation UX polish (Stage 13)

Scoped narrowly to the dashboard/recommendation experience on purpose — Stage 18 ("UI/UX final polish") is the whole-site design-system pass; Stage 13 only touches what a user sees around their actual recommendation. No business logic changed: every value shown here (similarity, strategy, generation timestamp) was already computed by Stages 8-12, just not surfaced before.

**A latent gap, fixed first.** `main.css` has defined `--font-ui` (Inter) and `--font-narrative` (Source Serif 4) since Stage 1, but no page ever actually loaded those font files — every page had silently been rendering system-font fallbacks the whole time. `base.html` now links Google Fonts for both; without this, Stage 13's one narrative-font decision below would have had no visible effect at all.

**What's new, in order of what a user actually sees:**

- The AI narration (Stage 10) now renders in Source Serif 4, not the UI sans-serif — exactly the "generated explanation reads as written, not chrome" distinction the Stage 0 design language called for.
- A "Personalized" or "Popular picks" badge sits right next to the "Recommended for you" heading, so the cold-start → personalized transition (Journey 1) is visible at a glance, not just inferable from the explainer copy underneath.
- Each personalized card shows a "`NN`% match" badge, built from `Recommendation.similarity` — a field the pipeline has computed since Stage 8 but never displayed. Absent entirely on the popularity fallback, where there's honestly no similarity to show (`similarity=None`).
- The Stage 12 cache banner's copy shifted from a debug-flavored string ("no AI calls made for this page view") to a two-tier line: a real timestamp as the primary message, the cache mechanic as a smaller secondary note — still fully transparent, just no longer reading like an internal log line on a page a real learner would see.
- The manual refresh button gets a lightweight loading state (vanilla JS, no new dependency — disables itself and reads "Refreshing…" while the POST is in flight). This is Stage 0 Section 15's explicit requirement ("shown with a loading state") for the one action left, post-Stage-12, where generation still happens synchronously on every click.

Live-verified against the real seeded catalog and mock Mesh server: the font stylesheet link resolves with the correct `family=Inter...Source+Serif+4...` query string, and a real personalized dashboard render showed genuine match badges (`79% match`, `74% match`, `73% match`, taken directly from that user's actual retrieval similarity scores) alongside the `Personalized` badge and the serif-styled narration text.

## Observability (Stage 14)

Stage 0 Section 17's ask: "we can answer 'why did this user get this recommendation' from logs alone," plus a `trace` JSON blob stored on the row itself — "because logs rotate and the audit trail shouldn't." Both halves are now real.

**The trace, captured at generation time, never reconstructed after the fact.** `RecommendationResult.trace` (`app/recommendations/schemas.py`) is populated by `app/recommendations/service.py` on every code path — cold start, no retrieval candidates, all candidates already engaged, personalized — with Stage 11's full retrieval-attempt history (per attempt: refined?, candidate count, top similarity, quality verdict, the actual query text) and the winning attempt's raw candidate pool (every product considered, with its real similarity score, not just the ones that made the final cut). `app/recommendations/narration.py`'s `NarrationResult` gained `raw_text` (Mesh's exact output before citation substitution) and `rejected_citations` (which `[n]` indices, if any, failed grounding validation) — trace-only fields, never sent to a template a real user sees. `app/recommendations/cache.py` merges both into `RecommendationSnapshot.trace` at the same moment it persists the ranking decision, and logs a structured `recommendation_generated` line (`snapshot_id`, `user_id`, `strategy`, `trigger_reason`, `latency_ms`, ...) — logs for real-time debugging, the persisted trace for after-the-fact inspection, both written from the same call so they can't drift into two different stories about the same event.

**The admin view (Journey 3).** `/admin/recommendations` lists every user with a generated recommendation (strategy, trigger reason, narration grounding, generated-at); `/admin/recommendations/{user_id}` is the full trace for one user — the retrieval-attempts table, the full candidate pool with each product's similarity and whether it was excluded for already being engaged with, the narration's raw pre-validation text and any rejected citations, and that user's recent raw events alongside it, all read directly off the `RecommendationSnapshot` row rather than recomputed — Section 17's explicit "surfaced directly," not "answered fresh," which could legitimately differ from what the user actually saw.

Live-verified against the real seeded catalog and mock Mesh server: a personalized user's trace showed real candidate similarities (`79%`, `74%`, `73%`, `73%`, `72%`) pulled straight from that request's actual retrieval. A hallucination scenario (a product retitled to trigger the mock server's citation-`[99]` response, engineered via the real `product_service` write path so novelty exclusion wouldn't strip it from the candidate pool) showed up in the admin trace exactly as it happened: raw Mesh output `"You should definitely check out [99], it's fantastic and not in your list at all."`, fallback reason `ungrounded_citation`, and a `99` pill under "Rejected citations" — none of which ever reached that user's actual dashboard.

## Proactive digest (Stage 15, bonus)

Stage 0 Section 15 lists a daily digest as a bonus feature that "delivers" fresh recommendations to users. A full-document grep found no email/SMTP/notification infrastructure planned anywhere else in the spec — no `smtplib`, no templates, nothing. So "delivers" is scoped narrowly and honestly: this job does **not** send anything to anyone. It regenerates and persists a fresh `RecommendationSnapshot` for every user who already has one, once a day, so the next time they open their dashboard Stage 12's cache serves an already-current recommendation instead of paying generation latency in-request. That is the entire scope — building a real email pipeline here would be inventing a feature the spec never actually describes.

**Why APScheduler, not Celery.** Stage 0 Section 15 explicitly calls the digest out as "APScheduler (digest only)" — the one background job in this whole project that isn't kept synchronous in-request the way the Stage 4 dual-write and Stage 9 Mesh calls are. It's a single cron-scheduled function call once a day, in-process; reaching for Celery would mean standing up a message broker and a separate worker process for a job that runs once every 24 hours.

**`app/recommendations/digest.py` — `run_daily_digest(db, *, now=None)`.** Iterates every user who has an existing `RecommendationSnapshot` (users who've never visited a dashboard are skipped — there's nothing to keep fresh for them), and regenerates+persists each one via `app/recommendations/cache.py`'s `regenerate_and_persist` — the exact same function `/dashboard`'s in-request cache miss calls, just tagged `trigger_reason="scheduled_digest"` instead of `"no_snapshot"`/`"ttl_and_signal"`/etc. There is only one code path that ever writes a snapshot. Each user is wrapped in its own try/except with a `db.rollback()` on failure, so one user's Mesh timeout or data oddity can't abort the run and leave every other user stale; `digest_run_started`/`digest_run_completed` structured log lines report the summary.

**`app/core/scheduler.py`.** A `BackgroundScheduler` (not `AsyncIOScheduler` — the digest job is synchronous SQLAlchemy + synchronous Mesh `httpx` calls end to end, matching every other service module in this codebase, so there's no async loop for it to cooperate with), registered with a daily cron job at `settings.digest_hour_utc`. `app/main.py`'s FastAPI startup/shutdown events start and stop it, gated by `settings.digest_enabled`. `tests/conftest.py`'s autouse `no_digest_scheduler` fixture forces `digest_enabled=False` for the whole test suite — verified directly that `TestClient(app)` used without a `with` block (as the `client` fixture does) never fires FastAPI's lifespan events on this FastAPI/Starlette version, so no real background thread was ever at risk during the 180-test run, but the fixture exists anyway as an explicit, version-independent guarantee.

**"Run digest now."** Waiting up to 24 hours to see the digest run is a bad operational experience, so `/admin/recommendations` got a "Run digest now" button — same CSRF-protected POST-then-redirect pattern as `/admin/products`'s "Sync now," calling `run_daily_digest` synchronously and redirecting back with a one-line result (`"2/2 regenerated"`).

Live-verified against the real seeded catalog and mock Mesh server: two fresh users' first dashboard visits each produced a `no_snapshot` snapshot (one real Mesh call apiece); clicking "Run digest now" as an admin regenerated both in one request, real `POST /v1/chat/completions` calls hit the mock server for each, both snapshots' `trigger_reason` flipped to `scheduled_digest`, and the redirect showed `2/2 regenerated`. Separately, starting the real app with `uvicorn` (not `TestClient`, so lifespan events actually fire) showed the scheduler registering its cron job and starting cleanly on boot, and a `SIGTERM` showed `apscheduler.scheduler: Scheduler has been shut down` / `chennai_labs.scheduler: scheduler_stopped` logged before the process exited — no orphaned background thread.

## Running tests

```bash
pytest
```

180 tests as of Stage 15: Stage 14's 173 plus 7 new — `tests/test_digest.py` covers `run_daily_digest` (skips users with no snapshot, regenerates every user who has one, updates the same row rather than inserting a new one, isolates a single user's failure from the rest of the run) and the admin "Run digest now" action (requires admin, regenerates and redirects with a result summary, a bad CSRF token regenerates nothing).

## Environment variables

See `.env.example` for the full list with comments. `SESSION_SECRET` and `SESSION_TTL_DAYS` control auth session cookies; `DATABASE_URL` points at your database (SQLite by default); `MESH_API_KEY` / `MESH_BASE_URL` / `MESH_EMBEDDING_MODEL` / `MESH_CHAT_MODEL` configure Mesh (Stage 9+) — leave `MESH_API_KEY` blank for local dev without real credentials (see "Mesh API integration" and "Recommendation narration" above for what still works without it). `DIGEST_ENABLED` / `DIGEST_HOUR_UTC` (Stage 15) control the proactive daily digest — see "Proactive digest" above. `.env` is git-ignored and must never be committed.

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
│   │   ├── time.py            # utcnow() — the one clock the whole app uses
│   │   └── scheduler.py       # Stage 15: APScheduler wiring for the daily digest job
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
│   │   ├── routes.py                    # /admin/products CRUD + manual "Sync now", gated by require_admin
│   │   ├── events_routes.py             # /admin/events debug view, gated by require_admin
│   │   └── recommendations_routes.py    # Stage 14: /admin/recommendations — Journey 3's "behavior & recommendations" view
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
│   │   ├── cache.py              # Stage 12: serves the persisted RecommendationSnapshot or regenerates + persists
│   │   └── digest.py             # Stage 15: run_daily_digest — regenerates every user's snapshot proactively
│   ├── retrieval/
│   │   ├── embeddings.py        # EmbeddingProvider interface, MeshEmbeddingProvider (default) +
│   │   │                         #   LocalHashEmbeddingProvider (kept as the test suite's stand-in)
│   │   ├── vector_store.py      # thin Chroma wrapper: upsert/delete/query/reset_collection/health_check
│   │   ├── sync.py              # dual-write mechanics: sync/remove/reconcile/detect_drift
│   │   └── query.py             # Stage 7: profile -> query text -> embedding -> ranked candidates
│   ├── mesh/
│   │   └── client.py             # the ONLY module allowed to call an AI provider —
│   │                              #   retry-aware Mesh HTTP client: embed() (Stage 9), chat() (Stage 10)
│   ├── templates/               # Jinja2 (auth/, courses/, admin/products/, admin/events/, admin/recommendations/, profile/, partials/)
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
