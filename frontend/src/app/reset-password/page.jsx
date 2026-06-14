'use client';

import React from 'react';
import dynamic from 'next/dynamic';

const ResetPasswordPage = dynamic(() => import('@/pages/ResetPasswordPage'), { ssr: false });

export default function Page() {
    return <ResetPasswordPage />;
}
