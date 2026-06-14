'use client';

import React, { useEffect } from 'react';
import ClientView from '@/pages-components/ClientView';

export default function LinksPage() {
    useEffect(() => {
        console.log("Hydrated:", window.location.hostname);
    }, []);

    return <ClientView />;
}
