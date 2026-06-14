'use client';

import React from 'react';
import dynamic from 'next/dynamic';

const ApplicationPage = dynamic(() => import('@/pages/ApplicationPage'), { ssr: false });

export default function ApplyPage() {
    return <ApplicationPage />;
}
