import React from 'react';
import ApplicationPage from '@/pages-components/ApplicationPage';

export const metadata = {
  title: 'Talentgram Agency',
  description: 'India - UAE',
  openGraph: {
    title: 'Talentgram Agency',
    description: 'India - UAE',
    type: 'website',
    siteName: 'Talentgram Agency',
    images: [
      {
        url: '/og-image',
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
    images: ['/og-image'],
  },
};

export default function ApplyPage() {
    return <ApplicationPage />;
}

