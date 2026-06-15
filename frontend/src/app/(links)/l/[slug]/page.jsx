import React from 'react';
import ClientView from '@/pages-components/ClientView';

export async function generateMetadata({ params }) {
  const { slug } = await params;
  
  return {
    title: 'Talentgram Agency',
    description: 'India - UAE',
    openGraph: {
      title: 'Talentgram Agency',
      description: 'India - UAE',
      type: 'website',
      siteName: 'Talentgram Agency',
      images: [
        {
          url: `/l/${slug}/opengraph-image`,
          width: 1200,
          height: 630,
          alt: 'Talentgram Agency',
        },
      ],
    },
    twitter: {
      card: 'summary_large_image',
      title: 'Talentgram Agency',
      description: 'India - UAE',
      images: [`/l/${slug}/opengraph-image`],
    },
  };
}

export default function LinksPage() {
    return <ClientView />;
}

