"""
Media-Assignment Reply-Identity Spike (2026-08-22) — TEMPORARY diagnostic-only
module, inert unless config.MEDIA_SPIKE_DIAGNOSTICS_ENABLED and
config.MEDIA_SPIKE_GROUP_NAME are both set.

Purpose: answer, with real evidence from a real disposable WhatsApp group,
whether a stable/deterministic identifier is available for (a) a media
message (photo/video) and (b) a reply that quotes one — the prerequisite for
the "reply to a talent's audition media = authoritative assignment" feature.
This is investigation code, never used for command dispatch or any write to
Talentgram data — see docs/plan "ticklish-cuddling-willow" for the full spike
design.

Two independent identifier sources are captured per message, since neither is
proven to work here yet:
  1. Full DOM attribute/outerHTML dump of the message bubble and (if present)
     its quoted-message block — a media message hasn't been tested before
     (only text quoting was, by inbound.py's _extract_reply_context, which
     found no data-id on the quoted block in real production samples).
  2. A best-effort probe of WhatsApp Web's own internal webpack module
     registry (the technique open-source tools like whatsapp-web.js use to
     reach the real Store.Msg model, which carries the actual protocol
     message id) — never attempted anywhere in this codebase before. Fully
     defensive: any failure returns a diagnostic reason string, never raises.

Every capture is written to BOTH the Railway logs (grep-able immediately) and
a dedicated Mongo collection (queryable/diffable afterward) — mirroring the
existing sender.py._capture_open_failure / _store_dom_snapshot pattern.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import config
import sender
from db import get_db

logger = logging.getLogger(__name__)

SPIKE_COLLECTION = "whatsapp_media_spike_diagnostics"

# In-memory-only dedup so an unchanged tail isn't rewritten every ~2s poll
# cycle — keyed by whatever identity the message itself offers (its
# data-testid, or a positional fallback). Spike-lifetime only; no Mongo
# backstop needed (see SEEN_COLLECTION in inbound.py for that heavier
# pattern — unwarranted here since this is throwaway diagnostic data, not
# something that must survive a worker restart).
_captured_keys: set[str] = set()

_MAX_MESSAGES_SCANNED = 20
_HTML_TRUNCATE = 20000

# Tier 1: is WhatsApp Web's internal webpack module registry reachable at
# all, and can a Store.Msg-shaped module be found among its modules? Tier 2
# (only attempted if Tier 1 succeeds) tries to actually read the currently
# rendered chat's message models straight from the Store, keyed by the
# group's own display name — bypassing DOM-index correlation entirely, since
# the Store holds real protocol ids regardless of what's on screen.
_STORE_PROBE_JS = r"""
(groupName) => {
  const result = {tier1: null, tier2: null};
  try {
    const chunkNames = Object.keys(window).filter((k) => /^webpackChunk/.test(k));
    if (!chunkNames.length) {
      result.tier1 = {ok: false, reason: "no webpackChunk* global found on window"};
      return result;
    }
    let moduleRequire = null;
    for (const chunkName of chunkNames) {
      try {
        window[chunkName].push([[Symbol()], {}, (r) => { moduleRequire = r; }]);
        if (moduleRequire) break;
      } catch (e) { /* try next chunk name */ }
    }
    if (!moduleRequire) {
      result.tier1 = {ok: false, reason: "push() did not yield a require fn", chunkNames};
      return result;
    }
    const cache = moduleRequire.c || {};
    const moduleIds = Object.keys(cache);
    const msgModuleIds = [];
    const chatModuleIds = [];
    for (const id of moduleIds) {
      let exp;
      try { exp = cache[id] && cache[id].exports; } catch (e) { continue; }
      if (!exp) continue;
      const candidates = [exp, exp.default].filter(Boolean);
      for (const c of candidates) {
        try {
          if (c.Msg && typeof c.Msg.get === "function" || (c.Msg && c.Msg.models)) msgModuleIds.push(id);
          if (c.Chat && (typeof c.Chat.get === "function" || c.Chat.models)) chatModuleIds.push(id);
        } catch (e) { /* not the shape we want */ }
      }
    }
    result.tier1 = {
      ok: true, chunkNameUsed: chunkNames[0], moduleCount: moduleIds.length,
      msgModuleIds: msgModuleIds.slice(0, 5), chatModuleIds: chatModuleIds.slice(0, 5),
    };
    if (!msgModuleIds.length && !chatModuleIds.length) return result;

    try {
      let ChatStore = null;
      for (const id of chatModuleIds) {
        const exp = cache[id].exports;
        ChatStore = exp.Chat || (exp.default && exp.default.Chat);
        if (ChatStore) break;
      }
      if (!ChatStore || !ChatStore.models) {
        result.tier2 = {ok: false, reason: "no usable Chat store found"};
        return result;
      }
      const chat = ChatStore.models.find((c) => (c.formattedTitle || c.name || "") === groupName);
      if (!chat) {
        result.tier2 = {
          ok: false, reason: "no chat matched groupName", chatCount: ChatStore.models.length,
          sampleTitles: ChatStore.models.slice(0, 10).map((c) => c.formattedTitle || c.name || null),
        };
        return result;
      }
      const msgs = (chat.msgs && chat.msgs.models) || [];
      const dumped = msgs.slice(-_MAX_MESSAGES_SCANNED_PLACEHOLDER).map((m) => {
        let quotedId = null;
        try {
          const q = typeof m.getQuotedObj === "function" ? m.getQuotedObj() : (m.quotedMsgObj || null);
          quotedId = q && q.id ? String(q.id) : (m.quotedStanzaID || null);
        } catch (e) { quotedId = "err:" + String(e && e.message || e); }
        let mediaInfo = null;
        try {
          mediaInfo = {
            type: m.type || null, isMedia: !!m.isMedia, mimetype: m.mimetype || null,
            caption: m.caption || null, filehash: m.filehash || null,
          };
        } catch (e) { mediaInfo = {error: String(e && e.message || e)}; }
        return {
          id: m.id ? String(m.id) : null,
          idKeys: m.id ? Object.keys(m.id) : null,
          t: m.t || null, from: m.from ? String(m.from) : null,
          to: m.to ? String(m.to) : null, fromMe: !!(m.id && m.id.fromMe),
          body: (m.body || "").slice(0, 120), quotedId, mediaInfo,
        };
      });
      result.tier2 = {ok: true, chatFound: true, msgCount: msgs.length, messages: dumped};
    } catch (e) {
      result.tier2 = {ok: false, error: String(e && e.message || e)};
    }
  } catch (e) {
    result.tier1 = {ok: false, error: String(e && e.message || e)};
  }
  return result;
}
""".replace("_MAX_MESSAGES_SCANNED_PLACEHOLDER", str(_MAX_MESSAGES_SCANNED))

_DOM_DUMP_JS = """
([sel, idx]) => {
  const els = document.querySelectorAll(sel);
  if (idx >= els.length) return null;
  const el = els[idx];
  const quoted = el.querySelector('[data-testid="quoted-message"]');
  return {
    messageHtml: el.outerHTML.slice(0, _TRUNC),
    quotedHtml: quoted ? quoted.outerHTML.slice(0, _TRUNC) : null,
    hasImage: !!el.querySelector('img'),
    hasVideo: !!el.querySelector('video'),
  };
}
""".replace("_TRUNC", str(_HTML_TRUNCATE))


async def ensure_spike_ttl_index() -> None:
    """TTL index created up front, from the same call site that would ever
    write to this collection — never as a follow-up (see worker.py's
    _ensure_dom_snapshot_ttl_index comment for why that discipline exists:
    a sibling collection once grew to 98% of the Atlas quota with none)."""
    try:
        db = get_db()
        await db[SPIKE_COLLECTION].create_index(
            "captured_at", expireAfterSeconds=config.MEDIA_SPIKE_TTL_SEC
        )
    except Exception:
        logger.exception("spike_diagnostics: failed to create TTL index (non-fatal)")


async def capture_group_diagnostics(page, group_name: str) -> None:
    """Best-effort: dump DOM + Store-probe diagnostics for every not-yet-
    captured message currently visible in the already-open `group_name`
    chat. Never raises — a capture failure is logged and skipped, exactly
    like every other diagnostic function in sender.py."""
    try:
        scope = await sender._resolve_scope(page)
        full_sel = f"{scope} [data-testid^='conv-msg-']"
        loc = page.locator(full_sel)
        n = await loc.count()
    except Exception:
        logger.exception("spike_diagnostics: failed to resolve messages for group=%r", group_name)
        return

    start = max(0, n - _MAX_MESSAGES_SCANNED)

    # Tier 2 Store probe is chat-scoped, not per-message — run it once per
    # capture cycle, reused across every message dumped this pass.
    store_result: Optional[dict] = None
    try:
        store_result = await page.evaluate(_STORE_PROBE_JS, group_name)
    except Exception as exc:
        store_result = {"tier1": {"ok": False, "error": f"evaluate raised: {exc}"}}

    for i in range(start, n):
        try:
            testid = await loc.nth(i).get_attribute("data-testid")
        except Exception:
            testid = None
        key = testid or f"idx:{i}"
        if key in _captured_keys:
            continue

        try:
            dom_dump = await page.evaluate(_DOM_DUMP_JS, [full_sel, i])
        except Exception as exc:
            logger.exception("spike_diagnostics: DOM dump failed group=%r index=%d", group_name, i)
            dom_dump = {"error": str(exc)}

        _captured_keys.add(key)

        doc = {
            "id": str(uuid.uuid4()),
            "group_name": group_name,
            "message_testid": testid,
            "message_index": i,
            "dom_dump": dom_dump,
            "store_probe": store_result,
            "captured_at": datetime.now(timezone.utc),
        }
        logger.info(
            "spike_diagnostics: captured group=%r testid=%r has_image=%s has_video=%s "
            "has_quoted=%s tier1_ok=%s tier2_ok=%s",
            group_name, testid,
            (dom_dump or {}).get("hasImage"), (dom_dump or {}).get("hasVideo"),
            bool((dom_dump or {}).get("quotedHtml")),
            ((store_result or {}).get("tier1") or {}).get("ok"),
            ((store_result or {}).get("tier2") or {}).get("ok"),
        )
        try:
            await get_db()[SPIKE_COLLECTION].insert_one(doc)
        except Exception:
            logger.exception("spike_diagnostics: failed to persist capture (group=%r testid=%r)", group_name, testid)
