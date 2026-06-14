'use client';

import React, { useEffect } from 'react';
import ApplicationPage from '@/pages-components/ApplicationPage';

export default function ApplyPage() {
    useEffect(() => {
        console.log("Hydrated:", window.location.hostname);
    }, []);

    return (
        <>
            <div data-route-debug="apply" style={{ display: 'none' }}>Apply Loaded</div>
            <ApplicationPage />
        </>
    );
}
