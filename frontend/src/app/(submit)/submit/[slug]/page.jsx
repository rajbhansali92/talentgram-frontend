import React from 'react';
import SubmissionPage from '@/pages-components/SubmissionPage';
import ErrorBoundary from '@/components/ErrorBoundary';
import { ogImages, twitterImages } from '@/lib/ogBrand';

export async function generateMetadata() {
  return {
    title: 'Talentgram Agency',
    description: 'India - UAE',
    openGraph: {
      title: 'Talentgram Agency',
      description: 'India - UAE',
      type: 'website',
      siteName: 'Talentgram Agency',
      images: ogImages(),
    },
    twitter: {
      card: 'summary_large_image',
      title: 'Talentgram Agency',
      description: 'India - UAE',
      images: twitterImages(),
    },
  };
}

export default function SubmitPage() {
    return (
        <ErrorBoundary>
            <SubmissionPage />
        </ErrorBoundary>
    );
}

