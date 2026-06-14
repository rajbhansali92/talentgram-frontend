'use client';

import React, { useEffect, useState } from 'react';
import dynamic from 'next/dynamic';

const PortalApp = dynamic(() => import('@/components/PortalApp'), { ssr: false });

export default function PortalPage() {
    const [mounted, setMounted] = useState(false);
    
    useEffect(() => {
        setMounted(true);
        console.log("Hydrated:", window.location.hostname);
    }, []);

    if (!mounted) return null;

    return <PortalApp />;
}
