import React, { useState } from "react";
import { AlertTriangle, X, Loader2, Trash2 } from "lucide-react";

/**
 * Branded confirm-delete modal.
 *
 *  <ConfirmDeleteDialog
 *    open={confirmOpen}
 *    title="Delete this link?"
 *    description="This will also delete views, actions and download history."
 *    confirmLabel="Delete link"
 *    typeToConfirm="DELETE"           // optional — forces the admin to type
 *    onCancel={() => setConfirmOpen(false)}
 *    onConfirm={async () => { ... }}
 *  />
 *
 * Uses shadcn tokens so it works in light + dark themes. The `onConfirm`
 * callback receives no args; if it throws, we keep the dialog open.
 */
export default function ConfirmDeleteDialog({
    open,
    title = "Are you sure?",
    description,
    confirmLabel = "Delete",
    typeToConfirm = null,
    onCancel,
    onConfirm,
    testid = "confirm-delete-dialog",
}) {
    const [typed, setTyped] = useState("");
    const [busy, setBusy] = useState(false);

    if (!open) return null;

    const enabled = !typeToConfirm || typed === typeToConfirm;

    const run = async () => {
        if (!enabled || busy) return;
        setBusy(true);
        try {
            await onConfirm();
            setTyped("");
        } catch {
            // onConfirm handler should have surfaced the error via toast
        } finally {
            setBusy(false);
        }
    };

    return (
        <div
            className="fixed inset-0 z-[60] bg-black/70 backdrop-blur flex items-center justify-center p-4"
            data-testid={testid}
        >
            <div className="w-full max-w-md border border-border bg-background p-6 md:p-7 rounded-sm relative">
                <button
                    type="button"
                    onClick={onCancel}
                    className="absolute top-4 right-4 text-muted-foreground hover:text-foreground"
                    data-testid={`${testid}-close`}
                >
                    <X className="w-4 h-4" />
                </button>
                <div className="flex gap-4 mb-5">
                    <div className="shrink-0 w-10 h-10 rounded-full bg-[var(--tg-danger)]/10 border border-[var(--tg-danger)]/25 flex items-center justify-center">
                        <AlertTriangle className="w-4 h-4 text-[var(--tg-danger)]" />
                    </div>
                    <div>
                        <p className="eyebrow mb-1">Danger zone</p>
                        <h3 className="font-display text-xl leading-tight">
                            {title}
                        </h3>
                    </div>
                </div>
                {description && (
                    <p className="text-sm text-muted-foreground mb-5 leading-relaxed">
                        {description}
                    </p>
                )}
                {typeToConfirm && (
                    <label className="block mb-5">
                        <span className="text-[11px] tracking-widest uppercase text-muted-foreground">
                            Type{" "}
                            <span className="tg-mono text-foreground">
                                {typeToConfirm}
                            </span>{" "}
                            to confirm
                        </span>
                        <input
                            value={typed}
                            onChange={(e) => setTyped(e.target.value)}
                            autoFocus
                            data-testid={`${testid}-input`}
                            className="mt-2 w-full bg-transparent border-b border-border focus:border-foreground outline-none py-2 text-sm tg-mono tracking-wider"
                        />
                    </label>
                )}
                <div className="flex gap-2">
                    <button
                        type="button"
                        onClick={onCancel}
                        disabled={busy}
                        className="flex-1 border border-border hover:border-foreground/60 py-2.5 rounded-sm text-sm"
                        data-testid={`${testid}-cancel`}
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={run}
                        disabled={!enabled || busy}
                        className="flex-1 bg-[var(--tg-danger)] text-white py-2.5 rounded-sm text-sm inline-flex items-center justify-center gap-2 disabled:opacity-40"
                        data-testid={`${testid}-confirm`}
                    >
                        {busy ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                            <Trash2 className="w-3.5 h-3.5" />
                        )}
                        {confirmLabel}
                    </button>
                </div>
            </div>
        </div>
    );
}
