import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, FILE_URL } from "@/lib/api";
import { Search, Plus, Image as ImageIcon } from "lucide-react";

export default function TalentList() {
    const [talents, setTalents] = useState([]);
    const [q, setQ] = useState("");
    const [loading, setLoading] = useState(true);

    const load = async (qq = "") => {
        setLoading(true);
        try {
            const { data } = await adminApi.get("/talents", {
                params: qq ? { q: qq } : {},
            });
            setTalents(data);
        } finally {
            setLoading(false);
        }
    };
    useEffect(() => {
        load();
    }, []);
    useEffect(() => {
        const t = setTimeout(() => load(q), 250);
        return () => clearTimeout(t);
    }, [q]);

    return (
        <div
            className="p-6 md:p-12 max-w-7xl mx-auto"
            data-testid="talent-list-page"
        >
            <div className="flex items-end justify-between mb-10 flex-wrap gap-4">
                <div>
                    <p className="eyebrow mb-3">Roster</p>
                    <h1 className="font-display text-4xl md:text-5xl tracking-tight">
                        Talents
                    </h1>
                </div>
                <Link
                    to="/admin/talents/new"
                    data-testid="new-talent-btn"
                    className="inline-flex items-center gap-2 bg-white text-black px-5 py-3 rounded-sm text-xs tracking-wide hover:opacity-90 transition-all"
                >
                    <Plus className="w-4 h-4" strokeWidth={1.5} /> Add Talent
                </Link>
            </div>

            <div className="mb-8 relative max-w-md">
                <Search className="absolute left-0 top-3 w-4 h-4 text-white/40" />
                <input
                    type="text"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="Search by name..."
                    data-testid="talent-search-input"
                    className="w-full bg-transparent border-b border-white/20 focus:border-white outline-none py-3 pl-7 text-sm"
                />
            </div>

            {loading ? (
                <div className="text-white/40 text-sm">Loading...</div>
            ) : talents.length === 0 ? (
                <div className="border border-white/10 p-12 text-center">
                    <ImageIcon
                        className="w-10 h-10 text-white/20 mx-auto mb-4"
                        strokeWidth={1}
                    />
                    <p className="text-white/60 mb-6">No talents yet</p>
                    <Link
                        to="/admin/talents/new"
                        className="inline-flex items-center gap-2 bg-white text-black px-5 py-2.5 rounded-sm text-xs"
                    >
                        Add your first talent
                    </Link>
                </div>
            ) : (
                <div
                    className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-6"
                    data-testid="talents-grid"
                >
                    {talents.map((t) => {
                        const cover = (t.media || []).find(
                            (m) => m.id === t.cover_media_id,
                        );
                        const anyImg =
                            cover ||
                            (t.media || []).find(
                                (m) =>
                                    m.category !== "video" &&
                                    m.content_type?.startsWith("image/"),
                            );
                        return (
                            <Link
                                key={t.id}
                                to={`/admin/talents/${t.id}`}
                                data-testid={`talent-card-${t.id}`}
                                className="group relative border border-white/10 hover:border-white/30 transition-all tg-fade-up"
                            >
                                <div className="aspect-[3/4] bg-[#0c0c0c] overflow-hidden">
                                    {anyImg ? (
                                        <img
                                            src={FILE_URL(anyImg.storage_path)}
                                            alt={t.name}
                                            className="w-full h-full object-cover group-hover:scale-[1.02] transition-all duration-500"
                                        />
                                    ) : (
                                        <div className="w-full h-full flex items-center justify-center text-white/20">
                                            <ImageIcon
                                                className="w-8 h-8"
                                                strokeWidth={1}
                                            />
                                        </div>
                                    )}
                                </div>
                                <div className="p-4">
                                    <div className="font-display text-lg tracking-tight">
                                        {t.name}
                                    </div>
                                    <div className="text-[11px] text-white/40 mt-1 tg-mono">
                                        {t.location ? t.location + " · " : ""}
                                        {(t.media || []).length} assets
                                    </div>
                                </div>
                            </Link>
                        );
                    })}
                </div>
            )}
        </div>
    );
}
