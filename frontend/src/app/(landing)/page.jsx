'use client';

import React, { useEffect } from 'react';
import Landing from '@/pages-components/Landing';

export default function LandingPage() {
    useEffect(() => {
        console.log("Hydrated:", window.location.hostname);
    }, []);

    return <Landing />;
}
