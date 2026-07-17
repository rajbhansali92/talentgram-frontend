import React from "react";

// A small, reusable boundary for isolating ONE section of a page (a
// comments panel, a metadata block, a feedback form) from the rest of it.
// Same getDerivedStateFromError/componentDidCatch pattern already used in
// this codebase for full-page boundaries (SubmissionPage.jsx's
// SubmissionErrorBoundary, AdminLayout.jsx's LayoutErrorBoundary) — this
// one is deliberately smaller/quieter, since it's meant to sit inline
// inside an otherwise-working page rather than replace the whole screen.
export default class SectionErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false };
    }

    static getDerivedStateFromError() {
        return { hasError: true };
    }

    componentDidCatch(error, errorInfo) {
        console.error(`[SectionErrorBoundary${this.props.label ? `: ${this.props.label}` : ""}]`, error, errorInfo);
        // Optional, generic — not specific to any one consumer. If the
        // caller passes a getDiagnostics() callback (sync or async), its
        // result is logged alongside the error so a crash report carries
        // the same request-id/browser/route context a support engineer
        // would otherwise have to ask the user to reproduce.
        if (typeof this.props.getDiagnostics === "function") {
            Promise.resolve(this.props.getDiagnostics())
                .then((diagnostics) => {
                    console.error(`[SectionErrorBoundary${this.props.label ? `: ${this.props.label}` : ""}] diagnostics`, diagnostics);
                })
                .catch(() => {
                    // Diagnostics collection must never itself throw or mask the original error.
                });
        }
    }

    render() {
        if (this.state.hasError) {
            if (this.props.fallback) return this.props.fallback;
            return (
                <div className="text-xs text-black/40 py-3 px-1">
                    This section couldn't load.
                </div>
            );
        }
        return this.props.children;
    }
}
