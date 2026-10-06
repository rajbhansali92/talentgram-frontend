// Single source of the brand image used for external link previews (WhatsApp, Meta,
// LinkedIn, X, generic Open Graph consumers). Every public page's og:image and
// twitter:image comes from here, so the logo can never differ between link families.
//
// The file is a STATIC asset under /public on purpose: the middleware passes any path
// containing a "." straight through on every host (apply./submit./links./review./www),
// so the same URL resolves everywhere. Titles and descriptions stay page-specific.
//
// Cache-busting: bump OG_IMAGE_VERSION only when the image file itself changes.
export const OG_IMAGE_PATH = "/brand/talentgram-og.png";
export const OG_IMAGE_VERSION = "1";
export const OG_IMAGE_URL = `${OG_IMAGE_PATH}?v=${OG_IMAGE_VERSION}`;
export const OG_IMAGE_WIDTH = 1200;
export const OG_IMAGE_HEIGHT = 630;

// Relative URLs resolve against each page's metadataBase (set per host in the root layout).
export function ogImages(alt = "Talentgram Agency") {
  return [{ url: OG_IMAGE_URL, width: OG_IMAGE_WIDTH, height: OG_IMAGE_HEIGHT, alt }];
}

export function twitterImages() {
  return [OG_IMAGE_URL];
}
