import React, { useCallback, useEffect, useMemo, useState } from "react";
import { adminApi } from "@/lib/api";
import { toast } from "sonner";
import { formatErrorDetail } from "@/lib/errorFormatter";
import TalentAvatar from "@/components/pipeline/TalentAvatar";
import { STAGE_LABELS } from "@/components/pipeline/constants";
import {
    Sparkles,
    Loader2,
    Search,
    RefreshCw,
    Check,
    X,
    AlertTriangle,
    HelpCircle,
    ChevronRight,
    Users,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Separator } from "@/components/ui/separator";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";

const GENDERS = [
    { key: "", label: "Any" },
    { key: "female", label: "Female" },
    { key: "male", label: "Male" },
    { key: "non_binary", label: "Non-binary" },
];

const SUBSCORES = [
    ["requirement_fit", "Requirement"],
    ["character_fit", "Character"],
    ["location_fit", "Location"],
    ["experience_fit", "Experience"],
    ["profile_confidence", "Profile"],
];

const TIER_META = {
    top: { label: "Top Matches", hint: "Strong fit on the data we have." },
    strong: { label: "Strong Matches", hint: "Good fit, worth considering." },
    possible: { label: "Possible Matches", hint: "One or more fields unknown or off-spec — verify before contacting." },
};

function ScoreBar({ label, value }) {
    const known = value !== null && value !== undefined;
    return (
        <div className="flex items-center gap-2 text-[11px]">
            <span className="w-20 shrink-0 text-muted-foreground">{label}</span>
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                {known ? (
                    <div
                        className="h-full rounded-full bg-primary/70"
                        style={{ width: `${Math.max(3, value)}%` }}
                    />
                ) : null}
            </div>
            <span className={`w-8 shrink-0 text-right ${known ? "" : "text-muted-foreground italic"}`}>
                {known ? value : "—"}
            </span>
        </div>
    );
}

const VERDICT_ICON = {
    match: <Check className="h-3 w-3 text-green-600" />,
    near: <Check className="h-3 w-3 text-amber-500" />,
    mismatch: <X className="h-3 w-3 text-destructive" />,
    different_city: <AlertTriangle className="h-3 w-3 text-amber-500" />,
    unknown: <HelpCircle className="h-3 w-3 text-muted-foreground" />,
    "n/a": <span className="text-muted-foreground">·</span>,
};

function VerifiedData({ fv }) {
    const rows = [
        ["Gender", fv.gender],
        ["Age", fv.age],
        ["Height", fv.height],
        ["Location", fv.location],
    ];
    return (
        <div className="rounded-md border bg-background/60 p-2 text-[11px]">
            <div className="mb-1 font-medium text-muted-foreground">Verified from profile</div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
                {rows.map(([k, cell]) => (
                    <div key={k} className="flex items-center gap-1.5">
                        {VERDICT_ICON[cell.verdict] || VERDICT_ICON["n/a"]}
                        <span className="text-muted-foreground">{k}:</span>
                        <span className="truncate">
                            {cell.status === "unknown" || cell.value == null
                                ? <em className="text-muted-foreground">not on file</em>
                                : Array.isArray(cell.value) ? cell.value.join(", ") : String(cell.value)}
                        </span>
                    </div>
                ))}
            </div>
            <div className="mt-1 text-muted-foreground">
                Not tracked in Talentgram: competitive-brand history, availability.
            </div>
        </div>
    );
}

