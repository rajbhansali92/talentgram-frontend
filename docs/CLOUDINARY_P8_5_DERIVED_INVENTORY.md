# Cloudinary Re-architecture — P8.5: Derived-Asset Inventory (READ-ONLY)

**Phase:** P8.5 — read-only derived-asset inventory (prerequisite for the P9 deletion architecture)
**Status:** complete — **awaiting review**
**This is NOT P9.** ZERO Cloudinary deletes · ZERO writes · ZERO uploads · ZERO regeneration ·
ZERO re-upload · ZERO migration · ZERO physical changes. Every API call is read-only.

Machine-readable summary + the persisted-legacy list: `docs/CLOUDINARY_P8_5_DERIVED_INVENTORY.json`
Full per-asset rows (8,511): `scratchpad/p85_derived_v2.json` (not committed — 7 MB).

---

## Cloudinary Admin API capability investigation (done first, no guessing)

| API | What it gives | Efficiency |
|---|---|---|
| **`cloudinary.api.transformations(max_results=500)`** | every transformation string + `used` / `named` flag. Paginated (`next_cursor`). **No per-transformation bytes/count in the list.** | **1 call** → 203 transformations, **164 used** (of which **134 are `fl_attachment:…` download strings**) |
| **`cloudinary.api.transformation(<string>, max_results=500)`** | the `derived[]` array for that transformation — **each entry has `public_id`, `resource_type`, `type`, `format`, `bytes`, `id`, `url`**. Paginated. | **1 call per used transformation** + pagination for heavy ones |
| `cloudinary.api.resource(pid, derived=True)` | the `derived[]` for ONE original (`transformation`, `bytes`, `id`, `url`) | per-original → **4,348 calls** — rejected |
| `cloudinary.api.usage()` | aggregate `derived_resources` count only | 1 call |

**Chosen method:** `transformations()` + `transformation(name)` for every `used` string.
**Total: 196 read-only Admin API calls** (1 list + ~181 transformation-detail + ~14 originals-listing
pagination) — **not** the 8,511 brute-force calls the spec warned against. Well within rate limits.
This is the only way to get per-derived-asset detail (bytes, url, id) efficiently, and it is
sufficient — no further inventory is technically necessary for P9.

---

## Classification method

For every derived asset:
1. **parent original** — recovered from the delivery URL (strip **all** leading transform segments
   + version + extension); checked against the current originals inventory
2. **parent referenced** — is the parent public_id (full or bare-leaf) in any `media[]` item
3. **derived URL persisted** — is *this exact derivative* persisted in a `media[]` field:
   `url` / `poster_url` / `thumbnail_url` / `original_url` / `compat_delivery_url` / `video_url`.
   Matched **per-variant** as `(normalised_parent, transformation_family)` — robust to Cloudinary's
   version-number and http/https differences. **P5 deliberately did not rewrite ~681 legacy stored
   derived URLs**, so this check is the safety gate.
4. **transformation status** — `current` (P4/P5 retained: canonical poster `c_fill,h_338,q_auto,w_600`,
   roster/thumb `c_fill,w_400`/`w_200` **without dpr**), `retired` (anything with `vc_auto` / `f_avif`
   / `dpr_auto` / `w_1280`+`f_mp4` / `c_limit`+`f_mp4` / `fl_awebp`), `download` (`fl_attachment:…`),
   `compat-or-retired-download` (bare `f_mp4` — ambiguous: P4 HEVC compat **or** a retired
   download transcode), `sprite`, `named`, `unknown`

`DELETE_CANDIDATE` requires **all** of: retired/download transform · parent original **exists** ·
parent is **actively referenced** · **no** persisted URL for this exact derivative · regenerable.
`derived + old`, `derived + parent deleted`, `derived + no Mongo reference` — **none** is sufficient
on its own. Anything ambiguous → `UNKNOWN_DERIVED` (never a candidate).

---

## 15-item output

### 1–2. Total

