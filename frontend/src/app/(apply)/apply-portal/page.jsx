'use client';

import React, { useEffect } from 'react';
import ApplicationPage from '@/pages-components/ApplicationPage';

export default function ApplyPage() {
    useEffect(() => {
        console.log("Hydrated:", window.location.hostname);
    }, []);

    return <ApplicationPage />;
}