function TalentCard({ r, checked, onToggle }) {
    const inPipeline = r.in_pipeline_stage;
    return (
        <div className={`rounded-lg border p-3 ${checked ? "border-primary bg-accent/40" : "bg-card"}`}>
            <div className="flex items-start gap-3">
                <Checkbox
                    className="mt-1"
                    checked={checked}
                    disabled={!!inPipeline}
                    onCheckedChange={() => onToggle(r.talent_id)}
                />
                <TalentAvatar src={r.image_url} name={r.name} size="md" />
                <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold">{r.name || "Unnamed talent"}</span>
                        {r.instagram_handle ? (
                            <span className="text-xs text-muted-foreground">@{r.instagram_handle}</span>
                        ) : null}
                        {inPipeline ? (
                            <Badge variant="secondary" className="font-normal">
                                Already in {STAGE_LABELS[inPipeline] || inPipeline}
                            </Badge>
                        ) : null}
                    </div>
                    <p className="mt-1 text-sm">{r.reason}</p>
                </div>
                <div className="shrink-0 text-right">
                    <div className="text-2xl font-bold leading-none">
                        {r.overall != null ? r.overall : "—"}
                        <span className="text-xs font-normal text-muted-foreground">/100</span>
                    </div>
                    <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        conf {Math.round((r.confidence || 0) * 100)}%
                    </div>
                    {r.evidence_coverage != null && r.evidence_coverage < 1 ? (
                        <div className="mt-0.5 text-[10px] text-amber-600" title={`Weighted score before the limited-data adjustment: ${r.base_overall}`}>
                            limited data · raw {r.base_overall}
                        </div>
                    ) : null}
                </div>
            </div>

            <div className="mt-3 grid gap-3 md:grid-cols-2">
                <div className="space-y-1">
                    {SUBSCORES.map(([k, label]) => (
                        <ScoreBar key={k} label={label} value={r.scores?.[k]} />
                    ))}
                </div>
                <VerifiedData fv={r.field_verification} />
            </div>

            {(r.strengths?.length || r.risks?.length || r.unknowns?.length) ? (
                <div className="mt-3 flex flex-wrap gap-1.5">
                    {(r.strengths || []).map((s, i) => (
                        <Badge key={`s${i}`} variant="outline" className="border-green-500/40 font-normal text-green-700 dark:text-green-400">
                            {s}
                        </Badge>
                    ))}
                    {(r.risks || []).map((s, i) => (
                        <Badge key={`r${i}`} variant="outline" className="border-amber-500/40 font-normal text-amber-700 dark:text-amber-400">
                            {s}
                        </Badge>
                    ))}
                    {(r.unknowns || []).map((s, i) => (
                        <Badge key={`u${i}`} variant="outline" className="font-normal text-muted-foreground">
                            {s}
                        </Badge>
                    ))}
                </div>
            ) : null}
        </div>
    );
}

