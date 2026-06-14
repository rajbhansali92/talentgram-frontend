'use client';

import React from 'react';
import dynamic from 'next/dynamic';

const GoogleCallback = dynamic(() => import('@/pages/GoogleCallback'), { ssr: false });

export default function Page() {
    return <GoogleCallback />;
}