| | |
|---|---|
| **Derived assets discovered** | **8,511** (matches the `usage()` `derived_resources` count exactly) |
| **Total derived bytes** | **4.945 GB** |
| Cloudinary Admin API calls | **196** (all read-only) |
| Cloudinary / MongoDB writes | **0 / 0** |
| Derived with an **existing** parent original | **8,511 / 8,511** — no orphaned derived chains |
| Derived with a **deleted / missing** parent original | **0** |
| Derived whose parent is **actively referenced** | **5,781** |
| Derived whose parent is an **orphan original** | **2,730** |

### 3–5. Transformation families (count · bytes · status)

| Family | Count | Bytes | Status |
|---|---:|---:|---|
| `f_avif,q_auto/jpg` | 2,694 | 0.872 GB | retired — full-res AVIF from the old `IMAGE_URL` injection (the `extra_avif_mp_encoding` line) |
| `w_N,c_fill,dpr_auto,f_auto,q_auto` | 2,464 | 0.065 GB | retired — old roster/thumb **with `dpr_auto`** (P5 removed `dpr_auto`) |
| `c_fill,dpr_auto,f_auto,q_auto,w_N` | 1,468 | 0.021 GB | retired — same, param-reordered |
| `c_fill,dpr_N.N,f_avif,q_auto,w_N/` | 304 | 0.004 GB | retired — sized AVIF with fixed dpr |
| `c_fill,dpr_N.N,h_N,w_N/q_auto/jpg` | 266 | 0.005 GB | retired — old poster with fixed dpr |
| `c_fill,h_N,w_N,q_auto/f_jpg` | 207 | 0.004 GB | **current** — canonical poster (P4/P5) |
| `c_limit,h_N,w_N/q_auto,vc_auto/f_mpN` | 207 | 0.822 GB | retired — old `stream_video_url` 3-segment 720p chain |
| `fl_attachment:* (download)` | 148 | 0.804 GB | download — the P5 RULE #7 target (146 distinct filename strings) |
| `f_avif,q_auto/png` | 135 | 0.009 GB | retired |
| `c_fill,dpr_auto,h_N,w_N/q_auto/jpg` | 128 | 0.002 GB | retired — old poster with dpr |
| `c_limit,h_N,w_N/q_auto,vc_auto` | 125 | 0.326 GB | retired — old video transcode |
| `f_mpN` (bare `f_mp4`) | **113** | **1.513 GB** | **compat-or-retired-download — ambiguous** |
| `c_limit,h_N,w_N/q_auto,vc_auto/f_mpN/` | 70 | 0.242 GB | retired |
| `c_limit,h_N,w_N,f_mpN/q_auto,vc_auto` | 44 | 0.137 GB | retired |
| `c_fill,w_N/` | 27 | 0.022 GB | current — small thumb |
| `f_auto,q_auto` | 24 | 0.004 GB | current — HEIC / render |
| `w_N,h_N,c_limit,q_auto,vc_auto,f_mpN` | 17 | 0.064 GB | retired — the old **eager 720p** (`w_1280,h_720,c_limit,q_auto,vc_auto,f_mp4`) |
| `f_avif,q_auto/{webp,heic}` | 39 | 0.004 GB | retired |
| `q_auto,vc_auto` | 2 | 0.004 GB | retired |
| `fl_sprite/{jpg,vtt}` | 2 | 0.001 GB | sprite (video preview sprite sheets) |
| `t_media_lib_thumb` | *(named, folded into thumb families)* | | named |
| `c_limit,w_N/jpg` | 1 | 0.0002 GB | retired image preset |

### 6. Active / legacy / referenced / unknown breakdown

