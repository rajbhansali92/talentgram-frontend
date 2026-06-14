'use client';

import React from 'react';
import dynamic from 'next/dynamic';

const SignupPage = dynamic(() => import('@/pages/SignupPage'), { ssr: false });

export default function Page() {
    return <SignupPage />;
}
