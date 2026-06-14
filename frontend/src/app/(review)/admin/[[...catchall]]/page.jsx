'use client';

import React, { useEffect, useState } from 'react';
import dynamic from 'next/dynamic';

const AdminApp = dynamic(() => import('@/components/AdminApp'), { ssr: false });

export default function AdminPage() {
    const [mounted, setMounted] = useState(false);
    
    useEffect(() => {
        setMounted(true);
        console.log("Hydrated:", window.location.hostname);
    }, []);

    if (!mounted) return null;

    return <AdminApp />;
}
