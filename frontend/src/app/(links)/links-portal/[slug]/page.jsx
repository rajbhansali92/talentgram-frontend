'use client';

import React, { useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import dynamic from 'next/dynamic';

const ClientView = dynamic(() => import('@/pages/ClientView'), { ssr: false });

export default function LinksPage() {
    const [mounted, setMounted] = useState(false);
    
    useEffect(() => {
        setMounted(true);
    }, []);

    if (!mounted) return null;

    return (
        <BrowserRouter>
            <Routes>
                <Route path="/l/:slug" element={<ClientView />} />
                <Route path="/links-portal/:slug" element={<ClientView />} />
                <Route path="/:slug" element={<ClientView />} />
            </Routes>
        </BrowserRouter>
    );
}
