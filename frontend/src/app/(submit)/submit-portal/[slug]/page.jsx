'use client';

import React, { useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import dynamic from 'next/dynamic';

const SubmissionPage = dynamic(() => import('@/pages/SubmissionPage'), { ssr: false });

export default function SubmitPage() {
    const [mounted, setMounted] = useState(false);
    
    useEffect(() => {
        setMounted(true);
    }, []);

    if (!mounted) return null;

    return (
        <BrowserRouter>
            <Routes>
                <Route path="/submit/:slug" element={<SubmissionPage />} />
                <Route path="/submit-portal/:slug" element={<SubmissionPage />} />
                <Route path="/:slug" element={<SubmissionPage />} />
            </Routes>
        </BrowserRouter>
    );
}