export default function ProjectAIScout({ projectId, project }) {
    const [health, setHealth] = useState(null);
    const [phase, setPhase] = useState("loading"); // loading | criteria | running | results
    const [criteria, setCriteria] = useState(null);
    const [criteriaSource, setCriteriaSource] = useState("");
    const [run, setRun] = useState(null);
    const [selected, setSelected] = useState(() => new Set());
    const [adding, setAdding] = useState(false);

    const load = useCallback(async () => {
        try {
            const [{ data: h }, { data: latest }] = await Promise.all([
                adminApi.get("/ai-scout/health"),
                adminApi.get(`/ai-scout/projects/${projectId}/runs/latest`),
            ]);
            setHealth(h);
            if (latest.run) {
                setRun(latest.run);
                setCriteria(latest.run.criteria);
                setPhase("results");
                return;
            }
            const { data: c } = await adminApi.get(`/ai-scout/projects/${projectId}/criteria`);
            setCriteria(c.criteria);
            setCriteriaSource(c.source);
            setPhase("criteria");
        } catch (e) {
            toast.error(formatErrorDetail(e, "Could not load AI Scout"));
            setPhase("criteria");
            setCriteria({ gender: "", age_min: null, age_max: null, height_min: "", locations: [], ethnicity: "", categories: [], competitive_brands_note: "", character_summary: "", hard_filters: [] });
        }
    }, [projectId]);

    useEffect(() => { load(); }, [load]);

    const setC = (patch) => setCriteria((prev) => ({ ...prev, ...patch }));
    const toggleHard = (k) =>
        setCriteria((prev) => {
            const hf = new Set(prev.hard_filters || []);
            hf.has(k) ? hf.delete(k) : hf.add(k);
            return { ...prev, hard_filters: [...hf] };
        });

    const runScout = async (force = false) => {
        setPhase("running");
        setSelected(new Set());
        try {
            const { data } = await adminApi.post(`/ai-scout/projects/${projectId}/run`, { criteria, force });
            setRun(data.run);
            setPhase("results");
            if (data.run.status === "no_candidates") {
                toast.info("No candidates matched — try relaxing the filters");
            }
        } catch (e) {
            toast.error(formatErrorDetail(e, "Scout run failed"));
            setPhase("criteria");
        }
    };

    const grouped = useMemo(() => {
        const g = { top: [], strong: [], possible: [] };
        for (const r of run?.results || []) (g[r.tier] || g.possible).push(r);
        return g;
    }, [run]);

    const selectableTop = useMemo(
        () => grouped.top.filter((r) => !r.in_pipeline_stage).map((r) => r.talent_id),
        [grouped],
    );

    const toggle = (id) =>
        setSelected((prev) => {
            const n = new Set(prev);
            n.has(id) ? n.delete(id) : n.add(id);
            return n;
        });

    const addSelected = async () => {
        if (selected.size === 0) return;
        setAdding(true);
        try {
            const { data } = await adminApi.post(`/ai-scout/projects/${projectId}/select`, {
                run_id: run.id,
                talent_ids: [...selected],
            });
            // reflect the new pipeline stage on the affected cards
            setRun((prev) => ({
                ...prev,
                results: prev.results.map((r) =>
                    data.stage_map[r.talent_id] ? { ...r, in_pipeline_stage: data.stage_map[r.talent_id] } : r,
                ),
            }));
            setSelected(new Set());
            const msg =
                data.skipped > 0
                    ? `Added ${data.added} to Asked to Test (${data.skipped} already in the pipeline)`
                    : `Added ${data.added} to Asked to Test`;
            toast.success(msg);
        } catch (e) {
            toast.error(formatErrorDetail(e, "Could not add talents to the pipeline"));
        } finally {
            setAdding(false);
        }
    };

    if (phase === "loading" || !criteria) {
        return (
            <div className="flex items-center gap-2 p-8 text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading AI Scout…
            </div>
        );
    }

    return (
        <div className="space-y-5 pb-24">
            <div className="flex items-center gap-2">
                <Sparkles className="h-5 w-5 text-primary" />
                <h2 className="text-lg font-semibold">AI Scout</h2>
                <span className="text-sm text-muted-foreground">
                    Ranks existing Talentgram talents for this project. You pick who goes to Asked to Test.
                </span>
            </div>

            {health && !health.llm_configured ? (
                <div className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                    <AlertTriangle className="h-4 w-4" />
                    AI is not configured on the backend (ANTHROPIC_API_KEY missing). Scouting will fail until an admin sets it.
                </div>
            ) : null}

            {/* ---------- CRITERIA ---------- */}
            {(phase === "criteria" || phase === "running") && (
                <section className="rounded-lg border bg-card p-4 md:p-5">
                    <div className="mb-3 flex items-center justify-between">
                        <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                            Scouting requirements
                        </h3>
                        {criteriaSource ? (
                            <Badge variant="outline" className="font-normal">
                                from {criteriaSource === "gate1_session" ? "AI Casting Desk" : criteriaSource === "llm" ? "AI (project text)" : "project text"}
                            </Badge>
                        ) : null}
                    </div>

                    <div className="grid gap-4 md:grid-cols-3">
                        <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Gender</span>
                            <Select value={criteria.gender || "any"} onValueChange={(v) => setC({ gender: v === "any" ? "" : v })}>
                                <SelectTrigger><SelectValue /></SelectTrigger>
                                <SelectContent>
                                    {GENDERS.map((g) => (
                                        <SelectItem key={g.key || "any"} value={g.key || "any"}>{g.label}</SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </label>
                        <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Age range</span>
                            <div className="flex items-center gap-2">
                                <Input type="number" placeholder="min" value={criteria.age_min ?? ""} onChange={(e) => setC({ age_min: e.target.value ? +e.target.value : null })} />
                                <span className="text-muted-foreground">–</span>
                                <Input type="number" placeholder="max" value={criteria.age_max ?? ""} onChange={(e) => setC({ age_max: e.target.value ? +e.target.value : null })} />
                            </div>
                        </label>
                        <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Min height</span>
                            <Input placeholder={`e.g. 5'6"`} value={criteria.height_min || ""} onChange={(e) => setC({ height_min: e.target.value })} />
                        </label>
                        <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Location(s) — comma separated</span>
                            <Input
                                placeholder="e.g. Mumbai"
                                value={(criteria.locations || []).join(", ")}
                                onChange={(e) => setC({ locations: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })}
                            />
                        </label>
                        <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Ethnicity (optional)</span>
                            <Input placeholder="e.g. indian" value={criteria.ethnicity || ""} onChange={(e) => setC({ ethnicity: e.target.value.toLowerCase() })} />
                        </label>
                        <div className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Categories</span>
                            <div className="flex flex-wrap gap-2 pt-1">
                                {["Acting", "Modeling", "Influencer Campaigns"].map((cat) => {
                                    const on = (criteria.categories || []).includes(cat);
                                    return (
                                        <label key={cat} className="flex items-center gap-1.5">
                                            <Checkbox
                                                checked={on}
                                                onCheckedChange={(c) =>
                                                    setC({
                                                        categories: c
                                                            ? [...(criteria.categories || []), cat]
                                                            : (criteria.categories || []).filter((x) => x !== cat),
                                                    })
                                                }
                                            />
                                            {cat}
                                        </label>
                                    );
                                })}
                            </div>
                        </div>
                    </div>

                    <label className="mt-4 block space-y-1 text-sm">
                        <span className="text-muted-foreground">Character / brief the Scout should match against</span>
                        <Textarea rows={3} value={criteria.character_summary || ""} onChange={(e) => setC({ character_summary: e.target.value })} />
                    </label>
                    {criteria.competitive_brands_note ? (
                        <p className="mt-2 text-xs text-amber-600">
                            Competitive-brand restriction noted — Talentgram does not track brand history, so the Scout will flag this for manual checking.
                        </p>
                    ) : null}

                    <Separator className="my-4" />
                    <div className="text-sm">
                        <span className="text-muted-foreground">Treat as a hard filter (exclude off-spec talents; incomplete profiles are still kept):</span>
                        <div className="mt-2 flex flex-wrap gap-4">
                            {["age", "height", "location"].map((k) => (
                                <label key={k} className="flex items-center gap-1.5 capitalize">
                                    <Checkbox checked={(criteria.hard_filters || []).includes(k)} onCheckedChange={() => toggleHard(k)} />
                                    {k}
                                </label>
                            ))}
                        </div>
                    </div>

                    <div className="mt-5 flex items-center gap-3">
                        <Button onClick={() => runScout(false)} disabled={phase === "running"}>
                            {phase === "running" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
                            Find Best Talents
                        </Button>
                        <span className="text-xs text-muted-foreground">Nothing is added to the pipeline — you choose after reviewing.</span>
                    </div>
                </section>
            )}

            {/* ---------- RESULTS ---------- */}
            {phase === "results" && run && (
                <section className="space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card p-3 text-sm">
                        <div className="flex items-center gap-4">
                            <span className="flex items-center gap-1.5"><Users className="h-4 w-4 text-muted-foreground" /> Scanned <b>{run.scanned_count}</b></span>
                            <ChevronRight className="h-3 w-3 text-muted-foreground" />
                            <span>Candidate pool <b>{run.candidate_count}</b>{run.truncated ? " (capped)" : ""}</span>
                            <ChevronRight className="h-3 w-3 text-muted-foreground" />
                            <span>Top <b>{run.tier_counts?.top || 0}</b> · Strong <b>{run.tier_counts?.strong || 0}</b> · Possible <b>{run.tier_counts?.possible || 0}</b></span>
                        </div>
                        <div className="flex items-center gap-2">
                            {selectableTop.length > 0 ? (
                                <Button variant="outline" size="sm" onClick={() => setSelected(new Set(selectableTop))}>
                                    Select all Top ({selectableTop.length})
                                </Button>
                            ) : null}
                            <Button variant="outline" size="sm" onClick={() => setPhase("criteria")}>
                                Edit requirements
                            </Button>
                            <Button variant="outline" size="sm" onClick={() => runScout(true)}>
                                <RefreshCw className="h-4 w-4" /> Re-run
                            </Button>
                        </div>
                    </div>

                    {run.status === "no_candidates" || (run.results || []).length === 0 ? (
                        <div className="rounded-lg border bg-card p-8 text-center text-sm text-muted-foreground">
                            No strong matches found. Try relaxing the requirements or turn off the hard filters to review talents with incomplete profiles.
                        </div>
                    ) : (
                        ["top", "strong", "possible"].map((tier) =>
                            grouped[tier].length ? (
                                <div key={tier}>
                                    <div className="mb-2 flex items-baseline gap-2">
                                        <h3 className="text-sm font-semibold">{TIER_META[tier].label}</h3>
                                        <span className="text-xs text-muted-foreground">{grouped[tier].length} · {TIER_META[tier].hint}</span>
                                    </div>
                                    <div className="space-y-2">
                                        {grouped[tier].map((r) => (
                                            <TalentCard key={r.talent_id} r={r} checked={selected.has(r.talent_id)} onToggle={toggle} />
                                        ))}
                                    </div>
                                </div>
                            ) : null,
                        )
                    )}
                </section>
            )}

            {/* ---------- STICKY ADD BAR ---------- */}
            {selected.size > 0 && (
                <div className="fixed bottom-4 left-1/2 z-40 -translate-x-1/2 rounded-full border bg-background px-4 py-2 shadow-lg">
                    <div className="flex items-center gap-3 text-sm">
                        <span>{selected.size} selected</span>
                        <Button size="sm" onClick={addSelected} disabled={adding}>
                            {adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                            Add to Asked to Test
                        </Button>
                        <button className="text-muted-foreground hover:text-foreground" onClick={() => setSelected(new Set())}>
                            <X className="h-4 w-4" />
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
