# P9 Production Dry-Run — Canary Approval Report

**READ-ONLY.** Nothing deleted. No batch. No approval. No flag change.
`MEDIA_LIFECYCLE_PHYSICAL_DELETE` = **OFF** (unset) in production.

Source: P8.5 derived inventory (`p85_derived_v2.json`) + live `cloudinary.api.resource()` reads.
Machine-readable: `docs/CLOUDINARY_P9_DRYRUN.json`.

---

## Overall

| | count | bytes |
|---|---:|---:|
| **P8.5 DELETE_CANDIDATE (input)** | **5,005** | **2.5896 GB** |
| Live-revalidated in this dry-run | 15 (smallest-AVIF canary probe) | — |
| — verdict `PASS` | **15 / 15** | — |
| — blocked / stale / unknown | 0 | 0 |
| Full per-asset live revalidation of all 5,005 | **deferred to execute-time** (P9's design re-validates *immediately before each delete*); ~2,700 `resource()` calls, rate-limited — not run in one shot for the report |

Verdict totals below use the P8.5 classification (≈1 h old, from a full transformation
enumeration + the live MongoDB reference index). Every asset is re-proven live in Layer 1
before it is ever deleted.

| Verdict (P8.5-derived) | count | bytes |
|---|---:|---:|
| would-be `PASS` candidates (retired transform · live+referenced parent · no persisted URL) | 5,005 | 2.5896 GB |
| `PROTECTED` / `STALE` / `RETENTION_BLOCKED` / etc. | 0 in the DELETE_CANDIDATE set (those are separate P8.5 classes) | — |

The 15 assets actually live-revalidated (Layer 1 against Cloudinary + MongoDB) all returned
`PASS` with an exact byte-for-byte identity match (`bytes` == Cloudinary `derived.bytes`).

---

## DELETE_CANDIDATE by transformation family

| Transformation family | Count | Bytes | Notes |
|---|---:|---:|---|
| `f_avif,q_auto/jpg` | 2,080 | 768.8 MB | **retired full-res AVIF** (old `IMAGE_URL` injection) — render-time only, never stored — **canary source** |
| `w_N,c_fill,dpr_auto,f_auto,q_auto` | 1,968 | 54.4 MB | old roster/thumb **with `dpr_auto`** (P5 removed dpr) |
| `c_fill,dpr_N.N,f_avif,q_auto,w_N` | 214 | 3.3 MB | sized AVIF with fixed dpr |
| `c_fill,dpr_N.N,h_N,w_N/q_auto/jpg` | 208 | 4.3 MB | old poster with fixed dpr |
| `c_limit,h_N,w_N/q_auto,vc_auto/f_mpN` | 179 | 700.8 MB | old `stream_video_url` 720p chain |
| `fl_attachment:*` | 146 | 795.7 MB | download derivatives (P5 RULE #7 target) |
| `f_avif,q_auto/png` | 68 | 6.9 MB | retired AVIF — **also canary source** |
| `c_limit,h_N,w_N/q_auto,vc_auto/f_mpN/` | 44 | 176.4 MB | old 720p chain variant |
| `c_fill,dpr_auto,f_auto,q_auto,w_N` | 42 | 1.0 MB | old thumb w/ dpr (reordered) |
| `f_avif,q_auto/webp` | 26 | 2.3 MB | retired AVIF |
| `c_limit,h_N,w_N,f_mpN/q_auto,vc_auto` | 22 | 69.3 MB | old 720p chain variant |
| `f_avif,q_auto/heic` · `q_auto,vc_auto` · `c_fill,dpr_auto,h_N,w_N/q_auto/jpg` · `c_limit,h_N,w_N/q_auto,vc_auto/mpN` | 3 · 2 · 2 · 1 | ~5 MB | small retired |

**The proposed canary comes ONLY from `f_avif,q_auto/jpg` + `f_avif,q_auto/png`** — retired,
render-time-only, regenerable, never persisted. It contains **no** `f_mp4`, `fl_attachment`,
`vc_auto`, `fl_sprite`, 720p, poster, or `dpr` thumbnail derivative.

---

## Proposed 10-asset Canary

All: P8.5 `DELETE_CANDIDATE` · P9 live verdict **`PASS`** · `owner_type=talent` (P3, folder
never consulted) · parent original **exists** and is **actively referenced** · **no persisted
URL** for the derivative · Cloudinary identity matches (`bytes` column == live
`derived.bytes`) · deleting the derivative cannot affect the parent (it is a separate derived
resource id).

| # | derived_id | parent_public_id | talent_id | owner_type | fmt | bytes | transformation | refs | persisted URL | parent_exists | parent_active | P9 verdict |
|--:|---|---|---|---|---|--:|---|--:|---|---|---|---|
| 1 | `4e0b91628ad040df8f03b35be20d40a9` | `talentgram/applications/29db6f7c-1027-4ea7-bbdc-c5c2c8105659/9301e771-8eb0-4b46-b5da-0bfbd4ba41f7` | `011d6e3e-ac55-43bc-9033-cd263b945cd9` | talent | avif | 91 | `f_avif,q_auto/jpg` | 1 | NO | YES | YES | PASS |
| 2 | `90e2c0d506f9c3204805c540a2b906d7` | `talentgram/applications/29db6f7c-1027-4ea7-bbdc-c5c2c8105659/093c3967-d713-4b22-b72e-e90314d5cb6d` | `011d6e3e-ac55-43bc-9033-cd263b945cd9` | talent | avif | 91 | `f_avif,q_auto/jpg` | 1 | NO | YES | YES | PASS |
| 3 | `dcb3a4b118d6ea2d24dc152bfd9bd743` | `talentgram/applications/29db6f7c-1027-4ea7-bbdc-c5c2c8105659/f75d7d72-6da0-4103-a1a2-61d1aa7f1887` | `011d6e3e-ac55-43bc-9033-cd263b945cd9` | talent | avif | 91 | `f_avif,q_auto/jpg` | 1 | NO | YES | YES | PASS |
| 4 | `36a77e5f8dd27f5e628133fb56afff65` | `talentgram/applications/17c84979-3a39-47c0-aeb5-27e6dd4b71fc/e0b83309-2bfe-417a-bf00-1fd40afb8094` | `7c65b4f7-0156-4df9-939e-6a7feae48487` | talent | avif | 95 | `f_avif,q_auto/png` | 1 | NO | YES | YES | PASS |
| 5 | `73d1d947e0df9b419a43b492a9a265a1` | `talentgram/submissions/f6feb6ba-dc16-459b-8a15-b8d5c8b50d58/087d2995-adab-4cf9-a163-508ee0a7770e` | `b5766027-4318-4548-b1aa-e77f78cb1c67` | talent | avif | 6294 | `f_avif,q_auto/jpg` | 1 | NO | YES | YES | PASS |
| 6 | `87b0e6770c6e9815b756624c8f19a185` | `talentgram/talents/382d8b6c-79c3-4045-a27c-d0e5b9292eb5_riti-walia/profile_images/0157ceab-1a0c-4654-b98a-5f4150191fe8` | `382d8b6c-79c3-4045-a27c-d0e5b9292eb5` | talent | avif | 10449 | `f_avif,q_auto/jpg` | 1 | NO | YES | YES | PASS |
| 7 | `439200ab322c41e518aee866828ff021` | `talentgram/talents/8f5e31f8-1e15-4f65-9ef6-9f4ac345a699_ria-jaitley/profile_images/f51b8165-6c70-4dc6-a157-8c52850fdccb` | `8f5e31f8-1e15-4f65-9ef6-9f4ac345a699` | talent | avif | 10514 | `f_avif,q_auto/jpg` | 1 | NO | YES | YES | PASS |
| 8 | `b1a29f34d6901420dae9cfa783e6f286` | `talentgram/talents/9227e123-3717-4df6-a918-33273b3e359d_tejshri-subodh/profile_images/bed18bd3-d913-4dee-b1ec-72fd6b90a7f2` | `9227e123-3717-4df6-a918-33273b3e359d` | talent | avif | 10621 | `f_avif,q_auto/jpg` | 1 | NO | YES | YES | PASS |
| 9 | `ee24054723beeb7b14da77221c9ccdd8` | `talentgram/applications/f022cbcf-aba3-4dc7-9b65-6f5ebee55e30/9509616f-4f46-429f-ba89-88ac8bba5cf9` | `689b82e6-9323-42b8-af8d-d47edfc29973` | talent | avif | 11062 | `f_avif,q_auto/jpg` | 2 | NO | YES | YES | PASS |
| 10 | `20ff5acda320d634c2d66ad523e63ddd` | `talentgram/talents/382d8b6c-79c3-4045-a27c-d0e5b9292eb5_riti-walia/profile_images/186b6a18-3e47-46d2-b44e-8f340664eb76` | `382d8b6c-79c3-4045-a27c-d0e5b9292eb5` | talent | avif | 11243 | `f_avif,q_auto/jpg` | 1 | NO | YES | YES | PASS |

Total canary bytes: **60,742 bytes (≈ 59 KB)** across 10 derived assets. `resource_type` = image
for all. `created_at` is not exposed by the transformation-listing API; it is available per
asset from `resource(derived=True)` and is re-checked at execute time.

---

## Approval identity

```
manifest_id     : pm_1c850b20c08d8551e914
candidate_hash  : b9abc586ff6fb894f989d045d98e846941fc2681570737e90028be052396db94
generated_at    : 2026-08-30T17:08:55Z
source_manifest : p8.5-derived-inventory

exact 10 candidate derived_ids (the ONLY assets this approval may cover):
  20ff5acda320d634c2d66ad523e63ddd
  36a77e5f8dd27f5e628133fb56afff65
  439200ab322c41e518aee866828ff021
  4e0b91628ad040df8f03b35be20d40a9
  73d1d947e0df9b419a43b492a9a265a1
  87b0e6770c6e9815b756624c8f19a185
  90e2c0d506f9c3204805c540a2b906d7
  b1a29f34d6901420dae9cfa783e6f286
  dcb3a4b118d6ea2d24dc152bfd9bd743
  ee24054723beeb7b14da77221c9ccdd8
```

`candidate_hash` is the order-independent SHA-256 of exactly these 10 ids. An approval is tied
to `(manifest_id, candidate_hash, these 10 ids)` and is consumed by one batch — it cannot be
reused for any other set.

*(This manifest was built with `persist=false` → nothing was written. To act on it, the
approval step calls `GET /purge/manifest?persist=true`, which re-runs Layer 1 and stores an
immutable manifest doc; `passed_candidate_hash` is deterministic over the passed id set, so it
matches the hash above as long as production state is unchanged.)*

---

## Safety verification for the 10

| Assertion | Status |
|---|---|
| parent originals survive a derived-asset delete | ✅ each is a distinct Cloudinary *derived* resource id; `delete_derived_resources([id])` removes only that derivative; Layer 3 verifies the parent still exists post-delete |
| no MongoDB media record is referenced by the derivative | ✅ per-variant persisted-URL check + full reference scan → `persisted_url_reference = NO`, ref is only on the *parent* (`refs` 1–2) |
| no client-review-link dependency | ✅ (checked in Layer 1's reference scan) |
| no application/submission/lineage/ledger dependency on the derivative | ✅ |
| ownership known and correct | ✅ all `owner_type = talent` from the P3 `media[i].ownership` sub-document (folder path never consulted) |
| retention satisfied | ✅ n/a — talent global media, not project audition |
| Cloudinary identity matches the manifest | ✅ `bytes` == live `derived.bytes` for all 10; transformation matches |
| transformation is retired | ✅ `f_avif,q_auto/*` — P5 stopped generating these; regenerable on demand |
| the canary contains DERIVED ASSETS ONLY | ✅ every row is a derived resource id, not a `public_id` upload |

---

## Unknown / deferred buckets (NOT in the canary, NOT deletable)

### #9 — the 113 `f_mp4` derivatives (1.5133 GB)

Disambiguated by fetching each parent video's codec (`cloudinary.api.resource(pid,
resource_type="video")` → `video.codec`) — **113 read-only calls**:

| Category | Count | Bytes |
|---|---:|---:|
| A — P4 HEVC / non-web-codec compat derivative (KEEP) | **0** | — |
| B — retired download transcode (could *eventually* be eligible, after review) | **0** | — |
| C — persisted historical compat (PROTECT) | **0** | — |
| **D — genuinely unknown (PROTECT)** | **113** | **1513.3 MB** |

None of the 113 parents are HEVC/non-web (so none are P4 compat), none have a persisted
`f_mp4` URL, **and none of the parents are actively referenced in MongoDB** (they are
derivatives of orphan-ish originals). With no way to prove B without guessing, all 113 stay
**`UNKNOWN_DERIVED` → PROTECT**. Layer 1 blocks them at the classification gate.

### #10 — the 2,654 `LEGACY_DERIVED` (0.6576 GB)

| | |
|---|---|
| parent original exists | **2,654 / 2,654** |
| parent original **actively referenced in MongoDB** | **0 / 2,654** — this is *why* they are `LEGACY_DERIVED` and not `DELETE_CANDIDATE` |
| derivatives with a persisted URL | **0** |

Their parent originals are themselves orphans (P8 `PROTECTED_UNKNOWN`). **Deferred**: the
parent must be resolved first (via the P8 originals path — which currently has **0
DELETE_ELIGIBLE**); the derivative is never deleted independently. Largest families:
`c_fill,dpr_auto,f_auto,q_auto,w_N` (1,056), `f_avif,q_auto/jpg` (614),
`w_N,c_fill,dpr_auto,f_auto,q_auto` (496).

### #11 — the 468 `PROTECTED_HISTORICAL_DERIVED` (0.1537 GB)

Every one has its **exact derived URL stored in a `media[]` field** (`media.url` /
`poster_url` / `thumbnail_url`). P9 Layer 1 blocks these at **both** the `_NEVER`
classification gate **and** the repo-wide reference check (`REFERENCE_BLOCKED`). **No P9 path
can delete them.** Deletion would require first rewriting each stored URL to the canonical
asset (a separate migration), then re-running P8.5 → P9. Families: old thumbnails
`c_fill,dpr_auto,f_auto,q_auto,w_N` (370), old posters (48), legacy 720p video URLs (49).

---

## Write verification

| | |
|---|---|
| Cloudinary writes / deletes / transformations generated / uploads | **0 / 0 / 0 / 0** |
| MongoDB writes / deletes / updates | **0 / 0 / 0** |
| Purge audit-ledger writes | **0** |
| Live Cloudinary `resource()` **read** calls (canary probe + f_mp4 codecs) | 15 + 113 + earlier 400-sample = **528** |

**Disclosure:** one `purge_manifests` analysis-artifact doc (`pm_7c0dc1b2008103a2a4c3`) was
inserted into production Mongo by an **earlier dry-run iteration**, before `persist=false` was
added to `build_purge_manifest`. It is an in-memory manifest snapshot — it references no
media, mutates nothing, and is not an approval or a batch. It is left in place per your
"do not delete any MongoDB document" rule. The corrected dry-run (this one, `persist=false`)
wrote **nothing**. Every subsequent path is now in-memory unless `persist=true` is explicitly
passed.

---

## Production flag

```
MEDIA_LIFECYCLE_PHYSICAL_DELETE = (unset)  →  _physical_delete_enabled() = False  →  OFF
db.purge_approvals = 0   db.purge_batches = 0   db.purge_audit_log = 0
```

No endpoint can set the flag. Real deletion additionally requires a matching, unconsumed,
hash-verified approval — the flag alone is not authorization.

---

## STOP

Nothing has been deleted. No batch or approval was created. The canary has not run. Physical
deletion remains disabled.

**Awaiting your explicit approval of the exact 10 `derived_id`s above** (and the
`candidate_hash`). Only then will the sequence be: enable the flag for that one scoped
operation → `POST /purge/approve` (these 10) → `POST /purge/batch` (canary, size 10) →
`POST /purge/execute {dry_run:false}` → the 10-point canary verification → **STOP again** for
your review before any batch 2.
