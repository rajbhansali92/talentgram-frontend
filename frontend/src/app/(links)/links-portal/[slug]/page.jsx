'use client';

import React, { useEffect } from 'react';
import ClientView from '@/pages-components/ClientView';

export default function LinksPage() {
    useEffect(() => {
        console.log("Hydrated:", window.location.hostname);
    }, []);

    return (
        <>
            <div data-route-debug="links" style={{ display: 'none' }}>Links Loaded</div>
            <ClientView />
        </>
    );
}
