'use client';

import React, { useEffect } from 'react';
import SubmissionPage from '@/pages-components/SubmissionPage';

export default function SubmitPage() {
    useEffect(() => {
        console.log("Hydrated:", window.location.hostname);
    }, []);

    return (
        <>
            <div data-route-debug="submit" style={{ display: 'none' }}>Submit Loaded</div>
            <SubmissionPage />
        </>
    );
}
