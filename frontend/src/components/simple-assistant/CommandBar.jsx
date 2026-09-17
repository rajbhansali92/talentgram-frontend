import React, { useRef, useState } from "react";
import { CornerDownLeft, Loader2, Paperclip, X as XIcon } from "lucide-react";
import { QUICK_COMMANDS } from "@/lib/simpleAssistant";

const MB = 1024 * 1024;

function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < MB) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / MB).toFixed(1)} MB`;
}

/**
 * Command input for the Command Centre. Text + Enter to submit; Shift+Enter
 * for a newline. Quick-command chips POPULATE the box (they don't fire).
 * An optional audition file can be attached (Phase 4C) — it is previewed as
 * an upload plan and nothing is sent until the user confirms.
 */
export default function CommandBar({ onSubmit, busy, step }) {
    const [value, setValue] = useState("");
    const [file, setFile] = useState(null);
    const taRef = useRef(null);
    const fileRef = useRef(null);

    const submit = () => {
        const v = value.trim();
        if ((!v && !file) || busy) return;
        onSubmit(v, file);
        setValue("");
        setFile(null);
        if (fileRef.current) fileRef.current.value = "";
        if (taRef.current) taRef.current.style.height = "auto";
    };

    const onKey = (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
        }
    };

    const grow = (e) => {
        setValue(e.target.value);
        e.target.style.height = "auto";
        e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px";
    };

    const pickFile = (e) => {
        const f = e.target.files?.[0] || null;
        setFile(f);
    };

    return (
        <div className="tga-cmd" data-testid="assistant-command-bar">
            <div className="tga-cmd-box">
                <button
                    type="button"
                    className="tga-attach"
                    onClick={() => fileRef.current?.click()}
                    disabled={busy}
                    title="Attach an audition video"
                    data-testid="assistant-attach"
                >
                    <Paperclip size={14} />
                </button>
                <input
                    ref={fileRef}
                    type="file"
                    accept="video/*"
                    hidden
                    onChange={pickFile}
                    data-testid="assistant-file-input"
                />
                <textarea
                    ref={taRef}
                    rows={1}
                    value={value}
                    onChange={grow}
                    onKeyDown={onKey}
                    disabled={busy}
                    placeholder='Try: "Mark Ahana unavailable for Google AI"  ·  attach a file + "Upload as Ahana&apos;s Take 2 for Google AI"'
                    data-testid="assistant-command-input"
                    aria-label="Command input"
                />
                <button
                    type="button"
                    className="tga-send"
                    onClick={submit}
                    disabled={busy || (!value.trim() && !file)}
                    data-testid="assistant-command-send"
                >
                    {busy ? <Loader2 size={14} className="tga-spin" /> : <CornerDownLeft size={14} />}
                    {busy ? "Working" : "Send"}
                </button>
            </div>

            {file && (
                <div className="tga-file-chip" data-testid="assistant-file-chip">
                    <Paperclip size={12} />
                    <span className="fn">{file.name}</span>
                    <span className="fs">{fmtSize(file.size)}</span>
                    <button
                        type="button"
                        onClick={() => {
                            setFile(null);
                            if (fileRef.current) fileRef.current.value = "";
                        }}
                        aria-label="Remove file"
                        data-testid="assistant-file-remove"
                    >
                        <XIcon size={12} />
                    </button>
                </div>
            )}

            {busy && step && (
                <div className="tga-processing" data-testid="assistant-processing">
                    <span className="dot" />
                    {step}
                </div>
            )}

            {!busy && (
                <div className="tga-chips-row" data-testid="assistant-quick-commands">
                    {QUICK_COMMANDS.map((q) => (
                        <button
                            key={q}
                            type="button"
                            className="tga-qc"
                            onClick={() => {
                                setValue(q);
                                taRef.current?.focus();
                            }}
                            data-testid="assistant-quick-command"
                        >
                            {q}
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}
