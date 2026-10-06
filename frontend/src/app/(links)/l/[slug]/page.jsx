import React from 'react';
import ClientView from '@/pages-components/ClientView';
import RosterView from '@/pages-components/RosterView';
import { ogImages, twitterImages } from '@/lib/ogBrand';

const BACKEND = process.env.NEXT_PUBLIC_BACKEND_URL || "https://api.talentgramagency.com";

// Tiny unauthenticated probe: which kind of link is this? Any failure falls
// back to the classic client view, so existing links can never be affected.
async function getLinkMeta(slug) {
  try {
    const res = await fetch(`${BACKEND}/api/public/links/${slug}/meta`, { next: { revalidate: 30 }, signal: AbortSignal.timeout(2500) });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function generateMetadata({ params }) {
  const { slug } = await params;
  const meta = await getLinkMeta(slug);
  const isRoster = meta?.link_type === 'roster';
  const title = isRoster ? (meta.title || 'Talentgram Roster') : 'Talentgram Agency';
  const description = isRoster ? (meta.subtitle || 'Talent Roster') : 'India - UAE';

  return {
    title,
    description,
    openGraph: {
      title,
      description,
      type: 'website',
      siteName: 'Talentgram Agency',
      images: ogImages(),
    },
    twitter: {
      card: 'summary_large_image',
      title,
      description,
      images: twitterImages(),
    },
  };
}

export default async function LinksPage({ params }) {
  const { slug } = await params;
  const meta = await getLinkMeta(slug);
  if (meta?.link_type === 'roster') return <RosterView />;
  return <ClientView />;
}
