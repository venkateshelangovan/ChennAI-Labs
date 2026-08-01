/**
 * ChennAI Labs behavioral tracker.
 *
 * Design constraints (Stage 0, Section 10 / Stage 5 brief), and how
 * this file satisfies each one:
 *
 * - Never blocks the page: every network call here is fire-and-forget.
 *   Nothing the user does waits on this file. If /api/events is down,
 *   the user notices nothing.
 * - Batching: events accumulate in an in-memory queue and flush on a
 *   size threshold (BATCH_SIZE) or a timer (FLUSH_INTERVAL_MS) —
 *   several page interactions become one network request, not one each.
 * - Immediate flush before navigation: this app is server-rendered —
 *   clicking a product card or loading a page with search/category
 *   params in the URL means the CURRENT page's in-memory queue is about
 *   to be thrown away by the browser. Anything that represents "the
 *   user is about to leave this page" calls flush() synchronously
 *   right after track(), rather than trusting the timer to catch it.
 * - sendBeacon first: delivery survives the page unloading, without an
 *   awaited response blocking anything. Falls back to a keepalive
 *   fetch (with one bounded retry) only if sendBeacon is unavailable or
 *   rejects the payload (e.g. oversized).
 * - Anonymous-safe: every event carries a client-generated session ID
 *   (persisted in localStorage + a first-party cookie the backend reads
 *   once, at login/register, to attach pre-registration behavior to the
 *   new account). No PII collected — just interaction shape.
 */

(function () {
  "use strict";

  var ENDPOINT = "/api/events";
  var SESSION_COOKIE = "cl_session_id";
  var SESSION_STORAGE_KEY = "cl_session_id";
  var BATCH_SIZE = 10;
  var FLUSH_INTERVAL_MS = 5000;
  var MIN_DWELL_MS = 500; // ignore accidental instant navigations as "time spent"

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    // Fallback for older browsers — not cryptographically strong, but this
    // is a client-correlation ID, not a security token.
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function getOrCreateSessionId() {
    try {
      var existing = window.localStorage.getItem(SESSION_STORAGE_KEY);
      if (existing) {
        ensureCookie(existing);
        return existing;
      }
    } catch (e) {
      /* localStorage unavailable (private mode etc.) — fall through to a per-load ID */
    }
    var id = uuid();
    try {
      window.localStorage.setItem(SESSION_STORAGE_KEY, id);
    } catch (e) {
      /* ignore */
    }
    ensureCookie(id);
    return id;
  }

  function ensureCookie(id) {
    // Non-httponly on purpose: only the server needs to read it (once,
    // at login/register), and it holds no secret — just a correlation ID.
    document.cookie = SESSION_COOKIE + "=" + id + "; path=/; max-age=" + 60 * 60 * 24 * 365 + "; samesite=lax";
  }

  var sessionId = getOrCreateSessionId();
  var queue = [];

  function track(eventType, options) {
    options = options || {};
    queue.push({
      client_event_id: uuid(),
      session_id: sessionId,
      event_type: eventType,
      product_id: options.productId || null,
      metadata: options.metadata || {},
    });
    if (queue.length >= BATCH_SIZE) flush();
  }

  function flush() {
    if (queue.length === 0) return;
    var batch = queue.splice(0, queue.length);
    var payload = JSON.stringify({ events: batch });
    sendBatch(payload, false);
  }

  function sendBatch(payload, isRetry) {
    if (navigator.sendBeacon) {
      var blob = new Blob([payload], { type: "application/json" });
      if (navigator.sendBeacon(ENDPOINT, blob)) return;
    }
    fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      keepalive: true,
    }).catch(function () {
      if (isRetry) return; // bounded: one retry, then drop — analytics must never nag the user
      setTimeout(function () {
        sendBatch(payload, true);
      }, 2000);
    });
  }

  function trackNow(eventType, options) {
    track(eventType, options);
    flush();
  }

  function init() {
    var body = document.body;
    var productId = body.dataset.productId ? parseInt(body.dataset.productId, 10) : null;
    var searchQuery = body.dataset.searchQuery;
    var resultCount = body.dataset.resultCount;
    var category = body.dataset.category;
    var pageSource = body.dataset.trackSource || "direct";

    if (productId) {
      trackNow("view", { productId: productId, metadata: { source: pageSource } });

      var viewStart = Date.now();
      var timeSpentSent = false;
      var sendTimeSpent = function () {
        if (timeSpentSent) return;
        var durationMs = Date.now() - viewStart;
        if (durationMs < MIN_DWELL_MS) return;
        timeSpentSent = true;
        trackNow("time_spent", { productId: productId, metadata: { duration_ms: durationMs } });
      };
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "hidden") sendTimeSpent();
      });
      window.addEventListener("pagehide", sendTimeSpent);
    }

    if (searchQuery) {
      trackNow("search", {
        metadata: { query: searchQuery, result_count: resultCount ? parseInt(resultCount, 10) : null },
      });
    }

    if (category) {
      trackNow("category_view", { metadata: { category: category } });
    }

    // Event delegation: one listener for every product-card link on the
    // page, present or added later, rather than binding N listeners.
    document.addEventListener(
      "click",
      function (event) {
        var link = event.target.closest("[data-track-click]");
        if (!link) return;
        var pid = link.dataset.productId ? parseInt(link.dataset.productId, 10) : null;
        if (!pid) return;
        var sourceEl = link.closest("[data-track-source]");
        trackNow("click", { productId: pid, metadata: { source: sourceEl ? sourceEl.dataset.trackSource : "unknown" } });
        // No preventDefault — the browser proceeds to navigate normally;
        // sendBeacon is specifically designed to survive that.
      },
      true
    );

    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") flush();
    });
    setInterval(flush, FLUSH_INTERVAL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Exposed for debugging in the console during development/demo, and
  // so future stages (e.g. a "Refresh my recommendations" button) can
  // fire a tracked event without reaching into internals.
  window.ChennAITracker = { track: track, trackNow: trackNow, flush: flush };
})();
