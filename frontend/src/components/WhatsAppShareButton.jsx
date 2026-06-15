import React from 'react';
import { Send, Loader2 } from 'lucide-react';

export default function WhatsAppShareButton({
    onClick,
    disabled = false,
    loading = false,
    className = "",
    label = "WhatsApp",
    "data-testid": testId,
    title,
    ...props
}) {
    return (
        <button
            type="button"
            onClick={onClick}
            disabled={disabled || loading}
            data-testid={testId}
            title={title}
            className={`inline-flex items-center justify-center gap-2 border border-black/[0.06] hover:border-black/[0.16] px-4 py-2.5 rounded-md text-xs font-medium text-black/70 hover:text-black transition-colors duration-150 active:scale-[0.98] min-h-[40px] disabled:opacity-50 disabled:cursor-not-allowed disabled:pointer-events-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black/10 focus-visible:ring-offset-2 ${className}`}
            {...props}
        >
            {loading ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
                <Send className="w-3.5 h-3.5" />
            )}
            <span>{label}</span>
        </button>
    );
}
