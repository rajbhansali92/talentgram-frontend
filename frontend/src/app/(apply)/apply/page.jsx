import React from 'react';
import ApplicationPage from '@/pages-components/ApplicationPage';
import ErrorBoundary from '@/components/ErrorBoundary';
import { ogImages, twitterImages } from '@/lib/ogBrand';

export const metadata = {
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

export default function ApplyPage() {
    return (
        <ErrorBoundary>
            <ApplicationPage />
        </ErrorBoundary>
    );
}

