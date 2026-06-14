'use client';

import React, { useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import dynamic from 'next/dynamic';

const Landing = dynamic(() => import('@/pages/Landing'), { ssr: false });

export default function LandingPage() {
    const [mounted, setMounted] = useState(false);
    
    useEffect(() => {
        setMounted(true);
    }, []);

    if (!mounted) return null;

    return (
        <BrowserRouter>
            <Routes>
                <Route path="*" element={<Landing />} />
            </Routes>
        </BrowserRouter>
    );
}
