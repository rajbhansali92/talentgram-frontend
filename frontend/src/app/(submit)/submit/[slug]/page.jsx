import React from 'react';
import SubmissionPage from '@/pages-components/SubmissionPage';

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
          url: `/submit/${slug}/opengraph-image`,
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
      images: [`/submit/${slug}/opengraph-image`],
    },
  };
}

export default function SubmitPage() {
    return <SubmissionPage />;
}

