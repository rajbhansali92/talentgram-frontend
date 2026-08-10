// Folder drag-and-drop support (Admin Submission Phase 2, item 2/10).
// Browsers only expose folder structure through the non-standard
// `DataTransferItem.webkitGetAsEntry()` API (Chrome/Edge/Safari; supported
// wherever this app already requires drag-and-drop). Recursively walks any
// dropped directory entries and flattens them into a plain File[] — the
// caller then hands that list to the EXISTING `uploadImages(files, category)`
// path unchanged. No new upload mechanism, just a richer file-collection
// step before the same call.

function readDirectoryEntries(dirReader) {
    return new Promise((resolve, reject) => {
        const all = [];
        const readBatch = () => {
            dirReader.readEntries((entries) => {
                if (!entries.length) {
                    resolve(all);
                    return;
                }
                all.push(...entries);
                readBatch(); // readEntries only returns a batch at a time — keep calling until empty
            }, reject);
        };
        readBatch();
    });
}

function entryToFile(entry) {
    return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function walkEntry(entry, out) {
    if (!entry) return;
    if (entry.isFile) {
        try {
            const file = await entryToFile(entry);
            out.push(file);
        } catch {
            // Skip unreadable entries rather than failing the whole drop.
        }
        return;
    }
    if (entry.isDirectory) {
        const reader = entry.createReader();
        const children = await readDirectoryEntries(reader).catch(() => []);
        for (const child of children) {
            await walkEntry(child, out);
        }
    }
}

/**
 * Resolves a drop event's DataTransfer into a flat File[] — walks any
 * dropped folders recursively; plain file drops resolve immediately via the
 * same array. Falls back to `dataTransfer.files` when the entry API isn't
 * available (older/non-Chromium browsers) — identical to today's behavior.
 */
export async function collectDroppedFiles(dataTransfer) {
    const items = dataTransfer?.items;
    if (!items || !items.length || typeof items[0]?.webkitGetAsEntry !== "function") {
        return Array.from(dataTransfer?.files || []);
    }
    const entries = Array.from(items)
        .map((item) => item.webkitGetAsEntry())
        .filter(Boolean);
    if (entries.length === 0) {
        return Array.from(dataTransfer?.files || []);
    }
    const out = [];
    for (const entry of entries) {
        await walkEntry(entry, out);
    }
    return out;
}
