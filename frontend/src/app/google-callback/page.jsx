'use client';

import React, { Suspense } from 'react';
import dynamic from 'next/dynamic';
import { Loader2 } from 'lucide-react';

const GoogleCallback = dynamic(() => import('@/pages/GoogleCallback'), { ssr: false });

export default function Page() {
    return (
        <Suspense fallback={
            <div className="min-h-screen bg-[#050505] text-white flex flex-col items-center justify-center px-4">
                <Loader2 className="w-8 h-8 animate-spin mx-auto text-white/80 mb-4" />
            </div>
        }>
            <GoogleCallback />
        </Suspense>
    );
}
