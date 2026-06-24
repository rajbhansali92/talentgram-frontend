'use client';

import React from "react";

/**
 * P2-10: a minimal, dependency-free React error boundary.
 *
 * A render-time throw in a talent-facing flow (apply / submit) would otherwise
 * white-screen the user mid-application. This catches the error, keeps the
 * surrounding chrome, and offers a recovery action instead of a blank page.
 * Draft state is already persisted server-side + in localStorage, so a reload
 * resumes where the user left off.
 */
export default class ErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false };
    }

    static getDerivedStateFromError() {
        return { hasError: true };
    }

    componentDidCatch(error, info) {
        // Surface to the console for diagnostics; wire to a real reporter later.
        // eslint-disable-next-line no-console
        console.error("[ErrorBoundary]", error, info?.componentStack);
    }

    handleReload = () => {
        if (typeof window !== "undefined") window.location.reload();
    };

    render() {
        if (!this.state.hasError) return this.props.children;

        if (this.props.fallback) return this.props.fallback;

        return (
            <div className="min-h-dvh flex items-center justify-center bg-[#faf9f6] text-[#1a1a1a] p-6">
                <div className="max-w-md text-center bg-white rounded-2xl p-8 border border-[#eaeaea] shadow-sm">
                    <h1 className="font-display text-2xl mb-3">Something went wrong</h1>
                    <p className="text-sm text-[#6b6b6b] mb-6 leading-relaxed">
                        We hit an unexpected error. Your progress is saved — reloading
                        will pick up where you left off.
                    </p>
                    <button
                        onClick={this.handleReload}
                        className="bg-[#1a1a1a] text-white px-5 py-3 rounded-xl text-sm font-medium hover:bg-[#333] transition-colors"
                    >
                        Reload
                    </button>
                </div>
            </div>
        );
    }
}