| Classification | Count | Bytes | Proposed handling |
|---|---:|---:|---|
| **`DELETE_CANDIDATE`** | **5,005** | **2.590 GB** | retired transform · live+referenced parent · no persisted URL · regenerable — **candidate SUBJECT TO P9's fresh per-asset re-check**, not a delete instruction |
| **`LEGACY_DERIVED`** | **2,654** | **0.658 GB** | retired transform, but the parent original is itself an **orphan** (unreferenced) — review **alongside the parent** (do not delete the derivative independently) |
| **`UNKNOWN_DERIVED`** | **116** | **1.513 GB** | 113 bare `f_mp4` (P4-compat **or** retired download — needs per-video codec disambiguation), 2 sprite sheets, 1 obscure image preset — **never auto-deletable** |
| **`PROTECTED_HISTORICAL_DERIVED`** | **468** | **0.154 GB** | this exact derivative's URL is persisted in a `media[]` field — **must not be deleted in P8.5 or P9 without a migration/replacement strategy** |
| **`ACTIVE_DERIVED`** | **268** | **0.030 GB** | current retained transform (canonical poster / roster / thumb) — KEEP |
| **`REFERENCED_DERIVED`** | folded into ACTIVE_DERIVED / PROTECTED_HISTORICAL_DERIVED | | |

### 7. Persisted legacy URLs — `PROTECTED_HISTORICAL_DERIVED` (468 / 0.154 GB)

| Persisted transform family | Count | What it is |
|---|---:|---|
| `c_fill,dpr_auto,f_auto,q_auto,w_N` | 370 | old talent-thumbnail URLs stored in `media.thumbnail_url` (pre-P5, with `dpr_auto`) |
| `c_limit,h_N,w_N/q_auto,vc_auto` (+`f_mpN` / `mpN` variants) | 41 | old 720p video-delivery URLs stored in `media.url` — the ~52 legacy videos P5's smoke test flagged |
| `c_fill,dpr_auto,h_N,w_N/q_auto/jpg` | 40 | old poster URLs (`dpr_auto`) stored in `media.poster_url` |
| `w_N,h_N,c_limit,q_auto,vc_auto,f_mpN` | 8 | the old **eager 720p** (`w_1280,h_720,…,f_mp4`) — `admin_media/` reference videos |
| `w_N,h_N,c_fill,q_auto,f_jpg` | 8 | old poster ordering, in `poster_url` |
| `/mpN` (bare) | 3 | intro-video download URLs |

98 are **videos**, 370 are **images**. **P9 must treat these as immovable** unless a persisted-URL
migration is done first (rewrite the stored `media.url` / `poster_url` / `thumbnail_url` to the
canonical asset, then the derivative is free) — which is itself a separate, out-of-scope change.

### 8. Derived assets with deleted parents

**0.** Every one of the 8,511 derived assets has a parent original that still exists in Cloudinary.
There are no orphaned derived chains.

### 9. Derived assets with active parents

**5,781** derived assets have a parent original that is actively referenced in MongoDB.
The remaining **2,730** have a parent that is itself an orphan original (a P8
`PROTECTED_UNKNOWN`) — their derivatives are `LEGACY_DERIVED`, reviewed with the parent.

### 10. Potentially safe candidates

**`DELETE_CANDIDATE` — 5,005 assets / 2.590 GB.** Largest families:

| Family | Count | Bytes |
|---|---:|---:|
| `f_avif,q_auto/jpg` | 2,080 | 0.769 GB — full-res AVIF, render-time only, never stored, P5 stopped creating them |
| `w_N,c_fill,dpr_auto,f_auto,q_auto` | 1,968 | 0.054 GB — old thumb with `dpr_auto` |
| `c_limit,h_N,w_N/q_auto,vc_auto/f_mpN` | 179 | 0.701 GB — old 720p video chains, unreferenced |
| `fl_attachment:*` | 146 | 0.796 GB — download derivatives, unreferenced |
| `c_fill,dpr_N.N,f_avif,q_auto,w_N` + `c_fill,dpr_N.N,h_N,w_N/q_auto/jpg` + AVIF png/webp/heic | ~470 | ~0.02 GB |

**"Potentially safe" ≠ safe.** Each is regenerable on demand; a rare still-cached client bookmark
could trigger one regeneration (1 transformation) on first re-request. P9's per-asset re-check +
small batches + anomaly-stop is what makes deletion safe — this list is its input, not its authority.

### 11. Protected candidates

