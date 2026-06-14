'use client';

import React from 'react';
import dynamic from 'next/dynamic';

const ForgotPasswordPage = dynamic(() => import('@/pages/ForgotPasswordPage'), { ssr: false });

export default function Page() {
    return <ForgotPasswordPage />;
}
