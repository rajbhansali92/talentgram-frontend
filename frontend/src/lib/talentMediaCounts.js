/**
 * The two small counters on a Global Talent list row.
 *
 *   video — 1 when the talent has an Introduction Video, otherwise 0. The Introduction Video is
 *           the media item with category "video" (the same field the Talent Preview plays); a
 *           talent has a single intro-video slot, so this is never more than 1.
 *   image — every other media item. (The stored `media_count` is the TOTAL and includes the
 *           video, so it must not be shown as the image count.)
 *
 * The list API computes both server-side (`video_count`, `image_count`) because it never sends
 * the media array itself. When those fields are absent (e.g. a hydrated talent that does carry
 * `media[]`) the same rule is derived from the array, and as a last resort the old behaviour.
 */
export function talentMediaCounts(t) {
    const list = Array.isArray(t?.media) ? t.media : [];
    const isIntro = (m) => m?.category === "video";
    const video = Number.isFinite(t?.video_count)
        ? t.video_count
        : Math.min(1, list.filter(isIntro).length);
    const image = Number.isFinite(t?.image_count)
        ? t.image_count
        : list.length
          ? list.filter((m) => !isIntro(m)).length
          : (t?.media_count ?? 0);
    return { imageCount: image, videoCount: video };
}