**`PROTECTED_HISTORICAL_DERIVED` (468) + `ACTIVE_DERIVED` (268) = 736 / 0.18 GB.**
Persisted-URL derivatives + current retained transforms. P9 must not delete any of these.

### 12. Unknown candidates

**`UNKNOWN_DERIVED` (116) + `LEGACY_DERIVED` (2,654) = 2,770 / 2.17 GB.**
- 113 bare `f_mp4` (**1.51 GB — the single largest byte bucket**): cannot tell from the transform
  string alone whether each is a P4 HEVC-compat delivery (keep) or a retired pre-P5 download
  transcode (candidate). Disambiguation needs the source video's codec per asset — a P9 step.
- 2,654 `LEGACY_DERIVED`: retired transforms whose parent original is an orphan — resolve with the
  parent, not independently.

### 13. API calls performed

**196**, all read-only:
`transformations()` ×1 · `transformation(name)` ×~181 (with pagination) · `resources()` ×~14
(originals listing, for the parent-exists check). Rate-limit headers were not surfaced by the SDK;
196 calls is comfortably within Cloudinary's Admin API limits and well below the 8,511-call
brute-force the spec forbade.

### 14. Confirmation of ZERO writes

**ZERO Cloudinary writes** (no `uploader.*`, no `destroy`, no `delete_resources*`, no eager
transformation, no transformed-URL request — only Admin API `transformations` / `transformation` /
`resources` reads). **ZERO MongoDB writes** (the reference index is `find`-only).

### 15. Whether further inventory is technically possible

**Yes, and it has been done.** `transformation(name, derived=True)` gives every field P9 needs
(public_id, parent, transformation, format, bytes, url, id) at ~196 calls. The only per-asset
datum *not* obtainable without a heavier scan is each `f_mp4` derivative's **source-video codec**
(needed to split the 113 ambiguous ones) — that is a `resource(pid)` per parent, ~113 calls, and
belongs in P9's disambiguation step, not here.

---

## Reconciliation with P8

```
Cloudinary usage-API total storage ...... 19.63 GB
  = P8 originals (4,348 / 14.69 GB)
  + P8.5 derived  (8,511 / 4.94 GB)          [8,511 matches usage().derived_resources exactly]
  ≈ 19.63 GB  ✓

Derived 4.94 GB
  = DELETE_CANDIDATE      5,005 / 2.59 GB
  + UNKNOWN (f_mp4 etc.)    116 / 1.51 GB
  + LEGACY_DERIVED        2,654 / 0.66 GB
  + PROTECTED_HISTORICAL    468 / 0.15 GB
  + ACTIVE_DERIVED          268 / 0.03 GB
```

## What this means for P9 (for your review — NOT built)

* **Reclaimable derived (needs P9 approval + re-check):** up to **~2.59 GB** (`DELETE_CANDIDATE`),
  plus a portion of the **1.51 GB** `f_mp4` bucket once each is proven to be a retired download
  transcode rather than a P4 compat delivery.
* **Immovable:** the **468 persisted-legacy derivatives** (0.15 GB) unless a stored-URL migration
  is done first.
* **Defer:** the **2,654 `LEGACY_DERIVED`** — resolve with their orphan parent originals in the
  same P9 batch, not separately.
* Combined with P8's originals picture (0 DELETE_ELIGIBLE today, deletable only after
  soft-delete + retention), the **total near-term reclaimable is derived-only**, roughly
  **2.5–4 GB**, and still requires P9's controlled, per-asset, approved deletion.

---

## Absolute safety confirmation

ZERO production Cloudinary deletes · ZERO bulk cleanup · ZERO regeneration · ZERO re-upload ·
ZERO migration · ZERO physical changes. Read-only Admin API only. No transformation was generated
to inspect a transformation. The `MEDIA_LIFECYCLE_PHYSICAL_DELETE` flag remains OFF and untouched.

**P9 is NOT built.** It needs your review of this inventory + a complete understanding of
originals + derived + references + ownership + retention + legacy-URL dependencies before any
physical-deletion architecture is designed.
