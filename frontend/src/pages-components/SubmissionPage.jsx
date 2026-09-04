'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { api as axios, adminApi, portalApi, PORTAL_TOKEN_KEY, IMAGE_URL } from "@/lib/api";
import { sendOtp, verifyOtp, buildGoogleAuthUrl, persistPortalToken } from "@/lib/talentAuth";
import { toast } from "sonner";
import { useUploadManager } from "@/context/UploadManagerContext";
import { useStickyFooterHeightVar } from "@/hooks/useStickyFooterHeightVar";
import { revealAndJumpToRequirementItem } from "@/lib/scrollHighlight";
import { REQUIREMENT_TIERS, SUBMIT_BLOCKING_REASONS, CTA_ACTIONS, SECTION_STATUS, OPERATIONAL_STATES } from "@/lib/readinessStatus";
import { useSubmissionExperienceModel } from "@/hooks/useSubmissionExperienceModel";
import { computeRequirementItems } from "@/lib/requirementEngine";
import { buildRecognizedIdentity, shouldAttemptSilentRecognition, classifyPortalLookupResult, tokenAuthenticatesEmail } from "@/lib/returningTalent";
import { formatErrorDetail } from "@/lib/errorFormatter";
import { splitPendingConsentByKnownDestination, groupByDestinationDecision } from "@/lib/mediaDestination";
import { useSwipeStep } from "@/hooks/useSwipeStep";
import SubmissionReadinessPanel from "@/components/shared/SubmissionReadinessPanel";
import LibraryMediaPicker from "@/components/submission/LibraryMediaPicker";
import WizardProgressBar from "@/components/submission/WizardProgressBar";
import WizardStepNav from "@/components/submission/WizardStepNav";
import UpdateProfileDisclosure from "@/components/submission/UpdateProfileDisclosure";
import { WIZARD_STEPS, TOTAL_STEPS, stepForSection, sectionForStep, readStep, writeStep, clearStep, wizardStepsForDisplay, readFinalStepReached, writeFinalStepReached, clearFinalStepReached } from "@/lib/wizardSteps";
import MaterialModal from "@/components/MaterialModal";
import Logo from "@/components/Logo";
import SkillsSelector from "@/components/SkillsSelector";
import LocationSelector from "@/components/LocationSelector";
import DobInput from "@/components/DobInput";
import ThemeToggle from "@/components/ThemeToggle";
import HlsVideo from "@/components/HlsVideo";
import { thumbnailUrl, posterUrl, normalizeInstagramHandle } from "@/lib/mediaUtils";
import { collectDroppedFiles } from "@/lib/collectDroppedFiles";
import { suggestCategoriesForBatch } from "@/lib/mediaCategorization";
import CategorizationReviewModal from "@/components/submission/CategorizationReviewModal";
import {
    Select,
    SelectContent,
    SelectGroup,
    SelectItem,
    SelectLabel,
    SelectSeparator,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    FolderOpen,
    Upload,
    Video,
    Camera,
    Check,
    Trash2,
    Loader2,
    X,
    Sparkles,
    Plus,
    Mic,
    MessageSquare,
    ChevronDown,
    ArrowRight,
    ArrowLeft,
    ChevronRight,
    User,
    Search,
    Play,
    Pause,
} from "lucide-react";
import {
    HEIGHT_OPTIONS,
    GENDER_OPTIONS,
    ETHNICITY_OPTIONS,
    FOLLOWER_TIERS,
    AVAILABILITY_OPTIONS,
    BUDGET_OPTIONS,
    calcAge,
} from "@/lib/talentSchema";

// --- Public project-load resilience -----------------------------------------
// P0 fix: the loader previously rendered "Project not found." for EVERY fetch
// failure (timeout, offline, CORS, 5xx, aborted), not just a genuine 404. These
// helpers classify the failure, drive bounded retry with backoff for transient
// errors, and emit structured diagnostics (no sensitive data).
const PROJECT_LOAD_TIMEOUT_MS = 12_000;
const PROJECT_LOAD_MAX_ATTEMPTS = 3;

function classifyLoadError(err) {
    if (err && err.response) {
        const status = err.response.status;
        if (status === 404) return { kind: "not_found", status, retry: false };
        if (status >= 500) return { kind: "server_error", status, retry: true };
        return { kind: "http_error", status, retry: false };
    }
    if (err && (err.code === "ECONNABORTED" || /timeout/i.test(err.message || ""))) {
        return { kind: "timeout", status: null, retry: true };
    }
    if (err && (err.code === "ERR_CANCELED" || err.name === "CanceledError")) {
        return { kind: "aborted", status: null, retry: false };
    }
    // Request made but no response: network / CORS / DNS / offline / conn reset.
    return { kind: "network", status: null, retry: true };
}

function getDeviceId() {
    try {
        let did = localStorage.getItem("tg_device_id");
        if (!did) {
            did = "dev_" + Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
            localStorage.setItem("tg_device_id", did);
        }
        return did;
    } catch (_) {
        return "unknown_device";
    }
}

function parseUserAgent(ua) {
    let browser = "Unknown Browser";
    let browserVersion = "unknown";
    let os = "Unknown OS";
    let osVersion = "unknown";
    let deviceType = "Desktop";

    if (/mobi|android|iphone|ipad|ipod|blackberry|iemobile|opera mini/i.test(ua)) {
        deviceType = "Mobile";
        if (/ipad|tablet/i.test(ua)) {
            deviceType = "Tablet";
        }
    }

    if (/whatsapp/i.test(ua)) {
        browser = "WhatsApp WebView";
    } else if (/instagram/i.test(ua)) {
        browser = "Instagram WebView";
    } else if (/fb_iab|fbav/i.test(ua)) {
        browser = "Facebook WebView";
    } else if (/messenger/i.test(ua)) {
        browser = "Messenger WebView";
    } else if (/telegram/i.test(ua)) {
        browser = "Telegram WebView";
    } else if (/chrome|crios/i.test(ua)) {
        browser = "Chrome";
        const match = ua.match(/(?:chrome|crios)\/([0-9.]+)/i);
        if (match) browserVersion = match[1];
    } else if (/safari/i.test(ua) && !/chrome|crios/i.test(ua)) {
        browser = "Safari";
        const match = ua.match(/version\/([0-9.]+)/i);
        if (match) browserVersion = match[1];
    } else if (/firefox|fxios/i.test(ua)) {
        browser = "Firefox";
        const match = ua.match(/(?:firefox|fxios)\/([0-9.]+)/i);
        if (match) browserVersion = match[1];
    }

    if (/iphone|ipad|ipod/i.test(ua)) {
        os = "iOS";
        const match = ua.match(/os\s+([0-9_]+)/i);
        if (match) osVersion = match[1].replace(/_/g, ".");
    } else if (/android/i.test(ua)) {
        os = "Android";
        const match = ua.match(/android\s+([0-9.]+)/i);
        if (match) osVersion = match[1];
    } else if (/windows/i.test(ua)) {
        os = "Windows";
        const match = ua.match(/phone\s+([0-9.]+)/i) || ua.match(/nt\s+([0-9.]+)/i);
        if (match) osVersion = match[1];
    } else if (/macintosh/i.test(ua)) {
        os = "macOS";
        const match = ua.match(/os\s+x\s+([0-9_]+)/i);
        if (match) osVersion = match[1].replace(/_/g, ".");
    }

    return { browser, browserVersion, os, osVersion, deviceType };
}

async function sendDiagnostics({ attempt, retry_succeeded, kind, status, time_taken_ms, err, slug, requestUrl }) {
    try {
        const nav = typeof navigator !== "undefined" ? navigator : {};
        const conn = nav.connection || nav.mozConnection || nav.webkitConnection || {};
        const scr = typeof screen !== "undefined" ? screen : {};
        const win = typeof window !== "undefined" ? window : {};
        
        const ua = nav.userAgent || "";
        const parsedUA = parseUserAgent(ua);

        const isWhatsApp = /whatsapp/i.test(ua);
        const isInstagram = /instagram/i.test(ua);
        const isFacebook = /fb_iab|fbav/i.test(ua);
        const isMessenger = /messenger/i.test(ua);
        const isTelegram = /telegram/i.test(ua);
        const isChrome = parsedUA.browser === "Chrome";
        const isSafari = parsedUA.browser === "Safari";
        const isInApp = isWhatsApp || isInstagram || isFacebook || isMessenger || isTelegram;

        let xRailwayRequestId = null;
        let xRequestId = null;
        let traceparent = null;
        let safeHeaders = {};

        if (err && err.response) {
            const h = err.response.headers || {};
            const whitelist = ["content-type", "server", "x-railway-request-id", "x-request-id", "traceparent", "cache-control"];
            whitelist.forEach(key => {
                if (h[key]) safeHeaders[key] = h[key];
            });
            xRailwayRequestId = h["x-railway-request-id"] || null;
            xRequestId = h["x-request-id"] || null;
            traceparent = h["traceparent"] || null;
        }

        let swControllerPresent = false;
        let swRegistrationStatus = "unsupported";
        let swScriptUrl = null;
        let swWaitingPresent = false;
        let swInstallingPresent = false;

        if (typeof navigator !== "undefined" && navigator.serviceWorker) {
            swControllerPresent = !!navigator.serviceWorker.controller;
            if (navigator.serviceWorker.controller) {
                swScriptUrl = navigator.serviceWorker.controller.scriptURL || null;
            }
            try {
                const regs = await navigator.serviceWorker.getRegistrations();
                swRegistrationStatus = regs.length > 0 ? "active" : "inactive";
                if (regs.length > 0) {
                    swWaitingPresent = !!regs[0].waiting;
                    swInstallingPresent = !!regs[0].installing;
                }
            } catch (_) {
                swRegistrationStatus = "error";
            }
        }

        const payload = {
            device_id: getDeviceId(),
            project_slug: slug,
            request_url: requestUrl,
            page_url: win.location?.href || "",
            axios_code: err?.code || null,
            axios_message: err?.message || null,
            response_status: status ?? null,
            response_headers: safeHeaders,
            is_timeout: kind === "timeout",
            is_network: kind === "network",
            is_cancellation: kind === "aborted" || (err && (err.code === "ERR_CANCELED" || err.name === "CanceledError")),
            user_agent: ua,
            platform: nav.platform || null,
            language: nav.language || null,
            is_online: nav.onLine ?? null,
            connection_info: {
                effectiveType: conn.effectiveType || null,
                downlink: conn.downlink || null,
                rtt: conn.rtt || null,
                saveData: conn.saveData || null,
            },
            viewport_width: win.innerWidth || 0,
            viewport_height: win.innerHeight || 0,
            device_pixel_ratio: win.devicePixelRatio || 1,
            referrer: document.referrer || null,
            is_whatsapp: isWhatsApp,
            is_instagram: isInstagram,
            is_facebook: isFacebook,
            is_safari: isSafari,
            is_chrome: isChrome,
            is_in_app: isInApp,
            sw_controller_present: swControllerPresent,
            sw_registration_status: swRegistrationStatus,
            sw_version: "talentgram-pwa-v4",
            sw_script_url: swScriptUrl,
            sw_waiting_present: swWaitingPresent,
            sw_installing_present: swInstallingPresent,
            app_version: "1.0.0",
            build_timestamp: new Date().toISOString(),
            frontend_build_id: "production-build",
            commit_sha: process.env.NEXT_PUBLIC_VERCEL_GIT_COMMIT_SHA || null,
            environment: process.env.NODE_ENV || "production",
            time_taken_ms: time_taken_ms,
            retry_attempt_count: attempt,
            retry_succeeded: retry_succeeded,
            failure_type: kind,
            browser: parsedUA.browser,
            browser_version: parsedUA.browserVersion,
            os: parsedUA.os,
            os_version: parsedUA.osVersion,
            device_type: parsedUA.deviceType,
            x_railway_request_id: xRailwayRequestId,
            x_request_id: xRequestId,
            traceparent: traceparent
        };

        fetch("/api/public/diagnostics", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        }).catch(() => { /* silent ignore */ });

    } catch (_) {
        // diagnostics must never crash the page
    }
}

function logProjectLoadFailure(info) {
    try {
        const nav = typeof navigator !== "undefined" ? navigator : {};
        // eslint-disable-next-line no-console
        console.error("[submit] project_load_failed", {
            slug: info.slug,
            requestUrl: info.requestUrl,
            errorType: info.kind,
            httpStatus: info.status ?? null,
            timeout: info.kind === "timeout",
            networkError: info.kind === "network",
            attempt: info.attempt,
            userAgent: nav.userAgent || null,
            platform: nav.platform || null,
        });
    } catch (_) {
        /* diagnostics must never throw */
    }
}

const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const MAX_IMAGES = 8;
// Phase 3: per-category portfolio image cap. Each of `image`/`indian`/
// `western` is independently capped at this value, NOT combined.
const MAX_IMAGES_PER_CATEGORY = 10;
const LS_KEY = (slug) => `tg_submission_${slug}`;
const LS_DRAFT_KEY = (slug) => `tg_draft_${slug}`;
// Long-lived opaque access token (stored in DB). Survives JWT expiry and
// cross-browser / cross-device scenarios where only the URL slug is known.
const LS_ATK_KEY = (slug) => `tg_atk_${slug}`;

// Whole-section scroll targets for SectionStatusBadge clicks on a section
// that has nothing unresolved (already complete, or has no required items).
// Mirrors the CSS-selector fallback `resolveRequirementElement` already uses
// in lib/scrollHighlight.js — same mechanism, just anchored to the section
// wrapper instead of one field.
const SECTION_WRAPPER_SELECTOR = {
    profile: '[data-testid="profile-section"]',
    skills: '[data-testid="skills-section"]',
    projectQuestions: '[data-testid="project-questions-section"]',
    uploads: '[data-testid="uploads-section"]',
};


function readSaved(slug) {
    if (typeof window === "undefined") return null;
    try {
        return JSON.parse(localStorage.getItem(LS_KEY(slug)) || "null");
    } catch {
        return null;
    }
}

// Draft form persistence — survives a refresh / app-switch on mobile so
// users never lose what they've typed even before the talent record is
// created on the backend.
function readDraft(slug) {
    if (typeof window === "undefined") return null;
    try {
        return JSON.parse(localStorage.getItem(LS_DRAFT_KEY(slug)) || "null");
    } catch {
        return null;
    }
}

// Sprint 1 — best-effort, *non-destructive* expansion of Indian budget shorthand
// (e.g. "1L" → "₹1,00,000", "2.5 Cr" → "₹2,50,00,000"). Returns null unless the
// WHOLE string is a recognised shorthand, so admin-entered free-text is never
// rewritten — the original value is always rendered as primary; this is only an
// optional clarifying hint shown beneath it.
const expandIndianBudgetShorthand = (raw) => {
    if (!raw || typeof raw !== "string") return null;
    const m = raw.trim().match(/^(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(l|lac|lacs|lakh|lakhs|cr|crore|crores)$/i);
    if (!m) return null;
    const num = parseFloat(m[1]);
    if (!isFinite(num)) return null;
    const unit = m[2].toLowerCase();
    const multiplier = unit.startsWith("c") ? 1e7 : 1e5; // crore vs lakh
    const amount = Math.round(num * multiplier);
    return `₹${amount.toLocaleString("en-IN")}`;
};

// Merges the backend's saved submission.form_data into local `form` state
// on resume (page reload / reopen). MUST NOT use a blind `{...f, ...fd}`
// spread: the backend only has whatever the talent's last successful
// saveForm() PATCH sent, so any field typed locally after that (e.g. Next
// was blocked by a DIFFERENT field's validation, so saveForm() never ran)
// is more current than the backend's copy — a raw spread lets that stale/
// blank backend value silently clobber it. Same "prefer local if it has
// content, else backend" rule populatePrefillData already uses elsewhere
// in this file, just covering the full form_data shape (`base` above).
function mergeResumedFormData(f, fd) {
    if (!fd) return f;
    const scalar = (key) => (f[key] || f[key] === 0 ? f[key] : fd[key]);
    const list = (key) => ((f[key] && f[key].length) ? f[key] : (fd[key] || []));
    return {
        ...f,
        first_name: scalar("first_name"),
        last_name: scalar("last_name"),
        email: scalar("email"),
        phone: scalar("phone"),
        alternate_contact_number: scalar("alternate_contact_number"),
        dob: scalar("dob"),
        age: scalar("age"),
        overrideAge: f.overrideAge || fd.overrideAge || false,
        submitted_age_override: scalar("submitted_age_override"),
        height: scalar("height"),
        location: list("location"),
        gender: scalar("gender"),
        ethnicity: scalar("ethnicity"),
        instagram_handle: scalar("instagram_handle"),
        instagram_followers: scalar("instagram_followers"),
        bio: scalar("bio"),
        work_links: list("work_links"),
        skills: list("skills"),
        has_competitive_brand_experience:
            f.has_competitive_brand_experience !== null && f.has_competitive_brand_experience !== undefined
                ? f.has_competitive_brand_experience
                : (fd.has_competitive_brand_experience ?? null),
        competitive_brand: scalar("competitive_brand"),
        commission: scalar("commission"),
        custom_answers: { ...(fd.custom_answers || {}), ...(f.custom_answers || {}) },
        availability:
            f.availability && f.availability.status
                ? f.availability
                : (typeof fd.availability === "object" && fd.availability !== null
                    ? { status: "", note: "", ...fd.availability }
                    : f.availability),
        budget:
            f.budget && f.budget.status
                ? f.budget
                : (typeof fd.budget === "object" && fd.budget !== null
                    ? { status: "", value: "", ...fd.budget }
                    : f.budget),
    };
}

const formatDuration = (sec) => {
    if (!sec) return null;
    const s = Math.round(sec);
    const mins = Math.floor(s / 60);
    const secs = s % 60;
    return `${mins}:${secs < 10 ? "0" : ""}${secs}`;
};

const formatMediaTimestamp = (m) => {
    const dStr = m?.updated_at || m?.created_at;
    if (!dStr) return null;
    try {
        const d = new Date(dStr);
        if (isNaN(d.getTime())) return null;
        return d.toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
        });
    } catch {
        return null;
    }
};

// Admin Mode ("Upload on Behalf") — persistent banner so it's always clear
// this session is an admin acting for a talent, never mistakable for the
// talent's own view. The client never sees any trace of this — it's purely
// this page's own chrome.
function AdminModeBanner({ talentName }) {
    return (
        <div
            className="sticky top-0 z-50 w-full bg-[#0c2340] text-white text-xs sm:text-sm font-medium py-2 px-4 text-center"
            data-testid="admin-mode-banner"
        >
            Admin Mode — submitting on behalf of {talentName || "this talent"}
        </div>
    );
}

function SubmissionPage() {
    const { slug } = useParams();
    const searchParams = useSearchParams();
    // Admin Mode ("Upload on Behalf") — entered only via a Pipeline card
    // action, never by a talent. `pid`/`talentId` identify who this session
    // is for; `sid` (optional) resumes an existing draft instead of creating
    // a new one. The submitter token itself is never carried in the URL —
    // see the admin-start bootstrap effect below, which mints it fresh into
    // React state on every mount.
    const adminMode = searchParams?.get("admin") === "1";
    const adminProjectId = searchParams?.get("pid") || null;
    const adminTalentId = searchParams?.get("talentId") || null;
    const adminExistingSid = searchParams?.get("sid") || null;
    const [project, setProject] = useState(null);
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState(null);   // {kind,status} — drives retryable vs not-found
    const [reloadNonce, setReloadNonce] = useState(0);  // bumped by the Retry button
    const [saved, setSaved] = useState(() => (adminMode ? null : readSaved(slug)));
    const [showMaterial, setShowMaterial] = useState(false);
    // Project details must stay reachable at every point in the flow, not
    // just pre-auth — expanded by default before the email gate unlocks
    // (this is the first thing a talent reads), collapsed behind a "View
    // Project Details" toggle once they're actively filling in the form or
    // have already submitted, so it doesn't visually compete with the form
    // itself.
    const [showProjectDetails, setShowProjectDetails] = useState(false);
    const [activeLightboxImage, setActiveLightboxImage] = useState(null);
    // Admin Mode bootstrap state — populated by the admin-start effect
    // further down, once `project` has loaded (admin-start needs `pid`,
    // which we already have from the URL, so it doesn't actually wait on
    // `project`, but the talent's display name for the banner comes back in
    // the same response).
    const [adminBootstrapping, setAdminBootstrapping] = useState(adminMode);
    const [adminBootstrapError, setAdminBootstrapError] = useState(null);
    const [adminTalentName, setAdminTalentName] = useState(null);

    // Full form (with draft restoration from localStorage)
    const [form, setForm] = useState(() => {
        const draft = adminMode ? null : readDraft(slug);
        const base = {
            first_name: "",
            last_name: "",
            email: "",
            phone: "",
            alternate_contact_number: "",
            dob: "",
            age: "",
            overrideAge: false,
            submitted_age_override: "",
            height: "",
            location: [],
            // Phase 2 — schema unification: every talent-facing form writes
            // the SAME shape directly into the talent record. No separate
            // mappings.
            gender: "",
            ethnicity: "",
            instagram_handle: "",
            instagram_followers: "",
            bio: "",
            work_links: [],
            skills: [],
            // NONE/YES answer; `competitive_brand` itself (below, kept in
            // its original spot alphabetically-adjacent fields) is the
            // free-text response, only meaningful when this is true — the
            // canonical downstream string field, unchanged in shape.
            has_competitive_brand_experience: null,
            competitive_brand: "",
            availability: { status: "", note: "" },
            budget: { status: "", value: "" },
            commission: "",
            custom_answers: {},
        };
        return draft ? { ...base, ...draft } : base;
    });
    const [starting, setStarting] = useState(false);
    const [submitAttempted, setSubmitAttempted] = useState(false);
    const [validationErrors, setValidationErrors] = useState({}); // { fieldId: errorMessage }
    const fieldRefs = useRef({}); // { fieldId: HTMLElement }

    const [submission, setSubmission] = useState(null);
    // Talent Profile Migration, Phase 3 — the talent's reusable Talent
    // Profile media, computed live server-side (build_prefill_media()) and
    // never auto-injected into `submission.media` anymore. Populated from
    // `/public/prefill` at email blur and refreshed from every
    // GET/resume/from-library response afterward (see
    // applySubmissionResponse below) so it stays honest against admin edits
    // made mid-draft.
    const [libraryMedia, setLibraryMedia] = useState([]);
    const [libraryBusyId, setLibraryBusyId] = useState(null);
    // Returning-talent Media step: whether the "My Saved Media" picker is
    // expanded. Starts expanded (matches prior behavior) and collapses to a
    // compact "Change" summary once auto-reuse has run — see the
    // auto-reuse effect near selectAllLibraryMedia below.
    const [showLibraryPicker, setShowLibraryPicker] = useState(true);
    const autoReuseAttemptedRef = useRef(false);
    const [dismissedRemovedWarnings, setDismissedRemovedWarnings] = useState(() => new Set());
    // Talent Profile Migration, Phase 4 — every reusable-category item
    // (intro_video/image/indian/western) the talent has JUST uploaded but
    // not yet said "only this project" or "update my Talent Profile" about.
    // Populated from `submission.pending_media_consent` (see
    // applySubmissionResponse below) — the backend is the single source of
    // truth for what's pending, this is never computed client-side.
    const [pendingMediaConsent, setPendingMediaConsent] = useState([]);
    const [mediaConsentSubmitting, setMediaConsentSubmitting] = useState(false);
    // Default selection is "only this project" per spec — nothing auto-updates.
    const [mediaConsentChoice, setMediaConsentChoice] = useState("only_this_project");
    // UX-polish fix — the "My Media Library" / "This Project" destination
    // question now has to be asked BEFORE the talent picks a file, not
    // after upload (see the auto-resolve effect near submitMediaConsent
    // below, which uses this to answer the pending-consent question the
    // instant it appears — before the old post-upload dialog ever gets a
    // chance to render; see lib/mediaDestination.js's
    // `splitPendingConsentByKnownDestination`, used both by that effect
    // and by the dialog's own render condition below, for how that race
    // is actually closed, not just usually-won). Keyed by the two independent decisions a talent can
    // make in one sitting: `intro_video` and `images` (one choice covers
    // whichever image sub-category — generic/Indian/Western — they upload
    // into, matching the single on-screen toggle). "library" is the
    // default for both — a talent's reusable Media Library/Dashboard is
    // the expected home for new uploads; "project" only applies once
    // explicitly chosen.
    const [mediaDestination, setMediaDestination] = useState({ intro_video: "library", images: "library" });
    // Sprint 1 — autosave indicator. "idle" | "saving" | "saved". Driven by the
    // debounced draft-persistence effect below so it reflects real save activity.
    const [saveStatus, setSaveStatus] = useState("idle");
    const draftMountRef = useRef(false);
    // Sprint 1 — height unit toggle. Display-only: the stored value is ALWAYS the
    // canonical feet/inches string (e.g. 5'8"). "cm" mode merely relabels the same
    // options, so backend storage and existing talent data are never touched.
    const [heightUnit, setHeightUnit] = useState("ft");
    const { activeUploads, retryQueue, uploadFile, retryUpload } = useUploadManager();
    const stickyFooterRef = useRef(null);
    useStickyFooterHeightVar(stickyFooterRef, "--tg-sticky-cta-h");
    const [finalizing, setFinalizing] = useState(false);
    const [editMode, setEditMode] = useState(false);
    const requirements = project?.submission_requirements || {};

    // Collapsible sections state
    const [collapsedSections, setCollapsedSections] = useState({
        profile: false,           // open by default
        skills: false,             // open by default
        projectQuestions: false,   // open by default
        uploads: false,            // open by default
    });

    // Submission Wizard — current step (1-4, see lib/wizardSteps.js). Admin
    // Mode never uses the wizard's own localStorage step key (it doesn't use
    // LS_DRAFT_KEY either — see the admin-start bootstrap effect), so it
    // always starts at 1 there and lets its own bootstrap logic decide what
    // to show. For a real talent, initialize from whichever signal is
    // available at MOUNT time (a resumed submission's persisted step); the
    // "returning talent, no submission yet" case (OTP/Google prefill) is
    // decided slightly later, once that prefill response actually arrives,
    // and is applied via setCurrentStep from those handlers directly.
    const [currentStep, setCurrentStepState] = useState(() => {
        if (adminMode) return 1;
        return readStep(slug) || 1;
    });
    // Direction of the last step change, purely for which slide-in
    // animation to play (lib/index.css's tg-step-slide-in-forward/-back) —
    // never read for any navigation/validation decision.
    const [stepDirection, setStepDirection] = useState("forward");
    const setCurrentStep = useCallback((step) => {
        setCurrentStepState((prev) => {
            const resolved = typeof step === "function" ? step(prev) : step;
            setStepDirection(resolved >= prev ? "forward" : "back");
            if (!adminMode) writeStep(slug, resolved);
            return resolved;
        });
    }, [adminMode, slug]);
    // CSS-hide (not unmount) a step that isn't current — mirrors
    // ProjectEdit.jsx's tab pattern, so PremiumPortfolioGroup's local
    // collapse state, the Upload Manager, and every field's local state
    // survive tabbing away and back. `Tailwind`'s `hidden` utility already
    // excludes the element from the tab order and a11y tree for free.
    const stepVisibilityClass = (stepId) =>
        currentStep === stepId
            ? (stepDirection === "forward" ? "tg-step-slide-in-forward" : "tg-step-slide-in-back")
            : "hidden";
    const [isGenericPortfolioCollapsed, setIsGenericPortfolioCollapsed] = useState(() => {
        return typeof window !== "undefined" && window.innerWidth < 768;
    });


    // Portfolio (General) — tracks which thumbnail has its action overlay
    // visible on touch devices (tap-to-reveal). null = all overlays hidden.
    const [activePortfolioThumbId, setActivePortfolioThumbId] = useState(null);

    // Ref: prevents the ATK-resume useEffect from running more than once per mount.
    const atkTriedRef = useRef(false);


    // Email-first gate: hides every form section EXCEPT the email field
    // until the talent's email has been blurred and the prefill response
    // is processed (Use this / Edit manually / no match).
    // Initialised here (rather than later in the component body) so
    // validateForm / validateStep1 can read it without TDZ surprises.
    const [emailGateUnlocked, setEmailGateUnlocked] = useState(() => {
        // Admin Mode — the admin's own authenticated session is the identity
        // proof (no OTP/email-ownership gate applies to on-behalf
        // submissions), so every section is unlocked from the start. The
        // talent is already known from the Pipeline card that launched this
        // page, not re-entered here.
        if (adminMode) return true;
        if (typeof window === "undefined") return false;
        // Issue 1: every project submission link must ALWAYS begin on the
        // landing page and require fresh authentication. Only a per-slug
        // in-progress submission for THIS exact project (its saved
        // JWT/ATK session) may skip the gate — that represents a submission
        // the talent already authenticated to create on this project. A
        // GLOBAL cross-project portal/Google session must NOT bypass the
        // landing page.
        return !!readSaved(slug);
    });
    const [prefillTried, setPrefillTried] = useState(false);
    const [prefillSuggestion, setPrefillSuggestion] = useState(null); // {data}
    const [prefillEmail, setPrefillEmail] = useState("");

    // Inline Portal Gateway states
    const [gatewayEmail, setGatewayEmail] = useState("");
    const [gatewayLoading, setGatewayLoading] = useState(false);
    const [gatewayRecognition, setGatewayRecognition] = useState(null);
    const [otpSent, setOtpSent] = useState(false);
    const [otpValue, setOtpValue] = useState("");
    const [otpLoading, setOtpLoading] = useState(false);
    const [otpResending, setOtpResending] = useState(false);

    // "UPLOAD TEST" silent recognition (returning-talent flow) — distinct
    // from `gatewayRecognition` above, which stays tied to the email-typed
    // Step A fallback UI. `recognizedIdentity` holds the profile returned by
    // an authenticated `GET /public/prefill` call (real bearer-token proof,
    // not a self-reported email), consumed later by the Identity
    // Confirmation card. `recognizing` only drives the CTA's own busy state.
    const [recognizing, setRecognizing] = useState(false);
    const [recognizedIdentity, setRecognizedIdentity] = useState(null);

    // Phase 2 (new-talent flow) — whether THIS session has actual proof of
    // email ownership (real OTP verify, Google OAuth, or a valid portal
    // token). A genuinely new/unrecognized talent is unlocked WITHOUT this
    // (see handleInlineLookup's not-found branch) so they can fill out the
    // whole submission with zero auth friction; `emailVerified` is what
    // gates the final "Almost Done" step before they can actually submit.
    // Recognized-by-token and email-typed-then-OTP-verified talents are
    // already true by the time they'd reach that gate, so they never see it.
    const [emailVerified, setEmailVerified] = useState(false);

    // Phase 3 (progress-indicator fix) — whether THIS session was recognized
    // as an EXISTING talent (as opposed to unlocked via the confirmed-new,
    // zero-friction path). Purely a display concern: it decides which step
    // chips WizardProgressBar shows, never anything about validation,
    // navigation, or the underlying draft/finalize lifecycle. Defaults to
    // false (new-talent, all 4 steps shown) — only flipped to true by an
    // actual existing-talent recognition (token-silent, OTP-verified-
    // existing, or Google-existing), never guessed at.
    const [isReturningTalent, setIsReturningTalent] = useState(false);

    // Manual-testing fix — the first project page must show ONLY Project
    // Details + the "UPLOAD TEST" CTA, with no auth UI (Google/Email/OTP/
    // Identity Confirmation) visible before the talent acts. The Talent
    // Details section below (Step A/B auth, Profile, Skills, Project
    // Questions) used to render unconditionally on load; it's now gated on
    // this flag OR `emailGateUnlocked` (so admin mode / a resumed in-
    // progress draft, which already unlock the gate on mount, still render
    // immediately with no extra click needed). Flipped true only by an
    // explicit "UPLOAD TEST" click, via `revealAndScrollToTalentDetails`.
    const [talentDetailsRevealed, setTalentDetailsRevealed] = useState(false);

    // UX-polish fix — the final submission CTA (Identity Confirmation /
    // Almost Done / plain finalize button) must appear ONLY on the actual
    // final page, never stacked underneath whatever step the talent
    // happens to be viewing the instant `experience.readinessSummary.ready`
    // flips true. Previously that footer rendered unconditionally once
    // ready — for a returning talent that meant it could appear directly
    // under Media (their second-to-last page) the moment the last required
    // upload finished, and for a new talent, under Skills, with no
    // deliberate "go to the final page" action in between. This is a real
    // reveal gate (same pattern as `talentDetailsRevealed` above), not a
    // CSS hide: the footer's content doesn't render at all until this is
    // true. Flipped true only by the explicit "Continue" button rendered
    // in its place while false. Admin Mode bypasses it (see the ternary at
    // the footer itself) — an admin submitting on a talent's behalf has no
    // "second-last page" concept to protect.
    const [finalStepReached, setFinalStepReached] = useState(() => {
        if (adminMode) return false;
        return readFinalStepReached(slug);
    });

    const introRef = useRef();
    const take1Ref = useRef();
    const newTakeRef = useRef();
    const imagesRef = useRef();
    const cameraImagesRef = useRef(); // mobile camera-first photo capture
    const indianImagesRef = useRef();
    const westernImagesRef = useRef();
    // Admin Mode extra look categories
    const selfieImagesRef = useRef();
    const profilesImagesRef = useRef();
    const fullLengthImagesRef = useRef();
    const sideProfileImagesRef = useRef();
    const ethnicImagesRef = useRef();
    const additionalPortfolioImagesRef = useRef();
    // Automatic Media Categorization (item 3) — generic bulk-add zone state.
    const bulkCategorizeInputRef = useRef();
    const [isBulkDragOver, setIsBulkDragOver] = useState(false);
    const [categorizingBatch, setCategorizingBatch] = useState(false);
    const [categorizationBatch, setCategorizationBatch] = useState(null); // {groups, uncategorized} | null
    const uploadsSectionRef = useRef();

    // Load project — classify failures, retry transient errors with backoff,
    // and only ever surface "Project not found." on a genuine 404 (see render).
    useEffect(() => {
        let cancelled = false;
        const controller = new AbortController();
        const requestUrl = `/public/projects/${slug}`;
        const startTime = Date.now();

        (async () => {
            setLoadError(null);
            setLoading(true);
            for (let attempt = 1; attempt <= PROJECT_LOAD_MAX_ATTEMPTS; attempt++) {
                try {
                    const { data } = await axios.get(requestUrl, {
                        timeout: PROJECT_LOAD_TIMEOUT_MS,
                        signal: controller.signal,
                    });
                    if (cancelled) return;
                    setProject(data);
                    // Snapshot commission on the form so it's preserved at submission time
                    setForm((f) => ({
                        ...f,
                        commission: f.commission || data.commission_percent || "",
                    }));
                    setLoading(false);
                    if (attempt > 1) {
                        // Log recovery diagnostics (attempt > 1 eventually succeeded)
                        sendDiagnostics({
                            attempt,
                            retry_succeeded: true,
                            kind: "success",
                            status: 200,
                            time_taken_ms: Date.now() - startTime,
                            err: null,
                            slug,
                            requestUrl
                        });
                    }
                    return;
                } catch (err) {
                    if (cancelled) return;
                    const cls = classifyLoadError(err);
                    logProjectLoadFailure({ ...cls, slug, requestUrl, attempt });
                    if (cls.kind === "aborted") return;           // navigated away — stop silently
                    
                    if (!cls.retry) {                             // genuine 404 / other 4xx — terminal
                        setLoadError(cls);
                        setLoading(false);
                        sendDiagnostics({
                            attempt,
                            retry_succeeded: false,
                            kind: cls.kind,
                            status: cls.status,
                            time_taken_ms: Date.now() - startTime,
                            err,
                            slug,
                            requestUrl
                        });
                        return;
                    }
                    if (attempt < PROJECT_LOAD_MAX_ATTEMPTS) {    // transient — backoff + retry
                        await _sleep(400 * 2 ** (attempt - 1));   // 400ms, then 800ms
                        if (cancelled) return;
                        continue;
                    }
                    setLoadError(cls);                            // transient, retries exhausted
                    setLoading(false);
                    sendDiagnostics({
                        attempt,
                        retry_succeeded: false,
                        kind: cls.kind,
                        status: cls.status,
                        time_taken_ms: Date.now() - startTime,
                        err,
                        slug,
                        requestUrl
                    });
                    return;
                }
            }
        })();

        return () => {
            cancelled = true;
            controller.abort();
        };
    }, [slug, reloadNonce]);

    // Dismiss the portfolio-thumbnail action overlay when the user taps/clicks
    // anywhere outside of the active tile. Uses a deferred document listener
    // (setTimeout) so the same event that opened the overlay does not
    // immediately close it via bubbling.
    useEffect(() => {
        if (!activePortfolioThumbId) return;
        const dismiss = () => setActivePortfolioThumbId(null);
        const tid = setTimeout(() => document.addEventListener("click", dismiss), 0);
        return () => {
            clearTimeout(tid);
            document.removeEventListener("click", dismiss);
        };
    }, [activePortfolioThumbId]);

    // Auth restoration / prefill on mount.
    //
    // Issue 1: every project submission link must ALWAYS begin on the landing
    // page and authenticate BEFORE any profile data is fetched. A GLOBAL
    // cross-project session (talentgram_portal_email / talentgram_google_*
    // left over from another project) must NEVER auto-unlock the gate or
    // auto-load a profile here. Only a signal proving authentication happened
    // FOR THIS project is honoured:
    //   • a per-slug Google auth (GoogleCallback writes `tg_google_done_<slug>`)
    //   • the per-slug JWT/ATK submission session (handled by the resume effects)
    useEffect(() => {
        // Admin Mode never uses Google Sign-In, the ?email= deep-link, or any
        // localStorage-keyed identity signal — the talent is already known
        // from the Pipeline card (adminTalentId), and reading these stale
        // per-slug keys here could hijack the session with a DIFFERENT
        // talent's leftover browser state if the admin previously tested the
        // public link on this same slug.
        if (adminMode) return;
        const urlParams = new URLSearchParams(window.location.search);
        const queryEmail = urlParams.get("email");

        // Google Sign-In — only when the OAuth round-trip completed for THIS
        // project. Existing talents WITH a submission are restored by the
        // JWT/ATK resume effects; the branches below cover the "just
        // authenticated, no submission yet" and "new talent" cases.
        const googleDone = localStorage.getItem(`tg_google_done_${slug}`);
        const googleEmail = localStorage.getItem("talentgram_google_email");
        if (googleDone && googleEmail) {
            const profileDataStr = localStorage.getItem("talentgram_google_profile_data");
            setForm((f) => ({ ...f, email: f.email || googleEmail }));
            setPrefillEmail(googleEmail);
            setEmailGateUnlocked(true);
            setEmailVerified(true);

            if (profileDataStr) {
                // Existing Google-authenticated talent — auth is proven, so
                // fetch the full profile (the portal token is attached
                // automatically) and populate media/video.
                setIsReturningTalent(true);
                (async () => {
                    try {
                        const { data } = await axios.get(
                            `/public/prefill?email=${encodeURIComponent(googleEmail)}`,
                        );
                        if (data && data.first_name) {
                            populatePrefillData(data);
                            setPrefillSuggestion({ data });
                            setPrefillTried(true);
                        } else {
                            populatePrefillData(JSON.parse(profileDataStr));
                        }
                    } catch (e) {
                        console.error("Auto prefill lookup failed:", e);
                    }
                })();
            } else {
                // New Google-authenticated talent — prefill identity only.
                const first = localStorage.getItem("talentgram_google_first_name") || "";
                const last = localStorage.getItem("talentgram_google_last_name") || "";
                setForm((f) => ({
                    ...f,
                    first_name: f.first_name || first,
                    last_name: f.last_name || last,
                }));
                const onboardKey = `tg_onboard_shown_${slug}`;
                if (!localStorage.getItem(onboardKey)) {
                    toast.success("Welcome to Talentgram! Let's create your profile");
                    localStorage.setItem(onboardKey, "true");
                }
            }
            return;
        }

        // Deep-link ?email=<addr> — pre-fill the LANDING email field and, for a
        // returning talent, send the OTP so the verification step appears. The
        // gate stays LOCKED: authentication must precede any profile load.
        if (queryEmail && !emailGateUnlocked && !gatewayEmail) {
            const formatted = queryEmail.trim().toLowerCase();
            setGatewayEmail(formatted);
            (async () => {
                try {
                    const { data } = await axios.get(
                        `/public/prefill?email=${encodeURIComponent(formatted)}`,
                    );
                    if (data && data.exists) {
                        try {
                            await sendOtp(formatted);
                            setOtpSent(true);
                            toast.message("Welcome back! Please verify your email", {
                                description: "We've sent a 6-digit code to load your profile.",
                            });
                        } catch (otpErr) {
                            toast.error(formatErrorDetail(otpErr, "Verification required to continue."));
                        }
                    }
                    // New talent with ?email= : leave them on the landing email step.
                } catch (e) {
                    console.error("Auto prefill lookup failed:", e);
                }
            })();
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [slug]);


    // Branded page title — replaces the raw slug-based title users used to
    // see in the browser tab. Shape: "Talentgram | <Brand> Audition".
    useEffect(() => {
        const prev = document.title;
        const brand = (project?.brand_name || "").trim();
        document.title = brand
            ? `Talentgram | ${brand} Audition`
            : "Talentgram | Audition";
        return () => {
            document.title = prev;
        };
    }, [project?.brand_name]);

    // Resume submission
    useEffect(() => {
        if (!saved?.token || !saved?.id) return;
        (async () => {
            try {
                const { data } = await axios.get(
                    `/public/submissions/${saved.id}`,
                    { headers: { Authorization: `Bearer ${saved.token}` } },
                );
                applySubmissionResponse(data);
                if (data.form_data) {
                    setForm((f) => mergeResumedFormData(f, data.form_data));
                }
            } catch {
                if (!adminMode) localStorage.removeItem(LS_KEY(slug));
                setSaved(null);
            }
        })();
    }, [saved, slug]);

    // Admin Mode ("Upload on Behalf") bootstrap — mints an attributed
    // submitter session for `adminTalentId` on `adminProjectId` via the
    // admin-authed start endpoint, then feeds it into the SAME `saved` state
    // the talent-facing JWT resume effect above already watches — so the
    // rest of this 5,000-line component (every form-patch call, every media
    // call, finalize) runs completely unmodified regardless of which flow
    // minted the token. Never touches localStorage: this page runs in the
    // ADMIN's browser, where one admin may work several different talents'
    // on-behalf submissions for the same project slug in one session, and
    // the existing localStorage keys are namespaced only by slug — reusing
    // them here would let a second talent's session read/clobber the
    // first's leftover draft. The server-persisted draft is already the
    // source of truth in Admin Mode, so localStorage adds nothing.
    useEffect(() => {
        if (!adminMode) return;
        if (!adminProjectId || !adminTalentId) {
            setAdminBootstrapError("Missing project or talent — reopen this from the Pipeline card.");
            setAdminBootstrapping(false);
            return;
        }
        let cancelled = false;
        (async () => {
            try {
                const { data } = await adminApi.post(
                    `/projects/${adminProjectId}/talents/${adminTalentId}/submissions/admin-start`,
                );
                if (cancelled) return;
                setSaved({ id: data.id, token: data.token });
                setAdminTalentName(data.talent_name || null);
            } catch (err) {
                if (cancelled) return;
                setAdminBootstrapError(
                    formatErrorDetail(err, "Could not start this submission. Please try again.")
                );
            } finally {
                if (!cancelled) setAdminBootstrapping(false);
            }
        })();
        return () => { cancelled = true; };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [adminMode, adminProjectId, adminTalentId]);

    // Persistent ATK-based resume — runs after JWT resume path so it only
    // fires when `saved` is null (no valid JWT in localStorage). Uses the
    // long-lived opaque access_token stored in LS_ATK_KEY to call the
    // /public/projects/{slug}/submission/me endpoint and restore the full
    // submission state without re-entering any identity details.
    useEffect(() => {
        if (adminMode) return;      // Admin Mode never uses the ATK/localStorage resume path.
        if (saved) return;          // JWT resume already handled this session
        if (atkTriedRef.current) return; // already attempted once this mount
        const atk = localStorage.getItem(LS_ATK_KEY(slug));
        if (!atk) return;
        atkTriedRef.current = true;
        (async () => {
            try {
                const { data } = await axios.get(
                    `/public/projects/${slug}/submission/me`,
                    { params: { atk } },
                );
                if (data?.id) {
                    // Restore saved state — use the ATK as the bearer token
                    // (decode_submitter now supports opaque ATK lookup)
                    const next = { id: data.id, token: atk };
                    localStorage.setItem(LS_KEY(slug), JSON.stringify(next));
                    setSaved(next);
                    applySubmissionResponse(data);
                    if (data.form_data) {
                        setForm((f) => mergeResumedFormData(f, data.form_data));
                    }
                    // Restore the email into the form so it's visible on
                    // the dashboard header and any validation checks pass.
                    if (data.talent_email) {
                        setForm((f) => ({ ...f, email: data.talent_email }));
                    }
                    setEmailGateUnlocked(true);
                }
            } catch {
                // Token invalid or submission deleted — clear stale ATK.
                localStorage.removeItem(LS_ATK_KEY(slug));
            }
        })();
    // `saved` in deps: if JWT resume runs first and sets saved→null (expired),
    // this effect re-evaluates and runs the ATK check as a fallback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [slug, saved]);

    const computedAge = useMemo(() => {
        if (form.overrideAge && form.submitted_age_override) {
            return parseInt(form.submitted_age_override, 10) || null;
        }
        return calcAge(form.dob) ?? (form.age ? parseInt(form.age, 10) : null);
    }, [form.dob, form.age, form.overrideAge, form.submitted_age_override]);

    const authCfg = useMemo(
        () =>
            saved?.token
                ? { headers: { Authorization: `Bearer ${saved.token}` } }
                : {},
        [saved],
    );

    // Every GET/resume/from-library response is shaped by the backend's
    // build_talent_submission_view(), which now always includes a live
    // `library_media` — apply it alongside `submission` wherever a response
    // is applied so the picker stays in sync. A no-op wherever the response
    // doesn't carry the field (e.g. the bare start_submission response).
    const applySubmissionResponse = (data) => {
        setSubmission(data);
        if (data && Object.prototype.hasOwnProperty.call(data, "library_media")) {
            setLibraryMedia(data.library_media || []);
        }
        if (data && Object.prototype.hasOwnProperty.call(data, "pending_media_consent")) {
            setPendingMediaConsent(data.pending_media_consent || []);
        }
    };

    // Sprint 1 — parallel cm labels for the height options. Each entry keeps the
    // canonical feet/inches `value` and only adds a centimetre display label, so
    // selecting in either unit writes the SAME stored string.
    const HEIGHT_CM_OPTIONS = useMemo(
        () =>
            HEIGHT_OPTIONS.map((h) => {
                const m = h.match(/(\d+)'(\d+)"/);
                const ft = m ? parseInt(m[1], 10) : 0;
                const inch = m ? parseInt(m[2], 10) : 0;
                const cm = Math.round((ft * 12 + inch) * 2.54);
                return { value: h, cm };
            }),
        [],
    );

    // Every field-level requirement below comes from the Requirement Engine
    // (experience.missingRequirements — lib/requirementEngine.js, driven
    // entirely by project.submission_requirements). No field is ever
    // hardcoded as required here: what's required is 100% admin/project
    // configuration, never frontend logic. The only two checks that stay
    // hardcoded are the email-ownership gate itself (email + emailGateUnlocked)
    // — that's authentication, not a submission requirement, so it isn't
    // something the Requirement Engine models.
    //
    // Phase 2 (new-talent flow) — the ONLY thing required to create the
    // submission draft or start an upload is a confirmed email. Profile/
    // Skills now come AFTER Project Questions/Media in the wizard order
    // (see wizardSteps.js), so gating draft-creation on validateStep1()/
    // validateForm() (which check profile-section fields) would
    // incorrectly block a new talent from uploading media before they've
    // even reached the Profile step — the exact "select file -> upload
    // immediately" promise this whole flow exists for. The backend's own
    // start_submission only requires name+email (name may be blank), so
    // this mirrors that, not the older fuller check those two still do
    // (correctly) for the Profile step's own Next-button validation.
    const canCreateDraft = () => {
        if (!form.email.trim()) return "Email is required";
        if (!emailGateUnlocked) return "Please complete the email step first";
        return null;
    };

    // Mobile wizard step validators — narrower than the full form so users
    // can advance after completing only the current step's fields.
    const validateStep1 = () => {
        // Same email-first rule as validateForm. Step 1 IS the talent-
        // details step, so without a confirmed email we have nothing to
        // validate against.
        if (!form.email.trim()) return "Email is required";
        if (!emailGateUnlocked) return "Please complete the email step first";
        const missing = experience.missingRequirements.filter((item) => item.section === "profile");
        if (missing.length > 0) return `${missing[0].label} is required`;
        return null;
    };

    // Persist a debounced draft of the form so a refresh / app-switch never
    // loses progress before the talent record is created on the backend.
    // Also drives the subtle "Saving… / Saved" autosave indicator (Sprint 1).
    useEffect(() => {
        // Admin Mode: the server-persisted draft is the source of truth (the
        // submission doc is already saved via admin-start + every field
        // patch), so localStorage draft persistence adds nothing and risks
        // cross-talent contamination — see the admin-start bootstrap effect.
        if (adminMode) return;
        const first = !draftMountRef.current;
        if (first) draftMountRef.current = true;
        else setSaveStatus("saving");
        const t = setTimeout(() => {
            try {
                localStorage.setItem(LS_DRAFT_KEY(slug), JSON.stringify(form));
            } catch (e) { console.error(e); }
            if (!first) setSaveStatus("saved");
        }, 1200);
        return () => clearTimeout(t);
    }, [form, slug]);

    // Revert the "Saved" badge back to its resting state after a beat so the
    // indicator stays subtle rather than permanently announcing itself.
    useEffect(() => {
        if (saveStatus !== "saved") return;
        const t = setTimeout(() => setSaveStatus("idle"), 2500);
        return () => clearTimeout(t);
    }, [saveStatus]);
    // Convenience wrapper that returns a boolean (vs the form-handler version).
    async function startSubmissionDirect() {
        const err = canCreateDraft();
        if (err) {
            toast.error(err);
            return null;
        }
        setStarting(true);
        try {
            const { data } = await axios.post(
                `/public/projects/${slug}/submission`,
                {
                    name: `${form.first_name} ${form.last_name}`.trim(),
                    email: form.email.trim().toLowerCase(),
                    phone: form.phone || null,
                    alternate_contact_number: form.alternate_contact_number || null,
                    // SubmissionStartIn only persists profile fields nested
                    // under form_data (see startSubmission's payload below) —
                    // top-level extras are silently dropped by the backend
                    // model, which used to leave a returning talent's
                    // submission with an empty form_data and a confusing
                    // "First and Last Name are required" error on finalize.
                    form_data: {
                        first_name: form.first_name,
                        last_name: form.last_name,
                        alternate_contact_number: form.alternate_contact_number || "",
                        dob: form.dob || null,
                        age: computedAge != null ? String(computedAge) : form.age || "",
                        height: form.height,
                        location: form.location,
                        gender: form.gender || "",
                        ethnicity: form.ethnicity || "",
                        instagram_handle: form.instagram_handle || "",
                        instagram_followers: form.instagram_followers || "",
                        bio: form.bio || "",
                        work_links: form.work_links || [],
                        skills: form.skills || [],
                        has_competitive_brand_experience: form.has_competitive_brand_experience,
                        competitive_brand: form.has_competitive_brand_experience ? (form.competitive_brand || "") : "",
                        availability: form.availability,
                        budget: form.budget,
                        custom_answers: form.custom_answers || {},
                    },
                },
            );
            const next = { id: data.id, token: data.token };
            localStorage.setItem(LS_KEY(slug), JSON.stringify(next));
            // Persist the long-lived access_token separately so the talent
            // can resume their submission from any browser/device as long as
            // this localStorage key survives (much longer than the 3-day JWT).
            if (data.access_token) {
                localStorage.setItem(LS_ATK_KEY(slug), data.access_token);
            }
            setSaved(next);
            applySubmissionResponse(data);
            setCollapsedSections((prev) => ({ ...prev, uploads: false }));
            toast.success("✓ Details saved successfully.");
            return next;
        } catch (e) {
            // P0-2: ownership now required when a record already exists for the
            // email. Route the returning talent through the one-time-code flow.
            if (e?.response?.status === 403) {
                const verifyEmail = (form.email || "").trim().toLowerCase();
                setEmailGateUnlocked(false);
                setGatewayEmail(verifyEmail);
                try {
                    await sendOtp(verifyEmail);
                    setOtpSent(true);
                    toast.message("Please verify your email", {
                        description: "We've sent a one-time code to continue.",
                    });
                } catch (otpErr) {
                    toast.error(
                        formatErrorDetail(otpErr, "Please verify your email to continue."),
                    );
                }
                return null;
            }
            toast.error(formatErrorDetail(e, "Could not save profile"));
            return null;
        } finally {
            setStarting(false);
        }
    }

    // Email-first auto-fill (Phase 1).
    //
    // On email blur we hit `/api/public/prefill`. If we get a hit, we DO NOT
    // silently overwrite — we surface a small inline confirmation card with
    // "Use this" / "Edit manually" so the talent stays in control.
    //
    // Strict scope (per Phase 1 spec):
    //   ✅ Auto-fill: first_name, last_name, age, dob, height, phone, location, instagram_*
    //   ❌ NEVER auto-fill: any media (intro, takes, images), previous form
    //      answers, custom-questions, availability/budget/commission.
    //
    // The user can re-trigger prefill by re-entering the email — we only
    // auto-trigger ONCE per email value.
    function populatePrefillData(data) {
        if (!data) return;
        setForm((f) => ({
            ...f,
            first_name: f.first_name || data.first_name || "",
            last_name: f.last_name || data.last_name || "",
            phone: f.phone || data.phone || "",
            age: f.age || (data.age != null ? String(data.age) : ""),
            dob: f.dob || data.dob || "",
            height: f.height || data.height || "",
            location: (f.location && f.location.length) ? f.location : (data.location || []),
            gender: f.gender || data.gender || "",
            ethnicity: f.ethnicity || data.ethnicity || "",
            bio: f.bio || data.bio || "",
            instagram_handle: f.instagram_handle || data.instagram_handle || "",
            instagram_followers:
                f.instagram_followers || data.instagram_followers || "",
            work_links:
                f.work_links && f.work_links.length
                    ? f.work_links
                    : (data.work_links || []),
            skills:
                f.skills && f.skills.length
                    ? f.skills
                    : (data.skills || []),
        }));
        // Talent Profile Migration, Phase 3: reusable media is no longer
        // auto-injected into the submission. It's surfaced as `libraryMedia`
        // for the "My Saved Media" picker — the talent explicitly chooses
        // what applies to THIS project (see toggleLibraryMedia). This is
        // superseded the moment the draft actually loads/resumes (the
        // server's `library_media`, via applySubmissionResponse), so this is
        // just the earliest-possible (pre-submission-creation) population.
        if (data.prefill_media) {
            setLibraryMedia(data.prefill_media || []);

            // Debugging requirement:
            console.log("[DEBUG] Talent found");
            console.log("[DEBUG] Profile loaded");
            console.log(`[DEBUG] Skills loaded count: ${(data.skills || []).length}`);
            console.log(`[DEBUG] Images loaded count: ${(data.prefill_media || []).filter(m => m.category !== "intro_video" && m.category !== "video").length}`);
            console.log(`[DEBUG] Videos loaded count: ${(data.prefill_media || []).filter(m => m.category === "intro_video" || m.category === "video").length}`);
            console.log(`[DEBUG] Portfolio categories loaded: ${Array.from(new Set((data.prefill_media || []).map(m => m.category))).join(", ")}`);
        }
    }

    // Returning-talent starting step: a known Talent Profile normally lets
    // the wizard skip straight to Step 3 (Project Info) — but "known
    // profile" doesn't mean "satisfies THIS project's requirements". A
    // project can require a field (e.g. Height) the talent's existing
    // profile never captured, and Step 3's own Next-button validation only
    // checks Step 3's own section, so an unconditional jump to 3 would let
    // the talent sail past a field they need to fill, only to be bounced
    // back from Step 4's Submit with no idea why. Computes requirements
    // directly against the incoming prefill data (mirrors populatePrefillData's
    // own merge-if-empty rule) rather than reading `form`/`experience`,
    // since both still reflect the pre-prefill render when this runs.
    const computeReturningTalentStartStep = useCallback((prefillData, emailOverride) => {
        if (!project || !prefillData) return 3;
        const mergedForm = {
            ...form,
            // `email` is deliberately NOT part of the prefill response (the
            // caller already knows it — that's how it looked the talent up)
            // and `form.email` is still the pre-update value here regardless
            // (setForm's update hasn't committed yet when callers compute
            // this synchronously right after calling it) — without this,
            // any project that requires email always evaluates it as
            // missing and bounces a fully-known returning talent back to
            // Step 1, defeating the entire "skip to Project Questions"
            // point of this function. Callers pass the email they already
            // resolved (from the prefill lookup, OTP verify, etc.).
            email: emailOverride || form.email || prefillData.email || "",
            first_name: form.first_name || prefillData.first_name || "",
            last_name: form.last_name || prefillData.last_name || "",
            phone: form.phone || prefillData.phone || "",
            age: form.age || (prefillData.age != null ? String(prefillData.age) : ""),
            dob: form.dob || prefillData.dob || "",
            height: form.height || prefillData.height || "",
            location: (form.location && form.location.length) ? form.location : (prefillData.location || []),
            gender: form.gender || prefillData.gender || "",
            ethnicity: form.ethnicity || prefillData.ethnicity || "",
            bio: form.bio || prefillData.bio || "",
            instagram_handle: form.instagram_handle || prefillData.instagram_handle || "",
            instagram_followers: form.instagram_followers || prefillData.instagram_followers || "",
            work_links: (form.work_links && form.work_links.length) ? form.work_links : (prefillData.work_links || []),
            skills: (form.skills && form.skills.length) ? form.skills : (prefillData.skills || []),
        };
        const items = computeRequirementItems({ project, form: mergedForm, submission: null, isReturningTalent: true });
        const earlyMissing = items.find((item) =>
            (item.section === "profile" || item.section === "skills") &&
            item.requirement === REQUIREMENT_TIERS.REQUIRED &&
            !item.satisfied
        );
        return earlyMissing ? stepForSection(earlyMissing.section) : stepForSection("projectQuestions");
    }, [project, form]);

    // "UPLOAD TEST" — the dominant CTA on Project Info. For a returning
    // talent recognized by this browser's portal session token, this
    // silently recognizes them (a live, authenticated `GET /portal/profile`
    // call — real bearer-token proof, not a stale/unauthenticated flag) and
    // jumps straight to Project Questions, skipping Basic Profile/Skills.
    //
    // Deliberately differs from the pre-8cfb1b5 bug this guards against
    // (see "Issue 1" comment on `emailGateUnlocked`'s initializer above):
    // that bug auto-unlocked the gate on mere page load from a leftover,
    // unauthenticated localStorage flag, with no user action and no visible
    // UI, silently sending an OTP behind the scenes. This only runs from an
    // explicit click, verifies a real session server-side before trusting
    // it, and never sends an OTP for the token-backed path (the token IS
    // the proof) — so the "stuck with a hidden pending OTP" failure mode
    // this fixed cannot recur here. If the token is missing or invalid, this
    // falls through to the existing Step A OTP/Google UI unchanged.
    const scrollToTalentDetails = () => {
        document.querySelector('[data-testid="talent-details-section"]')?.scrollIntoView({ behavior: "smooth", block: "start" });
    };

    // The section this scrolls to doesn't exist in the DOM until
    // `talentDetailsRevealed` flips true (see its declaration above), so the
    // reveal has to commit and re-render before `scrollIntoView` can find
    // it — hence the setTimeout rather than calling scrollToTalentDetails
    // directly.
    const revealAndScrollToTalentDetails = () => {
        setTalentDetailsRevealed(true);
        setTimeout(scrollToTalentDetails, 50);
    };

    // Shared silent-recognition core — verifies a real portal-token bearer
    // proof server-side (via /public/prefill) and, on success, performs
    // every state update needed to treat this as an already-authenticated
    // returning-talent session (no OTP: the token IS the proof of
    // ownership). Used both by the "UPLOAD TEST" CTA (no candidate email —
    // reads whatever token/email pair is in localStorage) and by the Step A
    // typed-email lookup (candidateEmail = what the talent just typed), so a
    // talent who already holds a valid trusted session for that exact email
    // never gets funneled into the untrusted, OTP-required Step B card just
    // because they typed their email instead of clicking Upload Test first.
    // Returns true on successful silent unlock, false otherwise — callers
    // decide what "otherwise" means (reveal Step A, or fall through to the
    // self-reported-match-then-OTP path).
    const attemptSilentRecognition = useCallback(async (candidateEmail) => {
        const token = typeof window !== "undefined" ? localStorage.getItem(PORTAL_TOKEN_KEY) : null;
        // talentgram_portal_email is always written/cleared alongside the
        // portal token itself (see handleVerifyOtp / the portalApi 401
        // interceptor in lib/api.js) — the two are kept in sync as a pair.
        const tokenEmail = typeof window !== "undefined" ? localStorage.getItem("talentgram_portal_email") : null;
        if (!tokenAuthenticatesEmail({ tokenEmail, candidateEmail })) return false;
        const email = (candidateEmail || tokenEmail || "").trim().toLowerCase();
        if (!shouldAttemptSilentRecognition({ adminMode, emailGateUnlocked, token, email })) return false;
        try {
            // /public/prefill (not /portal/profile) deliberately — it's the
            // one canonical prefill builder this page's populatePrefillData/
            // computeReturningTalentStartStep already expect (first_name/
            // last_name split, plus prefill_media for the Media step's
            // auto-reuse) and it accepts the same bearer portal token via
            // verify_email_ownership, so recognition still requires zero OTP.
            const { data } = await portalApi.get(`/public/prefill?email=${encodeURIComponent(email)}`);
            if (!data || !data.first_name) return false; // token valid, but no matching/complete profile
            populatePrefillData(data);
            setPrefillSuggestion({ data });
            setPrefillTried(true);
            setRecognizedIdentity(buildRecognizedIdentity(data));
            setEmailGateUnlocked(true);
            setEmailVerified(true);
            setIsReturningTalent(true);
            setForm((f) => ({ ...f, email: f.email || email }));
            setPrefillEmail(email);
            setCurrentStep(computeReturningTalentStartStep(data, email));
            toast.success(`Welcome back, ${data.first_name}!`);
            return true;
        } catch (error) {
            // 401 → the portalApi interceptor already cleared the stale
            // token; any other failure just means recognition didn't work.
            console.error("Silent recognition failed:", error);
            return false;
        }
    }, [adminMode, emailGateUnlocked, computeReturningTalentStartStep]);

    // Trusted-device recognition — the cookie-based analog of
    // attemptSilentRecognition above. Tried FIRST: unlike the portal token
    // (localStorage, matched against a client-supplied candidate email), the
    // trusted-device cookie is HttpOnly (never touched by this file's JS)
    // and proves identity on its own, so it works the moment the talent
    // opens ANY project link on a device that's already trusted — no email
    // typing, no per-project state to keep in sync. Falls through to the
    // existing portal-token path on any failure (missing/expired/revoked
    // cookie, or admin mode where this is skipped entirely).
    const attemptTrustedDeviceRecognition = useCallback(async () => {
        if (adminMode || emailGateUnlocked) return false;
        try {
            const { data } = await axios.get("/public/trusted-device/recognize");
            if (!data || !data.first_name) return false;
            populatePrefillData(data);
            setPrefillSuggestion({ data });
            setPrefillTried(true);
            setRecognizedIdentity(buildRecognizedIdentity(data));
            setEmailGateUnlocked(true);
            setEmailVerified(true);
            setIsReturningTalent(true);
            setForm((f) => ({ ...f, email: f.email || data.email || "" }));
            if (data.email) setPrefillEmail(data.email);
            setCurrentStep(computeReturningTalentStartStep(data, data.email));
            toast.success(`Welcome back, ${data.first_name}!`);
            return true;
        } catch (error) {
            // 401 (no/invalid/expired cookie) is the expected, silent case —
            // any other failure just means recognition didn't work.
            return false;
        }
    }, [adminMode, emailGateUnlocked, computeReturningTalentStartStep]);

    const handleUploadTestClick = useCallback(async () => {
        setRecognizing(true);
        try {
            const trusted = await attemptTrustedDeviceRecognition();
            if (trusted) return;
            const ok = await attemptSilentRecognition();
            if (!ok) revealAndScrollToTalentDetails();
        } finally {
            setRecognizing(false);
        }
    }, [attemptTrustedDeviceRecognition, attemptSilentRecognition]);

    const tryPrefill = async () => {
        if (saved) return; // submission already started — too late
        const email = (form.email || "").trim().toLowerCase();
        if (!email || !email.includes("@")) return;
        if (email === prefillEmail && prefillTried) return; // already tried
        setPrefillEmail(email);
        setPrefillTried(true);
        try {
            const { data } = await axios.get(
                `/public/prefill?email=${encodeURIComponent(email)}`,
            );
            if (data && data.exists) {
                // Talent exists but we are unauthenticated. Trigger OTP send and popup verification modal immediately.
                setPrefillSuggestion(null);
                setGatewayEmail(email);
                try {
                    await sendOtp(email);
                    setOtpSent(true);
                    toast.message("Welcome back! Please verify your email", {
                        description: "We've sent a 6-digit code to pre-fill your profile.",
                    });
                } catch (otpErr) {
                    toast.error(formatErrorDetail(otpErr, "Verification required to pre-fill."));
                    setEmailGateUnlocked(true);
                }
                return;
            }
            if (!data || !data.first_name) {
                // New talent — quietly unlock the rest of the form.
                setPrefillSuggestion(null);
                setEmailGateUnlocked(true);
                return;
            }
            // Returning talent — auto-load and open form immediately
            populatePrefillData(data);
            setPrefillSuggestion({ data });
            setEmailGateUnlocked(true);
            toast.success(`Welcome back, ${data.first_name}`);
        } catch {
            // 429 (rate-limited) or network — fail silently AND unlock so
            // the user isn't blocked behind a transient network error.
            setEmailGateUnlocked(true);
        }
    };

    const handleUseAnotherEmail = () => {
        // Revoke THIS device's trusted-device cookie only — other devices
        // already trusted for this talent are untouched. Best-effort/fire-
        // and-forget: the rest of this reset must not wait on or fail
        // because of a network hiccup.
        axios.post("/public/trusted-device/forget").catch(() => {});
        localStorage.removeItem("talentgram_portal_email");
        localStorage.removeItem("talentgram_google_email");
        localStorage.removeItem("talentgram_google_first_name");
        localStorage.removeItem("talentgram_google_last_name");
        localStorage.removeItem("talentgram_google_avatar");
        localStorage.removeItem("talentgram_google_profile_data");
        localStorage.removeItem(`tg_google_done_${slug}`);
        const onboardKey = `tg_onboard_shown_${slug}`;
        localStorage.removeItem(onboardKey);
        
        setForm({
            first_name: "",
            last_name: "",
            email: "",
            phone: "",
            alternate_contact_number: "",
            dob: "",
            age: "",
            overrideAge: false,
            submitted_age_override: "",
            height: "",
            location: [],
            gender: "",
            ethnicity: "",
            instagram_handle: "",
            instagram_followers: "",
            bio: "",
            work_links: [],
            skills: [],
            has_competitive_brand_experience: null,
            competitive_brand: "",
            availability: { status: "", note: "" },
            budget: { status: "", value: "" },
            commission: project ? (project.commission_percent || "") : "",
            custom_answers: {},
        });
        setPrefillEmail("");
        setPrefillTried(false);
        setPrefillSuggestion(null);
        setEmailGateUnlocked(false);
        setGatewayRecognition(null);
        setGatewayEmail("");
        setRecognizedIdentity(null);
        setIsReturningTalent(false);
        setFinalStepReached(false);
        clearFinalStepReached(slug);
        toast.info("Please enter your email to proceed.");
    };

    const handleGoogleLogin = () => {
        window.location.href = buildGoogleAuthUrl(slug);
    };

    const handleInlineLookup = async (e) => {
        if (e) e.preventDefault();
        if (gatewayLoading) return;
        const trimmedEmail = gatewayEmail.trim().toLowerCase();
        if (!trimmedEmail || !trimmedEmail.includes("@")) {
            toast.error("Please enter a valid email address.");
            return;
        }

        setGatewayLoading(true);
        try {
            // Trusted-session check FIRST — if this browser already holds a
            // valid portal token that authenticates THIS exact email,
            // that's real server-verified proof of ownership, identical to
            // what clicking "UPLOAD TEST" itself would have found. Treat it
            // the same way and skip Step B/OTP entirely (see
            // attemptSilentRecognition — this is what keeps an
            // already-authenticated talent from being asked to re-verify
            // just because they typed their email instead of clicking
            // Upload Test). Only when there's no trusted token for this
            // email does the self-reported-match-then-OTP flow below apply.
            if (await attemptSilentRecognition(trimmedEmail)) return;

            // Pre-auth recognition first — if we already know this talent,
            // show the "Is this you?" card (Step B) instead of jumping
            // straight into OTP. Only the minimal non-sensitive fields
            // (name/email/image_url) come back from this unauthenticated
            // endpoint; OTP is still required afterward (handleInlineContinue)
            // before anything is unlocked.
            if (!gatewayRecognition) {
                try {
                    const { data } = await axios.post("/portal/lookup", { email: trimmedEmail });
                    const classification = classifyPortalLookupResult(data);
                    if (classification === "known") {
                        setGatewayRecognition({ ...data.talent, email: trimmedEmail });
                        return;
                    }
                    if (classification === "new") {
                        // Confirmed brand-new talent (not a lookup failure —
                        // the backend positively found no record). Nothing
                        // to prove ownership of yet, so unlock immediately
                        // with NO OTP friction — matches the new-talent
                        // flow's "authenticate near the end, not the start"
                        // requirement. Real proof of email ownership is
                        // deferred to the Almost-Done step (emailVerified),
                        // not required here.
                        setForm((f) => ({ ...f, email: trimmedEmail }));
                        setPrefillEmail(trimmedEmail);
                        setEmailGateUnlocked(true);
                        setCurrentStep(stepForSection("projectQuestions"));
                        return;
                    }
                    // "unknown" (ambiguous/malformed response) — fall
                    // through to OTP, same as an outright lookup failure.
                } catch (lookupErr) {
                    console.error("Portal lookup failed:", lookupErr);
                    // Don't block OTP send on a lookup failure — fall through.
                }
            }
            await sendOtp(trimmedEmail);
            setOtpSent(true);
            toast.success("Verification code sent!");
        } catch (error) {
            console.error("OTP send error:", error);
            toast.error(formatErrorDetail(error, "Failed to send verification code. Please try again."));
        } finally {
            setGatewayLoading(false);
        }
    };

    // `onSuccess` is an optional extra callback fired after a successful
    // verify (state already committed) — used by the Almost-Done card
    // (Task 12) to immediately continue into finalize() once identity is
    // confirmed, without duplicating this whole function.
    const handleVerifyOtp = async (e, { onSuccess } = {}) => {
        if (e) e.preventDefault();
        if (otpLoading) return;
        const code = otpValue.trim();
        if (code.length !== 6 || !/^\d+$/.test(code)) {
            toast.error("Please enter a valid 6-digit verification code.");
            return;
        }

        setOtpLoading(true);
        try {
            const trimmedEmail = gatewayEmail.trim().toLowerCase();
            const data = await verifyOtp({ email: trimmedEmail, otp: code, slug });

            if (data.existing) {
                // Two different "returning" cases need different starting
                // steps: an in-progress submission for THIS project is a
                // resume (go back to wherever they left off); a known Talent
                // Profile with no submission yet for this project is the
                // wizard's "skip Profile/Skills, start on Project Info" case.
                const resumingSubmission = !!(data.token && data.submission_id);
                if (resumingSubmission) {
                    const ref = { id: data.submission_id, token: data.token };
                    localStorage.setItem(`tg_submission_${slug}`, JSON.stringify(ref));
                    localStorage.setItem(`tg_atk_${slug}`, data.token);
                    setSaved(ref);
                    setCurrentStep(readStep(slug) || 1);
                    toast.success("Welcome back!");
                } else {
                    toast.success("Welcome back!");
                }
                if (data.talent) {
                    populatePrefillData(data.talent);
                    setPrefillSuggestion({ data: data.talent });
                    setPrefillTried(true);
                    setIsReturningTalent(true);
                    if (!resumingSubmission) setCurrentStep(computeReturningTalentStartStep(data.talent, trimmedEmail));
                }
            } else {
                toast.success("Successfully authenticated. Welcome to Talentgram!");
            }

            // OTP proved ownership — persist the portal session token (Path B).
            persistPortalToken(data);
            localStorage.setItem("talentgram_portal_email", trimmedEmail);
            setForm((f) => ({ ...f, email: trimmedEmail }));
            setPrefillEmail(trimmedEmail);
            setEmailGateUnlocked(true);
            setEmailVerified(true);
            setOtpSent(false);
            onSuccess?.();
        } catch (error) {
            console.error("OTP verify error:", error);
            toast.error(formatErrorDetail(error, "Invalid or expired verification code."));
        } finally {
            setOtpLoading(false);
        }
    };

    const handleResendOtp = async () => {
        if (otpResending) return;
        const trimmedEmail = gatewayEmail.trim().toLowerCase();
        setOtpResending(true);
        try {
            await sendOtp(trimmedEmail);
            toast.success("Verification code resent.");
        } catch (error) {
            console.error("OTP resend error:", error);
            toast.error(formatErrorDetail(error, "Failed to resend code. Please try again."));
        } finally {
            setOtpResending(false);
        }
    };

    // ALMOST DONE — new-talent end-of-flow authentication. The email was
    // already collected with zero friction back at "UPLOAD TEST"
    // (handleInlineLookup's not-found branch); this just proves ownership
    // of that SAME email via the existing OTP machinery, right before
    // submit. Reuses gatewayEmail/otpSent/handleVerifyOtp/handleResendOtp
    // verbatim — this is the identical verification flow Step A/B already
    // use, just triggered from a different position in the page.
    const handleAlmostDoneSendCode = useCallback(async () => {
        const email = (form.email || "").trim().toLowerCase();
        if (!email) return;
        setGatewayEmail(email);
        setGatewayLoading(true);
        try {
            await sendOtp(email);
            setOtpSent(true);
            toast.success("Verification code sent!");
        } catch (error) {
            console.error("OTP send error:", error);
            toast.error(formatErrorDetail(error, "Failed to send verification code. Please try again."));
        } finally {
            setGatewayLoading(false);
        }
    }, [form.email]);
    // handleAlmostDoneVerify is defined further down, right after
    // handleSubmitCtaClick (which it calls on success) — see there.

    // Step B's "Yes, that's me" — a self-reported email/name match from the
    // unauthenticated /portal/lookup call is NOT proof of ownership, so this
    // must still require OTP before anything unlocks. (Previously this
    // unlocked the form directly on a bare match with no verification at
    // all — that would have been an authentication bypass, which is almost
    // certainly why this card was never wired up live.) Real prefill/step
    // computation happens in handleVerifyOtp once the code is confirmed.
    const handleInlineContinue = async () => {
        if (!gatewayRecognition || !gatewayRecognition.email) return;
        const formatted = gatewayRecognition.email.trim().toLowerCase();
        setGatewayEmail(formatted);
        setGatewayLoading(true);
        try {
            await sendOtp(formatted);
            setOtpSent(true);
            toast.success("Verification code sent!");
        } catch (error) {
            console.error("OTP send error:", error);
            toast.error(formatErrorDetail(error, "Failed to send verification code. Please try again."));
        } finally {
            setGatewayLoading(false);
        }
    };

    const handleInlineCancel = () => {
        localStorage.removeItem("talentgram_portal_email");
        localStorage.removeItem("talentgram_google_email");
        localStorage.removeItem("talentgram_google_first_name");
        localStorage.removeItem("talentgram_google_last_name");
        localStorage.removeItem("talentgram_google_avatar");
        localStorage.removeItem("talentgram_google_profile_data");
        localStorage.removeItem(`tg_google_done_${slug}`);
        const onboardKey = `tg_onboard_shown_${slug}`;
        localStorage.removeItem(onboardKey);
        setGatewayRecognition(null);
        setGatewayEmail("");
    };

    const startSubmission = async (e) => {
        e.preventDefault();
        const err = canCreateDraft();
        if (err) {
            toast.error(err);
            return;
        }
        setStarting(true);
        try {
            const payload = {
                name: `${form.first_name} ${form.last_name}`.trim(),
                email: form.email,
                phone: form.phone || null,
                alternate_contact_number: form.alternate_contact_number || null,
                form_data: {
                    first_name: form.first_name,
                    last_name: form.last_name,
                    alternate_contact_number: form.alternate_contact_number || "",
                    dob: form.dob || null,
                    age: computedAge != null ? String(computedAge) : "",
                    height: form.height,
                    location: form.location,
                    // Phase 2 unified identity (mirrored to talent on finalize)
                    gender: form.gender || "",
                    ethnicity: form.ethnicity || "",
                    instagram_handle: form.instagram_handle || "",
                    instagram_followers: form.instagram_followers || "",
                    bio: form.bio || "",
                    work_links: form.work_links || [],
                    skills: form.skills || [],
                    has_competitive_brand_experience: project.competitive_brand_enabled
                        ? form.has_competitive_brand_experience
                        : null,
                    competitive_brand: (project.competitive_brand_enabled && form.has_competitive_brand_experience)
                        ? (form.competitive_brand || "")
                        : "",
                    availability: form.availability,
                    budget: form.budget,
                    custom_answers: form.custom_answers || {},
                },
            };
            const { data } = await axios.post(
                `/public/projects/${slug}/submission`,
                payload,
            );
            const ref = { id: data.id, token: data.token };
            localStorage.setItem(LS_KEY(slug), JSON.stringify(ref));
            // Persist the long-lived access_token so the talent can resume
            // from any browser/device using the ATK-based resume path.
            if (data.access_token) {
                localStorage.setItem(LS_ATK_KEY(slug), data.access_token);
            }
            setSaved(ref);
            setCollapsedSections((prev) => ({ ...prev, uploads: false }));
            toast.success("✓ Details saved successfully. Next step: Upload your introduction video, audition takes and portfolio images.");
        } catch (err) {
            toast.error(formatErrorDetail(err, "Failed to start"));
        } finally {
            setStarting(false);
        }
    };

    // Function declaration (not `const = async () =>`) so it's fully hoisted
    // and `goToStep` above can call it without a TDZ reference.
    async function saveForm() {
        if (!saved) return;
        try {
            await axios.put(
                `/public/submissions/${saved.id}`,
                {
                    form_data: {
                        ...form,
                        age:
                            computedAge != null
                                ? String(computedAge)
                                : form.age || "",
                    },
                },
                authCfg,
            );
        } catch (e) { console.error(e); }
    }

    const triggerUpload = async (file, category, label = null) => {
        await uploadFile(file, category, label, {
            token: saved?.token,
            endpoint: saved ? `/public/submissions/${saved.id}/upload` : null,
            onSuccess: (data) => {
                applySubmissionResponse(data);
            },
            onBeforeUpload: async () => {
                let currentSaved = saved;
                if (!currentSaved) {
                    // Admin Mode never falls back to the talent-facing start
                    // endpoint — that would create a second, non-admin-
                    // attributed submission instead of using the one the
                    // admin-start bootstrap effect already created/resumed.
                    // If `saved` isn't set yet, bootstrap is still in flight
                    // (or failed) — surface that instead of racing it.
                    if (adminMode) {
                        toast.error(adminBootstrapping ? "Still starting this submission — please wait a moment and try again." : "Could not start this submission. Please reload and try again.");
                        return null;
                    }
                    const err = canCreateDraft();
                    if (err) {
                        toast.error(err);
                        return null;
                    }
                    const next = await startSubmissionDirect();
                    if (!next) return null;
                    currentSaved = next;
                }
                return {
                    token: currentSaved.token,
                    endpoint: `/public/submissions/${currentSaved.id}/upload`
                };
            }
        });
    };

    const patchTakeLabel = async (mid, label) => {
        try {
            const { data } = await axios.patch(
                `/public/submissions/${saved.id}/media/${mid}`,
                { label },
                authCfg,
            );
            applySubmissionResponse(data);
        } catch (err) {
            toast.error(formatErrorDetail(err, "Could not rename"));
        }
    };

    // Media derivations (initialised here before uploadImages so the
    // `allImages.length` read inside it never sees an undefined value).
    // `submission` is React state — it's null on first render, so `media`
    // safely falls back to []. Kept as `const` so re-renders pick up the
    // latest submission state automatically.
    const media = submission?.media || [];
    // Phase 2 — portfolio images come in 3 flavours: generic, indian look,
    // western look. They share the MAX_IMAGES bucket so the talent doesn't
    // exceed the 8-image cap by splitting categories.
    const images = media.filter((m) => m.category === "image");
    const indianImages = media.filter((m) => m.category === "indian");
    const westernImages = media.filter((m) => m.category === "western");
    // Admin Mode — additional look categories (Selfie/Profiles/Full Length/
    // Side Profile). Each is its own independent bucket (own per-category
    // cap), same as indian/western above — not counted into `allImages`,
    // matching how indian/western are already excluded from the generic
    // portfolio bucket.
    const selfieImages = media.filter((m) => m.category === "selfie");
    const profilesImages = media.filter((m) => m.category === "profiles");
    const fullLengthImages = media.filter((m) => m.category === "full_length");
    const sideProfileImages = media.filter((m) => m.category === "side_profile");
    const ethnicImages = media.filter((m) => m.category === "ethnic");
    const additionalPortfolioImages = media.filter((m) => m.category === "additional_portfolio");
    const allImages = [...images, ...indianImages, ...westernImages];

    const intro = media.find((m) => m.category === "intro_video");
    const takes = media
        .filter(
            (m) =>
                m.category === "take" ||
                m.category === "take_1" ||
                m.category === "take_2" ||
                m.category === "take_3",
        )
        .map((m) => {
            if (m.category === "take") return m;
            const n = m.category.replace("take_", "");
            return { ...m, _legacy: true, label: m.label || `Take ${n}` };
        });

    const showImagesSection = (requirements.portfolio_image_visibility !== REQUIREMENT_TIERS.HIDDEN) || (requirements.portfolio_indian_visibility !== REQUIREMENT_TIERS.HIDDEN) || (requirements.portfolio_western_visibility !== REQUIREMENT_TIERS.HIDDEN);

    // Talent Profile Migration, Phase 3 — "My Saved Media" picker
    // derivations. `media` already contains every item actually in this
    // submission (uploaded OR chosen from the Library — they render
    // identically below, by design); these just work out selection state
    // for the picker itself.
    const selectedLibrarySourceIds = new Set(
        media.filter((m) => m.source_talent_media_id).map((m) => m.source_talent_media_id),
    );
    // Items that WERE chosen from the Library but whose source has since
    // been deleted from the Talent Profile (reconciled server-side on every
    // resume/GET — see build_talent_submission_view). They no longer appear
    // in `libraryMedia` (their source is gone), so they're tracked
    // separately here and rendered as their own warning card.
    const removedFromProfileItems = media.filter(
        (m) => m.source_talent_media_id && m.removed_from_profile,
    );
    // Phase 2 — every REUSABLE_MEDIA_CATEGORIES category the backend
    // recognizes gets a picker section, not just the original 4. Audition
    // takes are never here (never reusable — project-only by design).
    // Data-driven by category name, not a hardcoded branch per category, so
    // a future custom project media category needs no code change here.
    const LIBRARY_CATEGORIES = [
        { key: "intro_video", label: "Intro" },
        { key: "image", label: "Portfolio" },
        { key: "indian", label: "Indian" },
        { key: "western", label: "Western" },
        { key: "selfie", label: "Selfie" },
        { key: "profiles", label: "Profiles" },
        { key: "full_length", label: "Full Length" },
        { key: "side_profile", label: "Side Profile" },
        { key: "ethnic", label: "Ethnic" },
        { key: "additional_portfolio", label: "Additional Portfolio" },
    ];
    const libraryByCategory = Object.fromEntries(
        LIBRARY_CATEGORIES.map(({ key }) => [key, libraryMedia.filter((m) => m.category === key)]),
    );
    const removedByCategory = Object.fromEntries(
        LIBRARY_CATEGORIES.map(({ key }) => [key, removedFromProfileItems.filter((m) => m.category === key)]),
    );

    // Talent Profile Migration, Phase 4 — the aggregated consent summary
    // ("1 Intro Video, 3 Portfolio Images"). `pendingMediaConsent` already
    // comes from the server as the exact current pending set (see
    // build_talent_submission_view's pending_media_consent) — this is pure
    // display grouping, no client-side "is this reusable" logic duplicated.
    const PENDING_CONSENT_LABELS = {
        intro_video: ["Intro Video", "Intro Videos"],
        image: ["Portfolio Image", "Portfolio Images"],
        indian: ["Indian Look Image", "Indian Look Images"],
        western: ["Western Look Image", "Western Look Images"],
        selfie: ["Selfie", "Selfies"],
        profiles: ["Profile Image", "Profile Images"],
        full_length: ["Full Length Image", "Full Length Images"],
        side_profile: ["Side Profile Image", "Side Profile Images"],
        ethnic: ["Ethnic Look Image", "Ethnic Look Images"],
        additional_portfolio: ["Additional Portfolio Image", "Additional Portfolio Images"],
    };
    // UX-polish fix — the legacy consent dialog below must only ever speak
    // for items whose destination genuinely isn't known yet (e.g. a draft
    // resumed from before this feature shipped, or an admin-uploaded item
    // handed back to the talent) — never for a normal new upload, since
    // those already have a pre-chosen destination the effect above
    // resolves silently. This is the single source of truth both the
    // summary text and the dialog's own render condition read from, so
    // they can never disagree about what's actually still awaiting a
    // choice.
    const { awaitingChoice: pendingConsentAwaitingChoice } = splitPendingConsentByKnownDestination(
        pendingMediaConsent, mediaDestination,
    );
    const pendingConsentSummary = Object.entries(
        pendingConsentAwaitingChoice.reduce((acc, m) => {
            acc[m.category] = (acc[m.category] || 0) + 1;
            return acc;
        }, {}),
    ).map(([category, count]) => {
        const [singular, plural] = PENDING_CONSENT_LABELS[category] || [category, category];
        return `${count} ${count === 1 ? singular : plural}`;
    });

    const activeConditionalVideoRules = useMemo(() => {
        if (!project || !Array.isArray(project.conditional_video_rules)) return [];
        return project.conditional_video_rules.filter((rule) => {
            const ans = (form.custom_answers || {})[rule.question_id];
            return (
                ans &&
                String(ans).trim().toLowerCase() ===
                    String(rule.trigger_value).trim().toLowerCase()
            );
        });
    }, [project, form.custom_answers]);

    const regularTakes = takes.filter(
        (t) => !activeConditionalVideoRules.some((r) => r.video_label === t.label),
    );

    const uploadImages = async (files, imageCategory = "image") => {
        // Phase 3 — per-category cap (10 each), not combined. Look up the
        // current count of THIS category and refuse uploads that would
        // overflow it.
        const currentForCategory =
            imageCategory === "indian"
                ? indianImages.length
                : imageCategory === "western"
                  ? westernImages.length
                  : images.length;
        const room = MAX_IMAGES_PER_CATEGORY - currentForCategory;
        const accepted = Array.from(files).slice(0, Math.max(0, room));
        if (room <= 0) {
            const label = imageCategory === "indian" ? "Indian look" : imageCategory === "western" ? "Western look" : "Portfolio";
            toast.error(`${label} image limit reached (${MAX_IMAGES_PER_CATEGORY})`);
            return;
        }
        if (files.length > room) {
            toast.info(`Only ${room} more ${imageCategory} images allowed (max ${MAX_IMAGES_PER_CATEGORY})`);
        }
        // Client-side per-image cap (20 MB) (P5)
        const over = accepted.find((f) => f.size > 20 * 1024 * 1024);
        if (over) {
            toast.error(`"${over.name}" is too large (max 20 MB per image).`);
            return;
        }
        // HEIC is a genuinely supported format (see UploadManagerContext.jsx
        // and the identical checks in ApplicationPage.jsx / TalentEdit.jsx,
        // and it's converted to a displayable image by Cloudinary same as
        // the other flows) — this check used to also reject it, meaning the
        // single most common iPhone camera-roll format was blocked here
        // while working on the other two upload flows. Only BMP/TIFF
        // (genuinely unsupported) are rejected now.
        const badFormat = accepted.find((f) => {
            const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
            return ['.bmp', '.tiff'].includes(ext) || ['image/bmp', 'image/tiff'].includes(f.type);
        });
        if (badFormat) {
            toast.error(`BMP and TIFF formats are not supported. Please upload JPEG, PNG, WEBP, or HEIC.`);
            return;
        }
        const unsupportedImage = accepted.find((f) => {
            const ext = f.name.substring(f.name.lastIndexOf('.')).toLowerCase();
            return !f.type.startsWith('image/') && !['.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'].includes(ext);
        });
        if (unsupportedImage) {
            toast.error(`"${unsupportedImage.name}" is not a supported image format. Please upload JPG, PNG, WEBP, or HEIC.`);
            return;
        }

        // These run concurrently (Promise.all) and the label is only used
        // as UploadManagerContext's in-flight slotKey
        // (`${category}:${label}`) — never sent to or stored by the backend
        // for image categories. Two files sharing a name (camera photos,
        // screenshots) selected in the same multi-select would collide on
        // that key: the sync in-flight guard silently drops the second with
        // only a console.warn, no toast, no failed-state entry. A per-call
        // unique suffix fixes it without touching that shared guard or
        // anything persisted server-side.
        await Promise.all(
            accepted.map((f) => triggerUpload(f, imageCategory, `${f.name}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`))
        );
    };

    // Automatic Media Categorization (item 3) — runs the heuristic over a
    // dropped/selected batch and opens the review modal. Nothing uploads
    // here; upload only happens from handleCategorizationConfirm, via the
    // exact same uploadImages() every other zone already uses.
    const runBulkCategorization = async (files) => {
        if (!files?.length) return;
        setCategorizingBatch(true);
        try {
            const result = await suggestCategoriesForBatch(files);
            setCategorizationBatch(result);
        } finally {
            setCategorizingBatch(false);
        }
    };

    const handleBulkCategorizeDrop = async (e) => {
        e.preventDefault();
        setIsBulkDragOver(false);
        const files = await collectDroppedFiles(e.dataTransfer);
        await runBulkCategorization(files);
    };

    const handleCategorizationConfirm = (finalGroups) => {
        setCategorizationBatch(null);
        Object.entries(finalGroups).forEach(([category, files]) => {
            uploadImages(files, category);
        });
    };

    const removeMedia = async (mid) => {
        try {
            await axios.delete(
                `/public/submissions/${saved.id}/media/${mid}`,
                authCfg,
            );
            const { data } = await axios.get(
                `/public/submissions/${saved.id}`,
                authCfg,
            );
            applySubmissionResponse(data);
        } catch {
            toast.error("Could not remove file");
        }
    };

    // Talent Profile Migration, Phase 3 — "My Saved Media" picker actions.
    // Selecting/deselecting a Library item is a reference operation: POST
    // .../media/from-library copies by value server-side (same public_id/
    // url, no upload), and deselecting reuses the existing DELETE endpoint
    // exactly like removing any other media item.
    const toggleLibraryMedia = async (item, isSelected) => {
        let currentSaved = saved;
        if (!currentSaved) {
            if (adminMode) {
                toast.error(adminBootstrapping ? "Still starting this submission — please wait a moment and try again." : "Could not start this submission. Please reload and try again.");
                return;
            }
            const err = canCreateDraft();
            if (err) {
                toast.error(err);
                return;
            }
            currentSaved = await startSubmissionDirect();
            if (!currentSaved) return;
        }
        const cfg = { headers: { Authorization: `Bearer ${currentSaved.token}` } };
        setLibraryBusyId(item.id);
        try {
            if (isSelected) {
                const existing = media.find((m) => m.source_talent_media_id === item.id);
                if (existing) {
                    await axios.delete(
                        `/public/submissions/${currentSaved.id}/media/${existing.id}`,
                        cfg,
                    );
                    const { data } = await axios.get(
                        `/public/submissions/${currentSaved.id}`,
                        cfg,
                    );
                    applySubmissionResponse(data);
                }
            } else {
                const { data } = await axios.post(
                    `/public/submissions/${currentSaved.id}/media/from-library`,
                    { talent_media_id: item.id },
                    cfg,
                );
                applySubmissionResponse(data);
            }
        } catch (e) {
            toast.error(formatErrorDetail(e, "Could not update selection"));
        } finally {
            setLibraryBusyId(null);
        }
    };

    const selectAllLibraryMedia = async () => {
        const toAdd = libraryMedia.filter((item) => !selectedLibrarySourceIds.has(item.id));
        for (const item of toAdd) {
            // Sequential, not parallel — each call reads-then-writes the
            // submission's own media array (cap checks, single-slot
            // replace), so firing them in parallel would race against the
            // same document.
            // eslint-disable-next-line no-await-in-loop
            await toggleLibraryMedia(item, false);
        }
    };

    // Manual-testing fix — existing Library media (photos/intro) must NOT be
    // preselected: the talent has to explicitly tap each item they want to
    // include (see LibraryMediaPicker below, which renders every item
    // unselected until toggled). This used to auto-attach every Library item
    // to the submission the first time the Uploads step was reached; that
    // auto-attach call has been removed. The one thing still worth doing
    // automatically is collapsing to the compact "Using your saved photos"
    // summary when a RESUMED draft already has real selections attached
    // (`selectedLibrarySourceIds` is derived straight from the submission's
    // own media, not from this effect) — that's reflecting a prior explicit
    // choice, not preselecting anything new.
    useEffect(() => {
        if (adminMode) return;
        if (sectionForStep(currentStep) !== "uploads") return;
        if (autoReuseAttemptedRef.current) return;
        if (libraryMedia.length === 0) return;
        autoReuseAttemptedRef.current = true;
        if (selectedLibrarySourceIds.size > 0) {
            setShowLibraryPicker(false);
        }
    }, [adminMode, currentStep, libraryMedia.length, selectedLibrarySourceIds.size]);

    const dismissRemovedWarning = (mid) => {
        // "Keep for this submission" — the item is already in
        // submission.media; keeping it needs no backend call, just hiding
        // the warning locally.
        setDismissedRemovedWarnings((prev) => new Set(prev).add(mid));
    };

    // Talent Profile Migration, Phase 4 — one decision resolves EVERY
    // currently-pending item in a single call (see
    // apply_media_consent_decision() server-side), whether the talent
    // uploaded 1 intro video, 3 images, or a mix.
    // `mediaIds` (Phase 2, item 5) — Admin Mode's per-item "Save to Master
    // Profile" checkbox scopes resolution to ONE media item instead of every
    // currently-pending item; omitted (undefined), this is byte-for-byte the
    // original talent-flow batch behavior. The generic toast/dialog-reset
    // side effects only make sense for that batch dialog, so they're skipped
    // for a scoped per-item call — the checkbox itself is the feedback.
    const submitMediaConsent = async (decision, mediaIds) => {
        if (!saved?.id) return;
        setMediaConsentSubmitting(true);
        try {
            const { data } = await axios.post(
                `/public/submissions/${saved.id}/media-consent`,
                mediaIds ? { decision, media_ids: mediaIds } : { decision },
                authCfg,
            );
            applySubmissionResponse(data);
            if (!mediaIds) {
                setMediaConsentChoice("only_this_project");
                toast.success(
                    decision === "update_profile"
                        ? "Your Talent Profile has been updated."
                        : "Saved for this project only.",
                );
            }
        } catch (e) {
            toast.error(formatErrorDetail(e, "Could not save your choice"));
        } finally {
            setMediaConsentSubmitting(false);
        }
    };

    // Admin Mode per-item "Save to Master Profile" checkbox (item 5). Only
    // reusable categories (REUSABLE_MEDIA_CATEGORIES server-side) ever carry
    // a pending consent item — checking/unchecking a non-reusable category's
    // media (e.g. an audition take) is simply never offered this control.
    const setMediaConsentForItem = (mediaId, saveToMasterProfile) => {
        submitMediaConsent(saveToMasterProfile ? "update_profile" : "only_this_project", [mediaId]);
    };

    // UX-polish fix — talent-facing uploads now answer their own pending-
    // consent question the instant it appears server-side, using whatever
    // destination the talent already picked BEFORE selecting the file (see
    // `mediaDestination`'s declaration above). Root-cause fix (not a visual
    // hide): the OLD "how would you like to use this?" dialog below is now
    // ALSO gated on the same `splitPendingConsentByKnownDestination` split
    // (lib/mediaDestination.js) — it never renders at all for a pending
    // item whose category already has a known destination, so there's no
    // longer a race between this effect firing and that dialog's own
    // render condition on the same tick (previously both were gated on the
    // raw, undifferentiated `pendingMediaConsent`, so the dialog could
    // flash into view for the round-trip duration of this effect's own
    // resolve call — near-instant locally, but very real over a mobile
    // network, which is exactly what manual testing caught). Admin Mode is
    // untouched (it already resolves consent through its own per-item
    // checkbox, never this dialog).
    useEffect(() => {
        if (adminMode) return;
        if (pendingMediaConsent.length === 0) return;
        const { known: toResolve } = splitPendingConsentByKnownDestination(pendingMediaConsent, mediaDestination);
        if (toResolve.length === 0) return;
        const byDecision = groupByDestinationDecision(toResolve, mediaDestination);
        Object.entries(byDecision).forEach(([decision, ids]) => submitMediaConsent(decision, ids));
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [adminMode, pendingMediaConsent, mediaDestination]);

    // Phase 2, item 5 — Admin Mode no longer auto-resolves every pending
    // item to "update_profile" in one blanket batch call (Phase 1's
    // behavior). It still skips the TALENT-facing modal dialog (the admin's
    // own session is the identity/consent proof, so no "how would you like
    // to use this?" prompt is shown) — but each admin-uploaded item now gets
    // its own "Save to Master Profile" checkbox (rendered per-thumbnail,
    // see setMediaConsentForItem above) instead of one decision for the
    // whole batch. Nothing auto-resolves; an unchecked item just stays
    // "only_this_project" until the admin explicitly checks it.

    const replaceMediaFile = async (oldMedia, file) => {
        const isVideoSlot = ["intro_video", "take", "take_1", "take_2", "take_3"].includes(oldMedia.category);
        // Same unique-slotKey fix as uploadImages() above — only "take"
        // needs its real label preserved (persisted server-side); images
        // just need a collision-proof in-flight key.
        const label = oldMedia.category === "take"
            ? oldMedia.label
            : (!isVideoSlot ? `${file.name}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` : null);
        await triggerUpload(file, oldMedia.category, label);
        await removeMedia(oldMedia.id);
    };

    const finalize = async () => {
        // ── Guided validation: run inline before network call ──────────────
        // `experience` (useSubmissionExperienceModel) was computed during the
        // render that's already happened by the time this click handler runs
        // — no need to recompute anything here, just read the model.
        const missing = experience.missingRequirements;
        if (missing.length > 0) {
            // Build error map
            const errors = {};
            missing.forEach((req) => {
                errors[req.id] = req.label + " is required";
            });
            setValidationErrors(errors);

            // A failed upload isn't "missing" — it's a distinct situation the
            // talent needs to act on. Prioritize telling them that instead of
            // the generic "please fill in" message (same readinessSummary the
            // footer reads, so this can't drift from what's shown there).
            const firstFailed = experience.readinessSummary.failed[0];
            const jumpTarget = firstFailed || missing[0];

            // Make the item visible (however that's achieved — no
            // per-section hardcoding here) and scroll/focus it, retrying as
            // more of the page reveals itself. Shared with the readiness
            // panel's click-to-jump — see lib/scrollHighlight.js.
            revealAndJumpToRequirementItem(jumpTarget, fieldRefs, () => ensureRequirementVisible(jumpTarget));

            if (experience.readinessSummary.failed.length === 1) {
                toast.error(`Your ${firstFailed.label} failed to upload. Please retry before submitting.`);
            } else if (experience.readinessSummary.failed.length > 1) {
                toast.error(`${experience.readinessSummary.failed.length} uploads need attention.`);
            } else {
                toast.error(
                    `Please fill in: ${missing[0].label}${
                        missing.length > 1 ? ` (+${missing.length - 1} more)` : ""
                    }`
                );
            }
            return;
        }

        const isResubmission = submission && submission.status && submission.status !== "draft";
        
        // All good — clear any stale errors and proceed
        setValidationErrors({});

        let currentSaved = saved;
        if (!currentSaved) {
            if (adminMode) {
                toast.error(adminBootstrapping ? "Still starting this submission — please wait a moment and try again." : "Could not start this submission. Please reload and try again.");
                return;
            }
            const next = await startSubmissionDirect();
            if (!next) return;
            currentSaved = next;
        } else {
            await saveForm();
        }
        // Phase 2, item 5 safety net — the per-item "Save to Master Profile"
        // checkbox means an item can be left genuinely pending if the admin
        // never touched its checkbox. The backend blocks finalize while
        // anything is still pending ("Please choose how to use your new
        // photo/video uploads"); rather than surface that as a confusing
        // error, default anything still-pending to the conservative choice
        // (project-only, no master-profile promotion) right before
        // finalizing — the admin can still promote it manually beforehand
        // via the checkbox for anything they DO want promoted.
        if (adminMode && pendingMediaConsent.length > 0) {
            await submitMediaConsent("only_this_project");
        }
        setFinalizing(true);
        try {
            await axios.post(
                `/public/submissions/${currentSaved.id}/finalize`,
                {},
                {
                    headers: {
                        Authorization: `Bearer ${currentSaved.token}`,
                    }
                },
            );
            const { data } = await axios.get(
                `/public/submissions/${currentSaved.id}`,
                {
                    headers: {
                        Authorization: `Bearer ${currentSaved.token}`,
                    }
                },
            );
            applySubmissionResponse(data);
            setEditMode(false);
            // Once the user finalises, clear the local draft — the
            // canonical state lives on the backend now.
            try { localStorage.removeItem(LS_DRAFT_KEY(slug)); } catch (e) { console.error(e); }
            clearStep(slug);
            clearFinalStepReached(slug);
            
            if (isResubmission) {
                toast.success("Your audition has been updated successfully.");
                setTimeout(() => {
                    toast.success("The Talentgram team will review the latest version.");
                }, 500);
            } else {
                toast.success("Your audition has been received.");
            }
        } catch (err) {
            toast.error(
                formatErrorDetail(err, "Please complete all required fields"),
            );
        } finally {
            setFinalizing(false);
        }
    };

    // The Submission Experience Model — single presentation model for this
    // page (hooks/useSubmissionExperienceModel.js). It aggregates the
    // Requirement Engine (lib/requirementEngine.js), the Operational Engine,
    // and the Readiness Engine (both lib/readinessStatus.js) into one object:
    // checklist, readinessSummary, uploadSummary, blockingReason, submitCta,
    // sectionStatus, overallProgress. Every derived value the page or its
    // children need — the readiness panel, the Submit button, the footer
    // messaging, and (later) the Upload Manager and section headers — comes
    // from THIS, not from a locally re-derived variable.
    const experience = useSubmissionExperienceModel({
        project,
        form,
        submission,
        activeUploads,
        finalizing,
        saveStatus,
        readyLabel: "Submit Audition",
        isReturningTalent,
    });

    // Submission Wizard — {stepId: SECTION_STATUS.*} for WizardProgressBar,
    // derived straight from the Readiness Engine's own per-section rollup
    // (experience.sectionStatus). No re-derivation of what's required or
    // satisfied happens here.
    const wizardStepStatusById = useMemo(() => {
        const map = {};
        WIZARD_STEPS.forEach((step) => {
            const entry = experience.sectionStatus.find((s) => s.section === step.section);
            if (entry) map[step.id] = entry.status;
        });
        return map;
    }, [experience.sectionStatus]);

    // Phase 3 (progress-indicator fix) — WHICH step chips WizardProgressBar
    // shows; see wizardStepsForDisplay's own doc comment. WIZARD_STEPS'
    // fixed 4-entry technical numbering is unchanged — currentStep/
    // navigation/validation all still use it exactly as before.
    // WizardProgressBar itself needs no changes — it already renders
    // `steps.length` dynamically, not a hardcoded 4.
    const wizardDisplaySteps = useMemo(
        () => wizardStepsForDisplay({ isReturningTalent, currentStep }),
        [isReturningTalent, currentStep],
    );

    // The READY-TO-SUBMIT FOOTER below (Almost Done / Identity Confirmation /
    // finalize button) must only ever appear on the actual last step of
    // THIS talent's flow — otherwise `experience.readinessSummary.ready`
    // alone (which the footer's outer gate also checks) can go true the
    // moment every requirement happens to already be satisfied, even while
    // the talent is still sitting on an earlier step (e.g. Media, with
    // Basic Profile/Skills still ahead, or a project where most fields are
    // optional) — stacking a second "Next"/Submit control underneath that
    // step's own WizardStepNav. "Last step of this flow" isn't always the
    // fixed id 4: wizardDisplaySteps already encodes the same new-talent-
    // vs-returning-talent variation (Skills vs. Uploads) this footer's own
    // comment describes, so reusing it here keeps both in agreement.
    const isOnFinalDisplayedStep =
        currentStep === Math.max(...wizardDisplaySteps.map((s) => s.id));

    // "Update my Profile" disclosure — one small task per screen (final
    // spec pass, 2026-08): collapsed by default for EVERY talent, new or
    // returning, so Basic Profile's default view is just Name/Phone/DOB/
    // Height and Skills' default view is just the skill chips — not both
    // of those plus Gender/Ethnicity/Instagram/Bio/Work Links all visible
    // at once. Still always forced open if any field it hides is actually
    // required-and-unsatisfied for THIS project (never silently hide
    // something blocking Next) — that safety net is untouched, only the
    // old "expanded for a brand-new talent, nothing to hide yet" default
    // is gone, since "nothing to hide" was true of the DATA but not of the
    // screen's visual density.
    const isFieldRequiredAndMissing = useCallback((fieldId) => {
        const item = experience.readinessModel.find((i) => i.id === fieldId);
        return !!item && item.requirement === REQUIREMENT_TIERS.REQUIRED && !item.satisfied;
    }, [experience.readinessModel]);
    // Drives the "*" label suffix on fields whose requirement varies by
    // project config (Age/Height/Location today) — never hardcode a "*"
    // on a field the Requirement Engine says is optional for this project.
    const isFieldRequired = useCallback((fieldId) => {
        const item = experience.readinessModel.find((i) => i.id === fieldId);
        return !!item && item.requirement === REQUIREMENT_TIERS.REQUIRED;
    }, [experience.readinessModel]);
    // P1 fix (2026-09): the admin's "Hidden" choice under Profile Fields
    // Configuration previously had no effect on rendering at all (only
    // "required" vs "everything else" was ever checked) — this is the one
    // new check that lets a HIDDEN field actually be omitted below, using
    // the Requirement Engine's own tier exactly like isFieldRequired does.
    const isFieldHidden = useCallback((fieldId) => {
        const item = experience.readinessModel.find((i) => i.id === fieldId);
        return !!item && item.requirement === REQUIREMENT_TIERS.HIDDEN;
    }, [experience.readinessModel]);
    // For a returning talent, Instagram lives on Project Questions now (see
    // recurringProfileFields below), not inside this disclosure — so it
    // shouldn't be a reason to auto-open a disclosure that no longer
    // contains it.
    const identityDisclosureOpen = (
        isReturningTalent ? ["gender", "ethnicity"] : ["gender", "ethnicity", "instagram_handle", "instagram_followers"]
    ).some(isFieldRequiredAndMissing);
    const skillsDisclosureOpen =
        ["bio", "work_links"].some(isFieldRequiredAndMissing);

    // The page's `ensureVisible` step for every navigation helper below:
    // makes one more attempt at revealing whatever's hidden and reports
    // whether it changed anything. Callers (and `revealAndJumpToRequirementItem`
    // in lib/scrollHighlight.js, which invokes this) only care about that
    // contract — "make more of the page visible, if you can" — never about
    // HOW visibility is achieved. Today that means opening every currently
    // collapsed key in `collapsedSections`, generically, by iterating the
    // map rather than naming sections; a future page could satisfy the same
    // contract by switching a tab, opening a drawer, or advancing a wizard
    // step instead, with zero change to any navigation call site
    // (SectionStatusBadge, the readiness panel, the Submit CTA, or
    // finalize-time validation).
    const ensureRequirementVisible = useCallback((item) => {
        let changed = false;
        // Wizard step switch — the target's `section` may live on a step
        // other than `currentStep`; CSS-hidden steps stay mounted (see the
        // `data-wizard-step` gates below), so switching just flips a class,
        // no remount, and revealAndJumpToRequirementItem's own retry loop
        // (lib/scrollHighlight.js) naturally re-resolves the selector once
        // it's visible — this function doesn't need to wait for it itself.
        const targetStep = item?.section ? stepForSection(item.section) : null;
        if (targetStep && targetStep !== currentStep) {
            setCurrentStep(targetStep);
            changed = true;
        }
        const collapsedKeys = Object.keys(collapsedSections).filter((key) => collapsedSections[key]);
        if (collapsedKeys.length > 0) {
            setCollapsedSections((prev) => {
                const next = { ...prev };
                collapsedKeys.forEach((key) => { next[key] = false; });
                return next;
            });
            changed = true;
        }
        return changed;
    }, [collapsedSections, currentStep, setCurrentStep]);

    const focusRequirementItem = useCallback((item) => {
        revealAndJumpToRequirementItem(item, fieldRefs, () => ensureRequirementVisible(item));
    }, [ensureRequirementVisible]);

    // SectionStatusBadge click handler — a UI shortcut, not another source of
    // truth. It picks a target from `experience.checklist` (already-computed
    // Readiness Engine output, same list the panel renders) and hands off to
    // the SAME navigation primitive (`revealAndJumpToRequirementItem`) the
    // readiness panel, the Submit CTA, and finalize-time validation all use,
    // so there is exactly one way a requirement item or section ever gets
    // focused in this page:
    //   - unresolved required item found  → `focusRequirementItem` (identical
    //     to a readiness-panel row click)
    //   - nothing unresolved (section already COMPLETE, or has no required
    //     items at all) → same reveal mechanism, scrolled to the section
    //     wrapper instead of a field, with `highlight: false` — no flash, no
    //     stolen focus, since there's nothing to draw attention to.
    const focusSection = useCallback((sectionEntry) => {
        if (!sectionEntry) return;
        const sectionKey = sectionEntry.section;
        const sectionItems = experience.checklist.filter((item) => item.section === sectionKey);
        const unresolved =
            sectionItems.find((item) => item.operational === OPERATIONAL_STATES.FAILED) ||
            sectionItems.find((item) => item.operational === OPERATIONAL_STATES.MISSING) ||
            sectionItems.find((item) => item.operational !== OPERATIONAL_STATES.COMPLETED);

        if (unresolved) {
            focusRequirementItem(unresolved);
            return;
        }

        const sectionTarget = { id: sectionKey, section: sectionKey, selector: SECTION_WRAPPER_SELECTOR[sectionKey] };
        revealAndJumpToRequirementItem(
            sectionTarget,
            fieldRefs,
            () => ensureRequirementVisible(sectionTarget),
            { block: "start", highlight: false },
        );
    }, [experience.checklist, focusRequirementItem, ensureRequirementVisible]);

    // UX enhancement — after a wizard step actually changes (Next, Back, or
    // jumping to a completed step via the progress bar), scroll back to the
    // top so the new step is always seen starting from its own header
    // (title, instructions, first field) rather than wherever the talent
    // happened to be scrolled on the step they just left — the exact
    // "user remains halfway down the page" complaint from mobile testing.
    // Deliberately NOT used by focusRequirementItem/ensureRequirementVisible
    // (the readiness panel's click-to-jump, and finalize-time validation) —
    // that path already scrolls to and highlights the specific missing
    // field via revealAndJumpToRequirementItem (lib/scrollHighlight.js), and
    // a blanket top-of-page scroll here would immediately fight it. A plain
    // window.scrollTo (not a ref+useEffect) is enough: this page has no
    // dedicated inner scroll container — the same document-level scroll
    // already used everywhere else in this component — and "top" (y=0)
    // doesn't depend on the new step's height, so there's nothing to wait
    // on React to render first.
    const scrollWizardStepToTop = useCallback(() => {
        if (typeof window === "undefined") return;
        const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
        window.scrollTo({ top: 0, behavior: prefersReducedMotion ? "auto" : "smooth" });
    }, []);

    // Submission Wizard — Next/Back for Steps 1-3 (Step 4's own Submit button
    // is unchanged, see handleSubmitCtaClick below). Validates ONLY the
    // current step's required items (experience.missingRequirements is
    // already the full Requirement Engine output — this just filters it to
    // one step's `section`), never anything ahead, per the wizard's own
    // validation rule. Advancing out of Project Questions (whichever numeric
    // step that is post-reorder — see wizardSteps.js) additionally has to
    // create the backend submission record the first time (item 1 of the
    // Phase 1 plan) — reuses startSubmissionDirect() verbatim, since Media
    // (the very next step) needs `saved.id`/`saved.token` to attach uploads
    // to.
    // WhatsApp/Drive-style upload architecture (2026-08) — "Next" on the
    // Uploads step must NOT wait for a required file to finish reaching
    // Cloudinary, only for the talent to have actually selected/started
    // sending it. `item.satisfied` (Requirement Engine) means "confirmed in
    // submission.media", which only becomes true after the network transfer
    // completes — using that alone here would recreate exactly the "select →
    // wait → watch upload → wait" flow this architecture removes. An item
    // with a live (non-failed) entry in activeUploads counts as handled for
    // navigation purposes; Submit still requires true completion (see
    // SUBMIT_BLOCKING_REASONS.WAITING in the Submission Experience Model,
    // unchanged) — a failed upload still blocks Next, same as before.
    const isMediaItemInFlight = useCallback((item) => {
        const prefix = item.media?.prefix?.replace(/:$/, "");
        if (!prefix) return false;
        return Object.values(activeUploads).some((u) => {
            if (!["queued", "compressing", "uploading", "processing", "retrying"].includes(u.status)) return false;
            return u.category === prefix || (u.category || "").startsWith(prefix);
        });
    }, [activeUploads]);

    const handleWizardNext = useCallback(async () => {
        const stepSection = sectionForStep(currentStep);
        const stepMissing = experience.missingRequirements.filter(
            (item) => item.section === stepSection && !isMediaItemInFlight(item),
        );
        if (stepMissing.length > 0) {
            const stepFailed = experience.readinessSummary.failed.find((item) => item.section === stepSection);
            focusRequirementItem(stepFailed || stepMissing[0]);
            toast.error(
                stepFailed
                    ? `Your ${stepFailed.label} failed to upload. Please retry before continuing.`
                    : `Please fill in: ${stepMissing[0].label}${stepMissing.length > 1 ? ` (+${stepMissing.length - 1} more)` : ""}`,
            );
            return;
        }

        if (stepSection === "projectQuestions") {
            if (!saved) {
                if (adminMode) {
                    toast.error(adminBootstrapping ? "Still starting this submission — please wait a moment and try again." : "Could not start this submission. Please reload and try again.");
                    return;
                }
                const next = await startSubmissionDirect();
                if (!next) return;
            } else {
                await saveForm();
            }
        } else {
            await saveForm();
        }
        setCurrentStep((s) => Math.min(TOTAL_STEPS, s + 1));
        scrollWizardStepToTop();
    }, [currentStep, experience.missingRequirements, experience.readinessSummary, isMediaItemInFlight, focusRequirementItem, saved, adminMode, adminBootstrapping, setCurrentStep, scrollWizardStepToTop]);

    const handleWizardBack = useCallback(() => {
        setCurrentStep((s) => Math.max(1, s - 1));
        scrollWizardStepToTop();
    }, [setCurrentStep, scrollWizardStepToTop]);

    // Progress-bar step-chip jump (Next/Back covered above) — only ever
    // called for an already-completed or optional step (see
    // WizardProgressBar's own canJumpTo), so no validation gate needed here,
    // same as clicking Back.
    const handleWizardStepJump = useCallback((step) => {
        setCurrentStep(step);
        scrollWizardStepToTop();
    }, [setCurrentStep, scrollWizardStepToTop]);

    // Mobile swipe navigation — swipe-back always allowed (identical to
    // tapping Back, never needs validation); swipe-forward calls the exact
    // same handleWizardNext() the Next button uses, so it's gated
    // identically either way. Disabled on Step 4 forward (no "Next" there,
    // Submit is a deliberate tap) and before the email gate unlocks.
    const wizardSwipeHandlers = useSwipeStep({
        onSwipeBack: () => currentStep > 1 && handleWizardBack(),
        onSwipeForward: () => currentStep < TOTAL_STEPS && handleWizardNext(),
        disabled: !emailGateUnlocked,
    });

    // Pure renderer trigger for the Submit button: reads `experience.submitCta`
    // (already-resolved label/disabled/action) and dispatches — it contains
    // no readiness logic of its own. Plain function, not useCallback:
    // `experience` already changes reference whenever anything relevant does
    // (useSubmissionExperienceModel's own useMemo), so wrapping this would buy
    // no extra referential stability.
    const handleSubmitCtaClick = () => {
        const { submitCta } = experience;
        if (submitCta.buttonAction === CTA_ACTIONS.SCROLL_TO_MISSING) {
            if (submitCta.scrollTarget) focusRequirementItem(submitCta.scrollTarget);
            return;
        }
        finalize();
    };

    // ALMOST DONE (continued from handleAlmostDoneSendCode above) —
    // verifying here means "confirm identity AND submit" in one motion, per
    // the spec's "Almost Done" copy. handleVerifyOtp's onSuccess hook fires
    // handleSubmitCtaClick() once emailVerified is committed; safe to call
    // unconditionally regardless of readiness — handleSubmitCtaClick itself
    // only finalizes when submitCta.buttonAction is SUBMIT (truly ready),
    // otherwise it just scrolls to the blocking item.
    const handleAlmostDoneVerify = (e) => {
        handleVerifyOtp(e, { onSuccess: () => handleSubmitCtaClick() });
    };

    const MAX_TAKES = 5;
    const canAddTake = takes.length < MAX_TAKES;
    const isSubmitted =
        submission && submission.status && submission.status !== "draft";

    // Smart Checklist deep link — the one new entry point Project Detail
    // (`/portal/projects/{slug}`) uses to jump into a specific requirement
    // here. `?focus=<requirementId>` is the only new signal; resolving it
    // reuses the exact same readinessModel lookup + focusRequirementItem
    // path a readiness-panel click already uses inside this page — no new
    // id/selector map. Split into two effects because a submitted project
    // renders the read-only Submission Hub (no fields in the DOM) until
    // `editMode` flips true; the jump effect waits for that flip before
    // querying the DOM, using the existing "Update Submission" transition
    // rather than a new one.
    const focusParam = useMemo(
        () => (typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("focus") : null),
        [],
    );
    // P1 — audition-material CTA moved up next to the title (was buried
    // below Additional Details); this derives the "Script / Reference
    // Video / Audio Brief" helper text from whichever categories the
    // backend actually returned, instead of hardcoding a label set.
    const hasAuditionMaterial = !!project && (
        (Array.isArray(project.materials) && project.materials.length > 0) ||
        (Array.isArray(project.video_links) && project.video_links.length > 0)
    );
    const auditionMaterialSummary = useMemo(() => {
        if (!project) return "";
        const materials = Array.isArray(project.materials) ? project.materials : [];
        const hasVideoLinks = Array.isArray(project.video_links) && project.video_links.length > 0;
        const labels = [];
        if (materials.some((m) => m.category === "script")) labels.push("Script");
        if (materials.some((m) => m.category === "video_file") || hasVideoLinks) labels.push("Reference Video");
        if (materials.some((m) => m.category === "audio")) labels.push("Audio Brief");
        return labels.join(" / ");
    }, [project]);
    // `loading` only reflects the project fetch — the JWT/ATK resume effects
    // above set `submission` on their own, unrelated timeline, so there's a
    // real window where `loading` is already false but `submission` hasn't
    // arrived yet. Reusing their own existing guards (`saved`, `LS_ATK_KEY`)
    // to recognize that in-flight window, so the jump effect doesn't act on
    // a false "not submitted" reading and fire before the Submission Hub
    // takes over the page.
    const resumePending = !submission && (!!saved?.token || !!(typeof window !== "undefined" && localStorage.getItem(LS_ATK_KEY(slug))));
    const focusEditModeAppliedRef = useRef(false);
    useEffect(() => {
        if (!focusParam || focusEditModeAppliedRef.current || resumePending) return;
        if (isSubmitted && !editMode) {
            focusEditModeAppliedRef.current = true;
            setEditMode(true);
        }
    }, [focusParam, isSubmitted, editMode, resumePending]);
    const focusJumpDoneRef = useRef(false);
    useEffect(() => {
        if (!focusParam || focusJumpDoneRef.current || loading || resumePending) return;
        if (isSubmitted && !editMode) return; // Submission Hub still showing — wait for the effect above
        const target = experience.readinessModel.find((item) => item.id === focusParam);
        if (!target) return;
        focusJumpDoneRef.current = true;
        focusRequirementItem(target);
    }, [focusParam, isSubmitted, editMode, loading, resumePending, experience.readinessModel, focusRequirementItem]);

    // WhatsApp project-message deep link (2026-08-31) — "?material=1" opens
    // the SAME Audition Material modal the "View Audition Material" button
    // already opens (setShowMaterial), automatically on load instead of
    // requiring a tap first. The WhatsApp Casting Call template's second
    // link points here so a talent lands directly on the brief. No new
    // viewer, no new state — reuses showMaterial/MaterialModal unchanged.
    // Guarded the same way focusParam's own jump effects are (ref-once,
    // waits for the project fetch to finish) so it can never fire twice or
    // race project loading; absent this param, page load is unaffected.
    const materialParam = useMemo(
        () => (typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("material") : null),
        [],
    );
    const materialDeepLinkDoneRef = useRef(false);
    useEffect(() => {
        if (!materialParam || materialDeepLinkDoneRef.current || loading || !project) return;
        materialDeepLinkDoneRef.current = true;
        setShowMaterial(true);
    }, [materialParam, loading, project]);

    // ---------------------------------------------------------------
    if (loading) {
        return (
            <div className="min-h-dvh flex items-center justify-center bg-gradient-to-b from-slate-50 to-white">
                <Loader2 className="w-6 h-6 animate-spin text-[#333333]" />
            </div>
        );
    }
    if (!project) {
        // Only a genuine backend 404 is "Project not found." Every transient
        // failure (network / timeout / offline / CORS / 5xx) shows a retryable
        // message instead of misreporting the project as missing.
        const isNotFound = loadError?.kind === "not_found";
        
        const getErrorMessage = () => {
            if (!loadError) return "We couldn't load this project. Please try again.";
            switch (loadError.kind) {
                case "not_found":
                    return "Project not found.";
                case "timeout":
                    return "Request timed out. Please check your connection and try again.";
                case "server_error":
                    return "The server encountered an error (5xx). Please try again later.";
                case "http_error":
                    return `HTTP error loading project (Status: ${loadError.status}).`;
                case "network":
                    return "Network connection failed. Please check if you are online and try again.";
                case "aborted":
                    return "Request was cancelled.";
                default:
                    return "We couldn't load this project. Please check your connection and try again.";
            }
        };

        return (
            <div className="min-h-dvh flex flex-col items-center justify-center bg-gradient-to-b from-slate-50 to-white text-[#333333] p-6 text-center gap-4">
                <p className="max-w-sm font-medium">{getErrorMessage()}</p>
                {!isNotFound && (
                    <button
                        type="button"
                        onClick={() => setReloadNonce((n) => n + 1)}
                        className="px-4 py-2 rounded-lg bg-[#333333] text-white text-sm font-medium hover:opacity-90 active:scale-95 transition"
                    >
                        Try again
                    </button>
                )}
            </div>
        );
    }

    if (adminMode && adminBootstrapping) {
        return (
            <div className="min-h-dvh flex flex-col items-center justify-center bg-gradient-to-b from-slate-50 to-white gap-3">
                <Loader2 className="w-6 h-6 animate-spin text-[#333333]" />
                <p className="text-sm text-[#666666]">Starting submission…</p>
            </div>
        );
    }
    if (adminMode && adminBootstrapError) {
        return (
            <div className="min-h-dvh flex flex-col items-center justify-center bg-gradient-to-b from-slate-50 to-white text-[#333333] p-6 text-center gap-4">
                <p className="max-w-sm font-medium">{adminBootstrapError}</p>
                <button
                    type="button"
                    onClick={() => window.location.reload()}
                    className="px-4 py-2 rounded-lg bg-[#333333] text-white text-sm font-medium hover:opacity-90 active:scale-95 transition"
                >
                    Try again
                </button>
            </div>
        );
    }

    // Status system — reused as-is, only extended with the one value
    // (`selected`) it was already missing (confirmed via
    // draft-talent-migration's own `$in` query in server.py: submitted /
    // updated / retest / shortlisted / selected / rejected / approved are
    // all real values `submission.status` already takes; "selected" was
    // simply never given a label here). No new status enum — this is
    // completing coverage of the existing one, in the exact switch pattern
    // that was already here.
    const getStatusLabel = () => {
        const status = submission?.status;
        if (status === "updated") return "Resubmitted";
        if (status === "retest") return "Retest Requested";
        if (status === "approved") return "Approved";
        if (status === "shortlisted") return "Shortlisted";
        if (status === "selected") return "Selected";
        if (status === "rejected") return "Closed";
        return "Submitted";
    };

    const getStatusStyles = () => {
        const status = submission?.status;
        if (status === "retest") return "bg-rose-50 border border-rose-200 text-rose-700";
        if (status === "approved" || status === "shortlisted" || status === "selected") return "bg-emerald-50 border border-emerald-200 text-emerald-700";
        if (status === "rejected") return "bg-slate-100 border border-slate-200 text-slate-600";
        return "bg-slate-50 border border-slate-200 text-[#333333]";
    };

    // Status-aware confirmation copy (P0‑2). Same switch, same status
    // values, one more line per status — not a second status system.
    const getStatusMessage = () => {
        const status = submission?.status;
        if (status === "retest") return "Action required. Please update the requested items and resubmit.";
        if (status === "approved") return "Congratulations. Your submission has been approved.";
        if (status === "shortlisted") return "Congratulations. Your submission has been shortlisted.";
        if (status === "selected") return "Congratulations! You've been selected.";
        if (status === "rejected") return "Thank you for your submission.";
        if (status === "updated") return "Your updated submission has been received and is under review.";
        return "Your submission has been received and is under review.";
    };

    const statusLabel = getStatusLabel();
    const statusClass = getStatusStyles();
    const statusMessage = getStatusMessage();

    const lastUpdated = formatMediaTimestamp({
        updated_at: submission?.updated_at,
        created_at: submission?.created_at
    });

    // Hoisted so both the Hub and the edit-mode view below can render the
    // *same* banner/feedback markup (P0‑1) — one JSX value, two read sites,
    // never two copies. `feedback` reuses the existing
    // `submission.client_feedback` field the Hub always fetched; no new
    // fetch, no new shape.
    const feedback = submission?.client_feedback || [];
    const isRetest = submission?.status === "retest";

    const retestBannerEl = isRetest && (
        <div
            className="mb-8 bg-rose-50/60 border border-rose-200 rounded-3xl p-6 text-left animate-in fade-in slide-in-from-top-4 duration-250"
            role="alert"
            data-testid="retest-banner"
        >
            <div className="flex items-start gap-3">
                <span className="shrink-0 w-6 h-6 rounded-full bg-rose-500 text-white flex items-center justify-center font-bold text-xs shadow-sm mt-0.5">!</span>
                <div>
                    <h4 className="font-semibold text-sm text-rose-950">Action Required: Retest Request</h4>
                    <p className="text-xs text-rose-800 leading-relaxed mt-1">
                        The casting team has requested a retest or additional takes for your audition. Please review
                        the feedback below, record your updates, and click "Update Submission" to submit your new takes.
                    </p>
                </div>
            </div>
        </div>
    );

    // Reordered ahead of the status/CTA card (P1‑5) — a talent should read
    // *why* before deciding what to do about it, whether that's a retest or
    // just checking in. Same feedback list, same empty state, same
    // FeedbackRow — only its position on the page changed.
    const feedbackSectionEl = (
        <section
            className="mb-10"
            aria-label="Client feedback and reviews"
            data-testid="talent-feedback-section"
        >
            <h2 className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#333333] mb-4">Client Feedback &amp; Reviews</h2>
            {feedback.length === 0 ? (
                <div
                    className="bg-white/40 rounded-2xl p-6 text-[13px] leading-relaxed text-[#333333] border border-[#eaeaea]/60"
                    data-testid="talent-feedback-empty"
                >
                    No reviews yet — the team will share notes here
                    once a client responds.
                </div>
            ) : (
                <div className="space-y-4">
                    {feedback.map((f) => (
                        <FeedbackRow key={f.id} fb={f} />
                    ))}
                </div>
            )}
        </section>
    );

    // Submitted Content Summary (P1‑4) — the exact same `experience.checklist`
    // / `experience.overallProgress` the edit-mode form's own readiness panel
    // already renders (useSubmissionExperienceModel, computed once above,
    // unconditionally). No second calculation, no new engine — a read-only
    // render of state that already existed.
    const contentSummaryEl = experience.checklist.length > 0 && (
        <div className="mb-10" data-testid="hub-content-summary">
            <SubmissionReadinessPanel
                title="What You've Submitted"
                items={experience.checklist}
                progress={experience.overallProgress}
                testId="hub-readiness-panel"
            />
        </div>
    );

    // Dashboard back-link (P1‑3) — the Portal's own header pattern (a
    // clickable Logo, see DashboardLayout.jsx) reused here as a plain
    // anchor: `/submit/{slug}` is a separate Next.js route outside the
    // Portal's react-router tree (same reasoning already established for
    // ProjectDetail's Quick Actions link), so a hard `<a>` is the correct
    // analog, not <NavLink>.
    const dashboardLinkEl = (
        <a
            href="/portal/home"
            className="inline-flex items-center gap-1.5 text-xs text-[#333333] hover:text-[#111111] transition-colors duration-150 w-fit"
            data-testid="hub-dashboard-link"
        >
            <ArrowLeft className="w-3.5 h-3.5" />
            Dashboard
        </a>
    );

    // ---------------------------------------------------------------
    // SUBMITTED / UPDATED / RETEST state — permanent Submission Hub dashboard
    if (isSubmitted && !editMode) {
        // Thank You screen — confirmation + the actions that matter next.
        // Project details stay reachable here too (via the same "View
        // Project Details" toggle used during the form) rather than
        // disappearing the moment a submission lands; portfolio/media
        // management is explicitly NOT this screen's job — "Update
        // Portfolio" hands off to the existing talent dashboard for that.
        return (
            <main className="min-h-dvh bg-gradient-to-b from-slate-50 via-white to-slate-50/30 text-[#111111] relative overflow-hidden" data-testid="submission-thank-you">
                {adminMode && <AdminModeBanner talentName={adminTalentName} />}
                <div className="absolute inset-0 pointer-events-none opacity-20 blur-3xl bg-[#0c2340]/20" />
                <div className="max-w-xl mx-auto px-4 sm:px-6 py-16 md:py-24 tg-fade-up">
                    <div className="bg-white/80 backdrop-blur-sm rounded-3xl p-10 border border-[#eaeaea]/60 shadow-[0_20px_40px_-12px_rgba(0,0,0,0.05)] text-center">
                        <div className="relative w-20 h-20 mx-auto mb-8">
                            <div className="absolute inset-0 rounded-full bg-emerald-100/60 blur-xl animate-pulse" />
                            <div className="relative w-full h-full rounded-full bg-emerald-50 border border-emerald-100 flex items-center justify-center shadow-sm">
                                <Check className="w-8 h-8 text-emerald-600" />
                            </div>
                        </div>

                        <h1 className="font-display text-4xl md:text-5xl tracking-tight text-[#111111] mb-4 leading-[1.05]">
                            Thank You
                        </h1>
                        <p className="text-[15px] leading-relaxed text-[#333333] mb-6">
                            Your submission has been received.
                        </p>

                        <button
                            type="button"
                            onClick={() => setShowProjectDetails((s) => !s)}
                            data-testid="thank-you-view-project-details-btn"
                            className="inline-flex items-center gap-1.5 mx-auto mb-6 px-4 py-2 rounded-full border border-[#eaeaea] hover:border-[#d4d4d4] text-[12px] font-semibold text-[#111111] transition-all duration-150"
                        >
                            {showProjectDetails ? "Hide" : "View"} Project Details
                            <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-200 ${showProjectDetails ? "rotate-180" : ""}`} />
                        </button>

                        {showProjectDetails && (
                            <div className="text-left mb-8 p-5 rounded-2xl bg-slate-50/70 border border-[#eaeaea]/60">
                                <h2 className="font-display text-xl tracking-tight text-[#111111] mb-4">
                                    Talentgram × {project.brand_name}
                                </h2>
                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-4">
                                    <Info label="Character" value={project.character} />
                                    <Info label="Shoot Dates" value={project.shoot_dates} />
                                    {project.budget_per_day && (
                                        <Info label="Budget Per Day" value={project.budget_per_day} />
                                    )}
                                    <Info label="Director" value={project.director} />
                                    <Info label="Production House" value={project.production_house} />
                                    <Info label="Commission" value={project.commission_percent} />
                                    <Info label="Medium / Usage" value={project.medium_usage} wide />
                                </div>
                                {project.additional_details && (
                                    <div className="mt-4 pt-4 border-t border-slate-200">
                                        <p className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono mb-2">
                                            Additional Details
                                        </p>
                                        <p className="text-[13px] leading-relaxed text-[#222222] whitespace-pre-line">
                                            {project.additional_details}
                                        </p>
                                    </div>
                                )}
                            </div>
                        )}

                        <div className="flex flex-col gap-3">
                            <a
                                href="/portal/home"
                                data-testid="thank-you-dashboard-link"
                                className="w-full bg-slate-900 text-white py-3.5 px-6 rounded-full text-xs font-semibold hover:bg-slate-800 hover:-translate-y-[1px] active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 shadow-sm"
                            >
                                Update Portfolio
                            </a>
                            <button
                                type="button"
                                onClick={() => setEditMode(true)}
                                data-testid="update-submission-hub-btn"
                                className="w-full border border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111] py-3.5 px-6 rounded-full text-xs font-semibold transition-all duration-150 inline-flex items-center justify-center gap-1.5"
                            >
                                Update Submission
                            </button>
                        </div>
                    </div>
                </div>
            </main>
        );
    }

    // Recurring-talent field relocation — DOB/Age/Height/Instagram, plus the
    // project-specific Age Override, are defined ONCE here (same fields,
    // same state, same handlers as always) and then rendered in exactly one
    // place below depending on isReturningTalent: inside "Your Profile" for
    // a first-time talent (unchanged), or at the top of "Project Questions"
    // for a returning talent (who never sees "Your Profile" at all, and so
    // previously had no way to fill these fields when a project required
    // them). Never both at once, so there is no duplicate field/state.
    const dobField = !isFieldHidden("dob") && (
        <PremiumFormField
            // Only the returning-talent (Project Questions) placement grows a
            // dynamic required marker, matching Height's existing pattern —
            // new-talent "Your Profile" keeps the exact same static label it
            // always had.
            label={isReturningTalent ? `Date of Birth ${isFieldRequired("dob") ? "*" : "(optional)"}` : "Date of Birth"}
            type="date"
            value={form.dob}
            max={new Date().toISOString().split("T")[0]}
            onChange={(v) =>
                setForm({ ...form, dob: v, age: "" })
            }
            onBlur={saveForm}
            testid="form-dob"
            className="[color-scheme:light]"
            autoComplete="bday"
            required={isFieldRequired("dob")}
            hint="Format: DD / MM / YYYY. We automatically calculate age from your date of birth."
        />
    );

    const overrideAgeBlock = (
        <div className="mt-4 p-5 rounded-2xl bg-slate-50/50 border border-[#eaeaea]/50 focus-within:border-[#0c2340]/40 focus-within:ring-4 focus-within:ring-[#0c2340]/5 transition-all duration-300 col-span-1 md:col-span-2">
            <label className="flex items-center gap-3 cursor-pointer min-h-[44px]">
                <input
                    type="checkbox"
                    checked={form.overrideAge || false}
                    onChange={(e) => {
                        const active = e.target.checked;
                        setForm({
                            ...form,
                            overrideAge: active,
                            submitted_age_override: active ? (form.submitted_age_override || String(computedAge || "")) : ""
                        });
                        setTimeout(saveForm, 0);
                    }}
                    data-testid="form-override-age-checkbox"
                    className="w-5 h-5 rounded border-[#d4d4d4] text-[#0c2340] focus:ring-[#0c2340] focus:ring-2 cursor-pointer transition duration-150 ease-in-out"
                />
                <span className="text-sm font-medium text-[#111111] select-none">
                    Use different age for this project?
                </span>
            </label>

            {form.overrideAge && (
                <div className="mt-4 animate-fadeIn transition-all duration-300">
                    <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                        Project-Specific Age Override *
                    </span>
                    <input
                        type="number"
                        inputMode="numeric"
                        pattern="[0-9]*"
                        value={form.submitted_age_override || ""}
                        onChange={(e) =>
                            setForm({
                                ...form,
                                submitted_age_override: e.target.value,
                            })
                        }
                        onBlur={saveForm}
                        min={10}
                        max={80}
                        placeholder="e.g. 25"
                        data-testid="form-override-age-input"
                        className="mt-2 w-full bg-white rounded-xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[15px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                    />
                    <p className="text-[10px] text-[#333333] font-mono mt-1.5">
                        Only use this if you wish to be presented as a different age range for this project. This override is isolated to this submission only.
                    </p>
                </div>
            )}
        </div>
    );

    const ageDisplayBlock = !isFieldHidden("age") && (
        <div data-testid="form-age-field">
            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                Age {form.dob ? "(auto calculated)" : isFieldRequired("age") ? "*" : "(optional)"}
            </span>
            <input
                type="number"
                value={
                    form.dob
                        ? (calcAge(form.dob) ?? "")
                        : form.age
                }
                disabled={true}
                min={10}
                max={80}
                data-testid="form-age-input"
                className="mt-2 w-full bg-slate-100 rounded-2xl border border-[#eaeaea] outline-none py-3 px-4 text-[15px] text-[#333333] shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
            />
        </div>
    );

    const heightBlock = !isFieldHidden("height") && (
        <div data-testid="form-height-field">
            <div className="flex flex-wrap items-center justify-between gap-y-2 gap-x-3">
                <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono whitespace-nowrap">
                    Height {isFieldRequired("height") ? "*" : "(optional)"}
                </span>
                {/* Sprint 1 — unit toggle. Stored value is unchanged; only the
                    labels switch between feet/inches and centimetres. */}
                <div
                    role="radiogroup"
                    aria-label="Height unit"
                    data-testid="height-unit-toggle"
                    className="inline-flex items-center rounded-full border border-[#eaeaea] bg-white p-0.5 shrink-0"
                >
                    {[
                        { key: "ft", label: "Feet/Inches" },
                        { key: "cm", label: "Centimeters" },
                    ].map((u) => (
                        <button
                            key={u.key}
                            type="button"
                            role="radio"
                            aria-checked={heightUnit === u.key}
                            onClick={() => setHeightUnit(u.key)}
                            data-testid={`height-unit-${u.key}`}
                            className={`px-3 py-1 rounded-full text-[11px] font-mono font-semibold transition-all duration-200 min-h-[28px] ${
                                heightUnit === u.key
                                    ? "bg-[#0c2340] text-white"
                                    : "text-[#333333] hover:text-[#111111]"
                            }`}
                        >
                            {u.label}
                        </button>
                    ))}
                </div>
            </div>
            <div className="mt-2">
                <Select
                    value={form.height || ""}
                    onValueChange={(v) => {
                        setForm({ ...form, height: v });
                        setTimeout(saveForm, 0);
                    }}
                >
                    <SelectTrigger
                        data-testid="form-height-trigger"
                        className="bg-white/60 border border-[#eaeaea] rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-[#111111] transition-all duration-200"
                    >
                        <SelectValue placeholder="Select height" />
                    </SelectTrigger>
                    <SelectContent className="max-h-72 bg-white border-[#eaeaea] rounded-2xl">
                        {heightUnit === "cm"
                            ? HEIGHT_CM_OPTIONS.map((h) => (
                                <SelectItem key={h.value} value={h.value}>
                                    {h.cm} cm
                                </SelectItem>
                            ))
                            : HEIGHT_OPTIONS.map((h) => (
                                <SelectItem key={h} value={h}>
                                    {h}
                                </SelectItem>
                            ))}
                    </SelectContent>
                </Select>
            </div>
            <span className="block text-[10px] text-[#333333] mt-1 font-mono">
                Enter your actual height without footwear.
            </span>
        </div>
    );

    const instagramHandleField = !isFieldHidden("instagram_handle") && (
        <PremiumFormField
            label={isReturningTalent ? `Instagram ${isFieldRequired("instagram_handle") ? "*" : "(optional)"}` : "Instagram Handle"}
            value={form.instagram_handle}
            onChange={(v) => {
                let clean = v.trim();
                if (clean.includes("instagram.com/")) {
                    const segments = clean.split("instagram.com/");
                    if (segments[1]) {
                        clean = segments[1].split(/[?#/]/)[0];
                    }
                }
                if (clean.startsWith("@")) {
                    clean = clean.substring(1);
                }
                clean = clean.replace(/\s+/g, "");
                setForm({ ...form, instagram_handle: clean });
            }}
            onBlur={() => {
                if (form.instagram_handle) {
                    setForm((prev) => ({
                        ...prev,
                        instagram_handle: normalizeInstagramHandle(form.instagram_handle)
                    }));
                }
                saveForm();
            }}
            testid="form-instagram-handle"
            placeholder="@yourhandle"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            required={isFieldRequired("instagram_handle")}
            hint={isFieldRequired("instagram_handle") ? "Required for this project." : "Optional, but helps casting teams review additional work."}
        />
    );

    const instagramFollowersBlock = !isFieldHidden("instagram_followers") && (
        <div data-testid="form-instagram-followers-field">
            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                Instagram Followers
            </span>
            <div className="mt-2">
                <Select
                    value={form.instagram_followers || ""}
                    onValueChange={(v) => {
                        setForm({
                            ...form,
                            instagram_followers: v,
                        });
                        setTimeout(saveForm, 0);
                    }}
                >
                    <SelectTrigger
                        data-testid="form-instagram-followers-trigger"
                        className="bg-white/60 border border-[#eaeaea] rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-[#111111] transition-all duration-200"
                    >
                        <SelectValue placeholder="Select range" />
                    </SelectTrigger>
                    <SelectContent className="max-h-72 bg-white border-[#eaeaea] rounded-2xl">
                        {FOLLOWER_TIERS.map((tier) => (
                            <SelectGroup key={tier.label}>
                                <SelectLabel className="text-[10px] tracking-wide uppercase text-[#333333] font-mono">
                                    {tier.label}
                                </SelectLabel>
                                {tier.items.map((it) => (
                                    <SelectItem
                                        key={it}
                                        value={it}
                                    >
                                        {it}
                                    </SelectItem>
                                ))}
                                <SelectSeparator />
                            </SelectGroup>
                        ))}
                    </SelectContent>
                </Select>
            </div>
        </div>
    );

    // Same field/state/handler as the "Skills & Attributes" step's own copy
    // below — defined once here so a returning talent's one-page form (see
    // recurringProfileFields) can show it inline with the project questions
    // instead of on a separate step they never visit, with zero duplicate
    // state.
    const workLinksBlock = requirements.work_links_visibility !== REQUIREMENT_TIERS.HIDDEN && (
        <div className="md:col-span-2" data-testid="form-work-links-field">
            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                Work Links (optional)
            </span>
            <p className="text-[12px] text-[#666] mt-1 mb-2 leading-relaxed">Add links to your professional websites or reels to showcase your previous work.</p>
            <WorkLinksEditor
                links={form.work_links || []}
                onChange={(arr) => {
                    setForm({ ...form, work_links: arr });
                    setTimeout(saveForm, 0);
                }}
            />
        </div>
    );

    // Same field/state/handler as the "Skills & Attributes" step's own
    // <SkillsSelector> — defined once here, same pattern as workLinksBlock
    // above, so a returning talent's one-page form can show it inline on
    // Project Questions instead of on the standalone step they never visit
    // (see recurringProfileFields). Reuses the existing SkillsSelector
    // component unchanged; the only addition is a plain-text line naming
    // any mandatory categories, using the same label/hint styling already
    // used for Work Links right above — no new visual language.
    const mandatorySkillCategories = Object.keys(requirements.skills || {}).filter((cat) => requirements.skills[cat]);
    const skillsFieldBlock = (
        <div className="md:col-span-2" data-testid="form-skills-field">
            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono block mb-2">
                Skills & Special Abilities
            </span>
            {mandatorySkillCategories.length > 0 && (
                <p className="text-[12px] text-[#666] mt-1 mb-2 leading-relaxed">
                    Required: select at least one skill from {mandatorySkillCategories.map((c) => `"${c}"`).join(", ")}.
                </p>
            )}
            <SkillsSelector
                selectedSkills={form.skills || []}
                onChange={(arr) => {
                    setForm((prev) => ({ ...prev, skills: arr }));
                    setTimeout(saveForm, 0);
                }}
            />
        </div>
    );

    return (
        <main className="min-h-dvh bg-gradient-to-b from-slate-50 via-white to-slate-50/30 text-[#111111] relative overflow-hidden" data-testid="submission-page">
            {adminMode && <AdminModeBanner talentName={adminTalentName} />}
            {/* Submission Readiness Dashboard (item 6) — pinned right under the
                banner so an admin sees it before scrolling into the form,
                instead of only inline further down. Same live experience
                model (useSubmissionExperienceModel → requirementEngine.js)
                every other instance of this panel already renders from — no
                separate validation logic, just an earlier render of it. */}
            {adminMode && emailGateUnlocked && (
                <div className="max-w-2xl mx-auto px-5 pt-4" data-testid="admin-pinned-readiness">
                    <SubmissionReadinessPanel
                        items={experience.checklist}
                        onItemClick={focusRequirementItem}
                        saveStatus={experience.saveStatus}
                        progress={experience.overallProgress}
                    />
                </div>
            )}
            {/* Submission Wizard progress — visible once the email gate has
                resolved (before that there's nothing to navigate yet). Not
                sticky in Admin Mode (see WizardProgressBar's own comment) —
                the AdminModeBanner already owns that position there. */}
            {emailGateUnlocked && (
                <WizardProgressBar
                    steps={wizardDisplaySteps}
                    currentStep={currentStep}
                    stepStatusById={wizardStepStatusById}
                    onStepClick={handleWizardStepJump}
                    sticky={!adminMode}
                />
            )}
            {/* Ambient luxury background blobs */}
            <div className="absolute inset-0 pointer-events-none opacity-30 blur-3xl">
                <div className="absolute top-0 -left-40 w-80 h-80 rounded-full bg-[#0c2340]/10 mix-blend-multiply animate-blob" />
                <div className="absolute bottom-0 -right-40 w-80 h-80 rounded-full bg-slate-200/40 mix-blend-multiply animate-blob animation-delay-2000" />
            </div>

            {/* Marketing/orientation header — Logo, Instagram, trust copy.
                Only meaningful BEFORE the talent has committed to the
                wizard (deciding whether to engage at all); once
                `emailGateUnlocked` is true they're mid-task and this is
                exactly the "large project header... clutter" the
                simplified wizard UX explicitly removes so only the
                progress indicator + current step remain. Not rendered at
                all in that state, not just visually collapsed. */}
            {!emailGateUnlocked && (
                <header className="relative w-full pt-10 pb-8 px-5 border-b border-[#eaeaea]/60 bg-white/40">
                    <div className="absolute top-5 right-5 z-40">
                        <ThemeToggle size="sm" />
                    </div>
                    {isSubmitted && (
                        <div className="absolute top-5 left-5 z-40">
                            {dashboardLinkEl}
                        </div>
                    )}
                    <div className="max-w-2xl mx-auto flex flex-col items-center text-center">
                        {/* Centered Logo — links back to the Dashboard, same as
                            the Portal's own header (DashboardLayout.jsx) */}
                        <div className="mb-4">
                            <a href="/portal/home" data-testid="edit-mode-logo-link">
                                <Logo size={76} className="mx-auto" />
                            </a>
                        </div>

                        {/* Clickable Instagram icon */}
                        <div className="mb-4">
                            <a
                                href="https://www.instagram.com/talentgram.agency/"
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center justify-center p-2 rounded-full text-[#111111] hover:bg-slate-100 transition-all duration-200 cursor-pointer group"
                                title="Follow us on Instagram"
                            >
                                <svg
                                    className="w-5 h-5 transition-colors duration-200 hover:text-[#E1306C] md:group-hover:text-[#E1306C]"
                                    viewBox="0 0 24 24"
                                    fill="none"
                                    stroke="currentColor"
                                    strokeWidth="2"
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                >
                                    <rect x="2" y="2" width="20" height="20" rx="5" ry="5" />
                                    <path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z" />
                                    <line x1="17.5" y1="6.5" x2="17.51" y2="6.5" />
                                </svg>
                            </a>
                        </div>

                        {/* Trust and Credibility Copy */}
                        <div className="max-w-md mx-auto">
                            <p className="text-[13px] font-semibold text-[#111111] tracking-tight leading-relaxed">
                                Complete your profile and upload your audition materials.
                            </p>
                            <p className="text-[11px] text-[#333333] font-medium leading-relaxed mt-1">
                                Your submission will be reviewed by the Talentgram team.
                            </p>
                        </div>
                    </div>
                </header>
            )}

            <div data-testid="submission-content" className="max-w-2xl mx-auto px-4 sm:px-6 md:px-8 py-6 md:py-10 pb-28 sm:pb-10" {...wizardSwipeHandlers}>
                {/* Retest context + client feedback (P0‑1) — the same
                    banner/feedback JSX the Hub renders (hoisted above,
                    before the Hub/edit-mode branch split), placed first in
                    the edit flow so a returning talent reads *why* they're
                    updating before they reach a single form field. Only
                    shown when editing an existing submission — a brand-new
                    talent (`!isSubmitted`) has no status or feedback to
                    show yet. */}
                {isSubmitted && (
                    <div className="mb-8">
                        {retestBannerEl}
                        {feedbackSectionEl}
                    </div>
                )}

                {/* SECTION 1 — Project Info. Only rendered pre-gate: once the
                    talent has clicked UPLOAD TEST (or been silently
                    recognized), this whole "large project header, project
                    information blocks, duplicated project details" card is
                    exactly the clutter the simplified wizard removes — the
                    talent has already decided to apply, so Character/Shoot
                    Dates/Budget/Director/Production House/Additional
                    Details no longer serve the current task. Not just
                    visually collapsed — unmounted, so only the progress
                    indicator + current step remain. */}
                {/* Project details are ALWAYS reachable — before auth (fully
                    expanded, first thing a talent reads), during the form
                    (collapsed behind "View Project Details" so it doesn't
                    compete with the fields), and on the Thank You screen
                    (same collapsed toggle, see the isSubmitted branch below).
                    Previously this whole section unmounted the instant
                    `emailGateUnlocked` became true — that's the exact
                    "project details disappear after submission" complaint
                    this fixes. */}
                <section className="mb-8 bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/60 shadow-[0_4px_20px_rgba(15,23,42,0.04)]" data-testid="project-info-section" data-step="1">
                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340] mb-4">Audition Brief</p>
                    <div className={emailGateUnlocked ? "" : "mb-8 border-b border-slate-100 pb-4"}>
                        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                            <div className="flex flex-col gap-1">
                                <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#333333]">PROJECT</p>
                                <h1 className="font-display text-2xl sm:text-3xl md:text-4xl tracking-tight text-[#111111] leading-[1.05]">
                                    Talentgram × {project.brand_name}
                                </h1>
                            </div>
                            {emailGateUnlocked && (
                                <button
                                    type="button"
                                    onClick={() => setShowProjectDetails((s) => !s)}
                                    data-testid="view-project-details-btn"
                                    className="shrink-0 inline-flex items-center gap-1.5 px-4 py-2 rounded-full border border-[#eaeaea] hover:border-[#d4d4d4] text-[12px] font-semibold text-[#111111] transition-all duration-150"
                                >
                                    {showProjectDetails ? "Hide" : "View"} Project Details
                                    <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-200 ${showProjectDetails ? "rotate-180" : ""}`} />
                                </button>
                            )}
                        </div>
                        {!emailGateUnlocked && hasAuditionMaterial && (
                            <div className="mt-4">
                                <button
                                    onClick={() => setShowMaterial(true)}
                                    data-testid="view-audition-material-btn"
                                    className="inline-flex items-center gap-2 px-5 py-2.5 border border-[#0c2340] hover:border-[#0c2340] hover:bg-[#0c2340]/[0.08] active:scale-[0.98] rounded-full text-[13px] text-[#0c2340] font-semibold transition-all hover:shadow-md hover:-translate-y-[1px] bg-[#0c2340]/[0.04]"
                                >
                                    <FolderOpen className="w-4 h-4 text-[#0c2340]" /> View Audition Material
                                </button>
                                {auditionMaterialSummary && (
                                    <p className="mt-2 ml-1 text-[11px] text-[#666666] tracking-wide">
                                        {auditionMaterialSummary}
                                    </p>
                                )}
                            </div>
                        )}
                    </div>
                    {(!emailGateUnlocked || showProjectDetails) && (
                    <>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-5 border-t border-[#eaeaea]/50 pt-6 mt-6">
                        <Info label="Character" value={project.character} />
                        <Info label="Shoot Dates" value={project.shoot_dates} />
                        {project.budget_per_day && (
                            <Info label="Budget Per Day" value={project.budget_per_day} />
                        )}
                        {/* Phase 1 (v37): expose Director + Production House so
                            talents see who's behind the project before they
                            invest time. Both fields already exist in the
                            project schema — this is rendering-only. `<Info>`
                            already auto-hides when value is empty, so no
                            extra guard needed. */}
                        <Info label="Director" value={project.director} />
                        <Info label="Production House" value={project.production_house} />
                        <Info label="Commission" value={project.commission_percent} />
                        <Info label="Medium / Usage" value={project.medium_usage} wide />
                    </div>
                    {project.additional_details && (
                        <div className="mt-6 pt-4 border-t border-slate-100">
                            <p className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono mb-2">
                                Additional Details
                            </p>
                            <p className="text-[13px] leading-relaxed text-[#222222] whitespace-pre-line">
                                {project.additional_details}
                            </p>
                        </div>
                    )}
                    {emailGateUnlocked && hasAuditionMaterial && (
                        <div className="mt-6 pt-4 border-t border-slate-100">
                            <button
                                onClick={() => setShowMaterial(true)}
                                data-testid="view-audition-material-btn"
                                className="inline-flex items-center gap-2 px-5 py-2.5 border border-[#0c2340] hover:border-[#0c2340] hover:bg-[#0c2340]/[0.08] active:scale-[0.98] rounded-full text-[13px] text-[#0c2340] font-semibold transition-all hover:shadow-md hover:-translate-y-[1px] bg-[#0c2340]/[0.04]"
                            >
                                <FolderOpen className="w-4 h-4 text-[#0c2340]" /> View Audition Material
                            </button>
                            {auditionMaterialSummary && (
                                <p className="mt-2 ml-1 text-[11px] text-[#666666] tracking-wide">
                                    {auditionMaterialSummary}
                                </p>
                            )}
                        </div>
                    )}
                    </>
                    )}
                    {/* Dominant CTA — clicking recognizes a returning talent
                        (trusted device first, then portal token — no OTP, no
                        retyped profile) and jumps straight to the project
                        form; a new/unrecognized talent is scrolled to the
                        existing email/OTP entry below. See
                        handleUploadTestClick. */}
                    {!emailGateUnlocked && (
                        <div className="mt-8 pt-6 border-t border-slate-100">
                            <button
                                type="button"
                                onClick={handleUploadTestClick}
                                disabled={recognizing}
                                data-testid="upload-test-cta"
                                className="w-full sm:w-auto inline-flex items-center justify-center gap-2 px-8 py-4 rounded-full bg-slate-950 text-white text-[15px] font-semibold tracking-wide shadow-[0_4px_20px_rgba(15,23,42,0.15)] hover:bg-[#0c2340] active:scale-[0.98] transition-all duration-200 disabled:opacity-60 min-h-[52px]"
                            >
                                {recognizing ? (
                                    <>
                                        <Loader2 className="w-4 h-4 animate-spin" /> Recognizing you…
                                    </>
                                ) : (
                                    <>SUBMIT FORM</>
                                )}
                            </button>
                        </div>
                    )}
                </section>

                {/* Simplified-wizard UX (2026-08): the full multi-item
                    readiness checklist is no longer shown DURING the active
                    wizard — it lists every requirement across every step at
                    once, which is exactly the "overwhelm the talent with
                    the entire application" this simplification removes.
                    `WizardProgressBar` (rendered once, sticky, at the very
                    top of the page) is now the single persistent
                    status/navigation element; per-field validation still
                    surfaces inline on whichever single step it belongs to.
                    The panel component itself is untouched — Admin Mode's
                    pinned summary above still uses it, a deliberate
                    power-user carve-out, not a talent-facing screen. */}

                {/* SECTION 2 — TALENT DETAILS FORM. Manual-testing fix: the
                    first project page must show ONLY Project Details +
                    "UPLOAD TEST" — no auth UI, no Profile/Skills/Project
                    Questions — until the talent explicitly clicks UPLOAD
                    TEST (or is silently recognized, or Admin Mode/a resumed
                    draft already unlocked the gate on mount). See
                    `talentDetailsRevealed`'s declaration for the full
                    rationale. */}
                {(talentDetailsRevealed || emailGateUnlocked) && (
                <section
                    className="pt-4 mb-10 sm:mb-16"
                    data-testid="talent-details-section"
                    data-step="1-2"
                >
                    <div className="bg-white rounded-3xl p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)]">
                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340]/70 mb-4" data-step="1">Talent Details</p>
                    <h2 className="font-display text-2xl md:text-3xl tracking-tight text-[#111111] mb-3 leading-[1.05]" data-step="1">
                        Your profile.
                    </h2>
                    <p className="text-[13px] leading-relaxed text-[#333333] mb-10" data-step="1">
                        All fields are required unless marked optional.
                    </p>
                    <form onSubmit={startSubmission} className="space-y-8">
                        {/* Phase 1 — email-first identity. The email field
                            anchors the form so we can prefill known talents
                            BEFORE they retype everything. UX-polish fix —
                            email is an identity/auth mechanism, not a
                            project submission field: once
                            `emailGateUnlocked` is true there is nothing to
                            render here at all (see the removed "Locked
                            Email State" branch below this used to render an
                            always-visible, editable "Email *" field on
                            every single step of the wizard, not just the
                            first — form.email stays correct internally via
                            the OTP/token/new-talent-unlock paths that set it
                            without ever needing a visible re-editable
                            field). */}
                        <div data-step="1">
                            {!emailGateUnlocked && (
                                otpSent ? (
                                    /* Step A.5: OTP Verification Input */
                                    <div className="flex flex-col gap-4 animate-in fade-in duration-200 text-left">
                                        <div className="flex flex-col gap-1.5">
                                            <label className="text-xs font-semibold text-[#111111] uppercase tracking-wider">
                                                Enter Verification Code
                                            </label>
                                            <p className="text-xs text-[#333333] leading-normal">
                                                We've sent a verification code to <span className="font-semibold text-slate-900">{gatewayEmail}</span>
                                            </p>
                                        </div>
                                        <div className="flex flex-col sm:flex-row gap-3">
                                            <input
                                                type="text"
                                                inputMode="numeric"
                                                pattern="[0-9]*"
                                                maxLength={6}
                                                value={otpValue}
                                                onChange={(e) => setOtpValue(e.target.value.replace(/\D/g, ''))}
                                                onKeyDown={(e) => {
                                                    if (e.key === "Enter") {
                                                        e.preventDefault();
                                                        handleVerifyOtp();
                                                    }
                                                }}
                                                placeholder="6-digit code"
                                                style={{ fontSize: "16px" }}
                                                className="flex-1 px-4 py-2.5 bg-white border border-[#eaeaea] rounded-xl text-[#111111] placeholder:text-[#333333] focus:border-slate-500 focus:outline-none transition duration-150 h-[44px]"
                                                disabled={otpLoading}
                                            />
                                            <div className="flex gap-2">
                                                <button
                                                    type="button"
                                                    onClick={handleVerifyOtp}
                                                    disabled={otpLoading}
                                                    className="bg-slate-900 text-white px-5 py-2.5 rounded-xl text-xs font-medium hover:bg-slate-800 active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 min-w-[100px] h-[44px] cursor-pointer"
                                                >
                                                    {otpLoading ? "Verifying..." : "Verify"}
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={handleResendOtp}
                                                    disabled={otpResending || otpLoading}
                                                    className="bg-white border border-[#eaeaea] hover:bg-slate-50 text-[#111111] text-xs font-medium px-4 py-2.5 rounded-xl transition duration-150 h-[44px] cursor-pointer"
                                                >
                                                    {otpResending ? "Resending..." : "Resend OTP"}
                                                </button>
                                            </div>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => {
                                                setOtpSent(false);
                                                setOtpValue("");
                                            }}
                                            className="text-left text-xs text-slate-500 hover:text-slate-900 transition underline cursor-pointer"
                                        >
                                            Change email address
                                        </button>
                                    </div>
                                ) : !gatewayRecognition ? (
                                    /* Step A: Inline Email Lookup */
                                    <div className="flex flex-col gap-4 animate-in fade-in duration-200 text-left">
                                        <button
                                            type="button"
                                            onClick={handleGoogleLogin}
                                            className="w-full bg-white border border-[#eaeaea] hover:bg-slate-50 text-[#111111] py-3 px-4 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-2.5 transition duration-150 shadow-sm active:scale-[0.98] cursor-pointer"
                                        >
                                            <svg className="w-4 h-4" viewBox="0 0 24 24">
                                                <path
                                                    fill="#EA4335"
                                                    d="M12 5.04c1.78 0 3.38.61 4.64 1.8l3.46-3.46C17.99 1.19 15.21 0 12 0 7.31 0 3.28 2.69 1.34 6.61l4.08 3.16C6.4 7.02 9.01 5.04 12 5.04z"
                                                />
                                                <path
                                                    fill="#4285F4"
                                                    d="M23.49 12.27c0-.81-.07-1.59-.2-2.36H12v4.51h6.46c-.29 1.48-1.14 2.73-2.4 3.58l3.73 2.89c2.18-2.01 3.7-4.97 3.7-8.62z"
                                                />
                                                <path
                                                    fill="#FBBC05"
                                                    d="M5.42 14.78c-.24-.72-.38-1.49-.38-2.28s.14-1.56.38-2.28L1.34 7.06C.48 8.79 0 10.74 0 12.8s.48 4.01 1.34 5.74l4.08-3.76z"
                                                />
                                                <path
                                                    fill="#34A853"
                                                    d="M12 24c3.24 0 5.97-1.07 7.96-2.91l-3.73-2.89c-1.04.7-2.36 1.11-4.23 1.11-3.01 0-5.6-1.98-6.51-4.73L1.34 17.68C3.28 21.6 7.31 24 12 24z"
                                                />
                                            </svg>
                                            Continue with Google
                                        </button>
                                        <div className="flex items-center my-1.5">
                                            <div className="flex-grow border-t border-[#eaeaea]"></div>
                                            <span className="mx-4 text-[10px] text-[#888888] font-mono uppercase tracking-wider">or</span>
                                            <div className="flex-grow border-t border-[#eaeaea]"></div>
                                        </div>
                                        <div className="flex flex-col gap-1.5">
                                            <label className="text-xs font-semibold text-[#111111] uppercase tracking-wider">
                                                Continue with Email
                                            </label>
                                            <p className="text-xs text-[#333333] leading-normal">
                                                We use your email to recognise you and load any previously submitted details.
                                            </p>
                                        </div>
                                        <div className="flex flex-col sm:flex-row gap-3">
                                            <input
                                                type="email"
                                                value={gatewayEmail}
                                                onChange={(e) => setGatewayEmail(e.target.value)}
                                                onKeyDown={(e) => {
                                                    if (e.key === "Enter") {
                                                        e.preventDefault();
                                                        handleInlineLookup();
                                                    }
                                                }}
                                                placeholder="Enter your email address"
                                                style={{ fontSize: "16px" }}
                                                className="flex-1 px-4 py-2.5 bg-white border border-[#eaeaea] rounded-xl text-[#111111] placeholder:text-[#333333] focus:border-slate-500 focus:outline-none transition duration-150 h-[44px]"
                                                disabled={gatewayLoading}
                                            />
                                            <button
                                                type="button"
                                                onClick={handleInlineLookup}
                                                disabled={gatewayLoading}
                                                className="bg-slate-900 text-white px-5 py-2.5 rounded-xl text-xs font-medium hover:bg-slate-800 active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 min-w-[120px] h-[44px]"
                                            >
                                                {gatewayLoading ? "Verifying..." : "Continue"}
                                                <ArrowRight className="w-3.5 h-3.5" />
                                            </button>
                                        </div>
                                        <p className="text-[10px] text-[#333333] font-mono mt-1">
                                            We use your email to recognise you and load any previously submitted details.
                                        </p>
                                    </div>
                                ) : (
                                    /* Step B: Inline Cinematic Recognition */
                                    <div className="flex flex-col gap-5 border border-slate-100 rounded-2xl p-5 bg-slate-50/50 animate-in fade-in zoom-in-95 duration-200 text-left">
                                        <div className="flex items-center gap-4">
                                            {gatewayRecognition.image_url ? (
                                                <img
                                                    src={gatewayRecognition.image_url}
                                                    alt={gatewayRecognition.name}
                                                    className="w-20 h-20 rounded-full object-cover border border-[#eaeaea] shadow-sm shrink-0"
                                                />
                                            ) : (
                                                <div className="w-20 h-20 rounded-full bg-slate-200 flex items-center justify-center border border-[#d4d4d4] shrink-0">
                                                    <User className="w-8 h-8 text-[#333333]" />
                                                </div>
                                            )}
                                            <div className="text-left">
                                                <h4 className="font-semibold text-sm text-[#111111]">Is this you?</h4>
                                                <p className="text-xs text-[#333333] font-medium mt-1">
                                                    {gatewayRecognition.name} {(() => {
                                                        const locs = Array.isArray(gatewayRecognition.location) 
                                                            ? gatewayRecognition.location 
                                                            : (gatewayRecognition.location ? [{ city: gatewayRecognition.location }] : []);
                                                        return locs.length > 0 ? `· ${locs.map(l => l?.city || l).join(", ")}` : "";
                                                    })()}
                                                </p>
                                            </div>
                                        </div>

                                        <div className="flex flex-col items-stretch gap-2 pt-2 border-t border-[#eaeaea]/40">
                                            <button
                                                type="button"
                                                onClick={handleInlineContinue}
                                                disabled={gatewayLoading}
                                                className="w-full bg-slate-900 text-white px-4 py-2.5 rounded-xl text-xs font-semibold hover:bg-slate-850 active:scale-[0.98] transition-all duration-150 inline-flex items-center justify-center gap-1.5 h-[40px] disabled:opacity-60"
                                            >
                                                {gatewayLoading ? "Sending code…" : "Yes, that's me"}
                                                <ChevronRight className="w-3.5 h-3.5" />
                                            </button>
                                            <button
                                                type="button"
                                                onClick={handleInlineCancel}
                                                disabled={gatewayLoading}
                                                className="w-full text-[#333333] text-xs font-medium py-1.5 hover:text-[#111111] transition-colors"
                                            >
                                                Not you? Use another email
                                            </button>
                                        </div>
                                    </div>
                                )
                            )}
                        </div>



                        {emailGateUnlocked && (
                        <>
                        {/* Section 1: Your Profile */}
                        <div data-testid="profile-section" data-wizard-step="2" className={`bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)] mb-8 ${stepVisibilityClass(2)}`}>
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#eaeaea]/30">
                                <div>
                                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340] mb-1">Talent Profile</p>
                                    <h2 className="font-display text-2xl font-bold tracking-tight text-slate-950 leading-[1.05]">Your Profile</h2>
                                    <p className="text-[13px] text-[#222222] mt-1.5 leading-relaxed">Please confirm your personal details exactly as they should appear for casting.</p>
                                </div>
                                <div className="flex items-center gap-2 shrink-0">
                                    <SectionStatusBadge section={experience.sectionStatus.find((s) => s.section === "profile")} onClick={focusSection} />
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setCollapsedSections(prev => ({
                                                ...prev,
                                                profile: !prev.profile,
                                            }))
                                        }
                                        className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                        title={collapsedSections.profile ? "Expand Profile" : "Collapse Profile"}
                                    >
                                        <ChevronDown
                                            className={`h-4 w-4 transform transition-transform duration-200 ${
                                                collapsedSections.profile ? "-rotate-90" : ""
                                            }`}
                                        />
                                    </button>
                                </div>
                            </div>
                            
                            {!collapsedSections.profile && (
                                <div className="space-y-8 animate-fadeIn">
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-8" data-step="1">
                                        <PremiumFormField
                                            label="First Name *"
                                            value={form.first_name}
                                            onChange={(v) => {
                                                setForm({ ...form, first_name: v });
                                                if (validationErrors.first_name) setValidationErrors((e) => ({ ...e, first_name: undefined }));
                                            }}
                                            onBlur={saveForm}
                                            testid="form-first-name"
                                            required
                                            error={validationErrors.first_name}
                                            inputRef={(el) => { fieldRefs.current.first_name = el; }}
                                        />
                                        <PremiumFormField
                                            label="Last Name *"
                                            value={form.last_name}
                                            onChange={(v) => {
                                                setForm({ ...form, last_name: v });
                                                if (validationErrors.last_name) setValidationErrors((e) => ({ ...e, last_name: undefined }));
                                            }}
                                            onBlur={saveForm}
                                            testid="form-last-name"
                                            required
                                            error={validationErrors.last_name}
                                            inputRef={(el) => { fieldRefs.current.last_name = el; }}
                                        />
                                        {!isFieldHidden("phone") && (
                                        <PremiumFormField
                                            label="Phone Number (WhatsApp)"
                                            type="tel"
                                            value={form.phone}
                                            onChange={(v) =>
                                                setForm({ ...form, phone: v })
                                            }
                                            onBlur={saveForm}
                                            testid="form-phone"
                                            hint="Please enter the number that is active on WhatsApp. This will be used for casting communication and project updates."
                                        />
                                        )}
                                        <PremiumFormField
                                            label="Alternate Contact Number (optional)"
                                            type="tel"
                                            value={form.alternate_contact_number}
                                            onChange={(v) =>
                                                setForm({ ...form, alternate_contact_number: v })
                                            }
                                            onBlur={saveForm}
                                            testid="form-alt-phone"
                                            hint="Optional backup contact number."
                                        />
                                        {/* Recurring talents see DOB/Age Override/Age/Height on
                                            Project Questions instead (see recurringProfileFields
                                            below) — same fields defined once above, never both
                                            places at once. */}
                                        {!isReturningTalent && (
                                        <>
                                        {dobField}
                                        {overrideAgeBlock}
                                        {ageDisplayBlock}
                                        {heightBlock}
                                        </>
                                        )}
                                    </div>

                                    {/* Phase 2 — unified identity fields. Wrapped in the
                                        wizard's "Update my Profile" disclosure — these are
                                        the fields not explicitly named in Step 1's core
                                        list (Name/Age/Height/Phone/Location), so they're
                                        exactly the "everything else" the disclosure hides
                                        by default for a recognized returning talent. */}
                                    <UpdateProfileDisclosure defaultOpen={identityDisclosureOpen}>
                                    <div
                                        className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-8"
                                        data-step="1"
                                        data-testid="unified-identity-block"
                                    >
                                        {!isFieldHidden("gender") && (
                                        <div data-testid="form-gender-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Gender
                                            </span>
                                            <div className="mt-2 grid grid-cols-2 gap-2">
                                                {GENDER_OPTIONS.map((g) => {
                                                    const active = form.gender === g.key;
                                                    return (
                                                        <button
                                                            key={g.key}
                                                            type="button"
                                                            onClick={() => {
                                                                setForm({
                                                                    ...form,
                                                                    gender: active
                                                                        ? ""
                                                                        : g.key,
                                                                });
                                                                setTimeout(saveForm, 0);
                                                            }}
                                                            data-testid={`form-gender-${g.key}`}
                                                            className={`px-3 py-2.5 text-[12px] rounded-full border transition-all duration-200 min-h-[44px] active:scale-[0.97] ${
                                                                active
                                                                    ? "bg-slate-900 text-white border-slate-900 shadow-sm"
                                                                    : "bg-white/60 border-[#eaeaea] hover:border-[#d4d4d4] text-[#222222]"
                                                            }`}
                                                        >
                                                            {g.label}
                                                        </button>
                                                    );
                                                })}
                                            </div>
                                        </div>
                                        )}
                                        {!isFieldHidden("ethnicity") && (
                                        <div data-testid="form-ethnicity-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Ethnicity
                                            </span>
                                            <div className="mt-2">
                                                <Select
                                                    value={form.ethnicity || ""}
                                                    onValueChange={(v) => {
                                                        setForm({ ...form, ethnicity: v });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                >
                                                    <SelectTrigger
                                                        data-testid="form-ethnicity-trigger"
                                                        className="bg-white/60 border border-[#eaeaea] rounded-2xl px-4 py-3 min-h-[44px] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 shadow-[0_1px_2px_rgba(0,0,0,0.03)] text-[#111111] transition-all duration-200"
                                                    >
                                                        <SelectValue placeholder="Select ethnicity" />
                                                    </SelectTrigger>
                                                    <SelectContent className="max-h-72 bg-white border-[#eaeaea] rounded-2xl">
                                                        {ETHNICITY_OPTIONS.map((e) => (
                                                            <SelectItem
                                                                key={e.key}
                                                                value={e.key}
                                                                data-testid={`form-ethnicity-option-${e.key}`}
                                                            >
                                                                {e.label}
                                                            </SelectItem>
                                                        ))}
                                                    </SelectContent>
                                                </Select>
                                            </div>
                                        </div>
                                        )}
                                        {/* Recurring talents see Instagram on Project Questions
                                            instead (see recurringProfileFields below). */}
                                        {!isReturningTalent && (
                                        <>
                                        {instagramHandleField}
                                        {instagramFollowersBlock}
                                        </>
                                        )}
                                    </div>
                                    </UpdateProfileDisclosure>
                                </div>
                            )}
                        </div>

                        {/* Wizard Step 2: Skills & Attributes — Skills, Bio, and Work
                            Links relocated here (out of the Step 1 grid above, and out
                            of their old standalone "Section 3" card below) so they're
                            all one step's content. Purely a JSX relocation: same
                            fields, same handlers, same testids — nothing about what
                            they do or how they validate changed. */}
                        <div data-testid="skills-section" data-wizard-step="2" className={`bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)] mb-8 ${stepVisibilityClass(2)}`}>
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#eaeaea]/30">
                                <div>
                                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340] mb-1">Talent Profile</p>
                                    <h2 className="font-display text-2xl font-bold tracking-tight text-slate-950 leading-[1.05]">Skills & Attributes</h2>
                                    <p className="text-[13px] text-[#222222] mt-1.5 leading-relaxed">Tell us about your skills, abilities, and professional background.</p>
                                </div>
                                <div className="flex items-center gap-2 shrink-0">
                                    <SectionStatusBadge section={experience.sectionStatus.find((s) => s.section === "skills")} onClick={focusSection} />
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setCollapsedSections(prev => ({
                                                ...prev,
                                                skills: !prev.skills,
                                            }))
                                        }
                                        className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                        title={collapsedSections.skills ? "Expand Skills" : "Collapse Skills"}
                                    >
                                        <ChevronDown
                                            className={`h-4 w-4 transform transition-transform duration-200 ${
                                                collapsedSections.skills ? "-rotate-90" : ""
                                            }`}
                                        />
                                    </button>
                                </div>
                            </div>

                            {!collapsedSections.skills && (
                                <div className="space-y-8 animate-fadeIn">
                                    {/* Recurring talents see this same block inline on Project
                                        Questions instead (see recurringProfileFields below) —
                                        this step is never visible to them anyway, but guarded
                                        for consistency with every other relocated field. */}
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-8">
                                        {!isReturningTalent && skillsFieldBlock}
                                    </div>
                                    {/* Bio + Work Links behind the "Update my Profile"
                                        disclosure — Skills above stays always visible as
                                        the step's core content. */}
                                    <UpdateProfileDisclosure defaultOpen={skillsDisclosureOpen}>
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-8">
                                        {!isFieldHidden("bio") && (
                                        <label className="block md:col-span-2" data-testid="form-bio-field">
                                            <span className="text-[11px] text-[#333333] tracking-[0.2em] uppercase font-mono">
                                                Bio (optional)
                                            </span>
                                            <textarea
                                                value={form.bio}
                                                onChange={(e) =>
                                                    setForm({
                                                        ...form,
                                                        bio: e.target.value,
                                                    })
                                                }
                                                onBlur={saveForm}
                                                rows={3}
                                                maxLength={600}
                                                data-testid="form-bio"
                                                className="mt-2 w-full bg-white/60 rounded-2xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[15px] resize-none transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                placeholder="A short note about you (max 600 chars)"
                                            />
                                        </label>
                                        )}
                                        {!isReturningTalent && workLinksBlock}
                                    </div>
                                    </UpdateProfileDisclosure>
                                </div>
                            )}
                        </div>

                        {/* Section 2: Project Questions */}
                        <div data-step="2" data-wizard-step="1" data-testid="project-questions-section" className={`bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)] mb-8 ${stepVisibilityClass(1)}`}>
                            <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#eaeaea]/30">
                                <div>
                                    <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340] mb-1">Project Questions</p>
                                    <h2 className="font-display text-2xl font-bold tracking-tight text-slate-950 leading-[1.05]">Project Questions</h2>
                                    <p className="text-[13px] text-[#222222] mt-1.5 leading-relaxed">Please answer these project-specific questions and confirm your availability.</p>
                                </div>
                                <div className="flex items-center gap-2 shrink-0">
                                    <SectionStatusBadge section={experience.sectionStatus.find((s) => s.section === "projectQuestions")} onClick={focusSection} />
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setCollapsedSections(prev => ({
                                                ...prev,
                                                projectQuestions: !prev.projectQuestions,
                                            }))
                                        }
                                        className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                        title={collapsedSections.projectQuestions ? "Expand Project Questions" : "Collapse Project Questions"}
                                    >
                                        <ChevronDown
                                            className={`h-4 w-4 transform transition-transform duration-200 ${
                                                collapsedSections.projectQuestions ? "-rotate-90" : ""
                                            }`}
                                        />
                                    </button>
                                </div>
                            </div>

                            {!collapsedSections.projectQuestions && (
                                <div className="space-y-8 animate-fadeIn">
                                    {/* Recurring talents never see "Your Profile" (Personal
                                        Information), so any project that requires DOB/Age/
                                        Height/Instagram would otherwise block them with no way
                                        to fill it in. Same fields/state/handlers defined once
                                        above (dobField etc.) — rendered here instead of in
                                        "Your Profile" only for a returning talent, never both. */}
                                    {isReturningTalent && (
                                        <div data-testid="recurring-profile-fields" className="mb-6">
                                            {/* Read-only identity — from the recognized global
                                                talent record; never re-entered here. */}
                                            <div
                                                data-testid="recurring-talent-identity"
                                                className="mb-6 flex items-center gap-3 p-4 rounded-2xl bg-slate-50/70 border border-[#eaeaea]/60"
                                            >
                                                <div className="w-10 h-10 rounded-full bg-slate-200 flex items-center justify-center shrink-0">
                                                    <User className="w-4 h-4 text-[#333333]" />
                                                </div>
                                                <div className="min-w-0">
                                                    <p className="text-[10px] uppercase tracking-[0.2em] font-mono text-[#333333]">Talent</p>
                                                    <p className="text-sm font-semibold text-[#111111] truncate">
                                                        {[form.first_name, form.last_name].filter(Boolean).join(" ")}
                                                    </p>
                                                    <p className="text-xs text-[#666] truncate">{form.email}</p>
                                                </div>
                                            </div>
                                            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-8">
                                                {dobField}
                                                {overrideAgeBlock}
                                                {ageDisplayBlock}
                                                {heightBlock}
                                                {instagramHandleField}
                                                {instagramFollowersBlock}
                                                {workLinksBlock}
                                                {/* P0 fix (2026-09): a returning talent never visits
                                                    the standalone Skills & Attributes step, so a
                                                    mandatory skill category they don't already have
                                                    was previously unreachable — same relocation
                                                    pattern as every other field in this block. */}
                                                {skillsFieldBlock}
                                            </div>
                                        </div>
                                    )}
                                    {/* CURRENT LOCATION — project-specific answer only; never
                                        written back to the talent's global profile location. */}
                                    {!isFieldHidden("location") && (
                                    <div
                                        data-testid="location-question-block"
                                        className="mb-6"
                                    >
                                        <span className="text-[11px] text-[#111111] tracking-[0.08em] font-semibold uppercase font-mono block mb-2">
                                            Current Location(s) {isFieldRequired("location") ? "*" : "(optional)"}
                                        </span>
                                        <LocationSelector
                                            value={form.location || []}
                                            onChange={(arr) => {
                                                setForm({ ...form, location: arr });
                                                if (validationErrors.location) setValidationErrors((e) => ({ ...e, location: undefined }));
                                                setTimeout(saveForm, 0);
                                            }}
                                            testid="form-location"
                                            error={validationErrors.location}
                                        />
                                    </div>
                                    )}

                                    {/* AVAILABILITY — decision block */}
                                    {!isFieldHidden("availability") && (
                                    <div
                                        data-testid="availability-block"
                                        data-step="2"
                                        className="mb-6"
                                    >
                                        <div className="bg-white/70 border border-[#eaeaea]/80 rounded-2xl p-5 mb-4 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                                            <p className="text-[12px] tracking-[0.1em] uppercase font-mono font-semibold text-[#333333] mb-2">
                                                Availability
                                            </p>
                                            {project.shoot_dates ? (
                                                <div className="space-y-2 mt-1">
                                                    {project.shoot_dates.split("\n").map((line, idx) => {
                                                        const trimmed = line.trim();
                                                        if (!trimmed) return null;
                                                        return (
                                                            <div key={idx} className="flex items-start gap-2.5 text-[15px] font-medium text-[#111111]">
                                                                <span className="w-1.5 h-1.5 rounded-full bg-[#0c2340] shrink-0 mt-2.5" />
                                                                <span>{trimmed}</span>
                                                            </div>
                                                        );
                                                    })}
                                                </div>
                                            ) : (
                                                <p className="text-[15px] font-medium text-[#333333]">Dates to be confirmed</p>
                                            )}
                                        </div>
                                        <div className="grid grid-cols-3 gap-2 mb-4">
                                            {AVAILABILITY_OPTIONS.map((opt) => {
                                                const active =
                                                    form.availability.status === opt.key;
                                                return (
                                                    <button
                                                        key={opt.key}
                                                        type="button"
                                                        onClick={() => {
                                                            setForm({
                                                                ...form,
                                                                availability: {
                                                                    ...form.availability,
                                                                    status: opt.key,
                                                                },
                                                            });
                                                            setTimeout(saveForm, 0);
                                                        }}
                                                        data-testid={`avail-${opt.key}-btn`}
                                                        className={`px-2 py-3 rounded-full text-[12px] font-semibold border transition-all duration-200 min-h-[48px] leading-tight ${
                                                            active
                                                                ? "bg-slate-950 text-white border-slate-950 shadow-sm"
                                                                : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                                                        }`}
                                                    >
                                                        {opt.label}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                        {/* "partial" asks WHICH days (a positive detail to
                                            collect); "no" asks WHY/alternate availability (an
                                            explanation) — same field, different placeholder,
                                            no separate calendar UI for either. */}
                                        {(form.availability.status === "partial" || form.availability.status === "no") && (
                                            <textarea
                                                value={form.availability.note}
                                                onChange={(e) =>
                                                    setForm({
                                                        ...form,
                                                        availability: {
                                                            ...form.availability,
                                                            note: e.target.value,
                                                        },
                                                    })
                                                }
                                                onBlur={saveForm}
                                                rows={3}
                                                placeholder={
                                                    form.availability.status === "partial"
                                                        ? "Which days are you available?"
                                                        : "Please specify reason / alternate availability"
                                                }
                                                data-testid="availability-note-input"
                                                className="w-full bg-white/60 rounded-2xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[13px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                            />
                                        )}
                                    </div>
                                    )}

                                    {/* COMMISSION — card */}
                                    {project.commission_percent && (
                                        <div
                                            data-testid="commission-block"
                                            className="mb-6"
                                        >
                                            <div className="bg-white/70 border border-[#eaeaea]/80 rounded-2xl p-5 shadow-[0_1px_2px_rgba(0,0,0,0.02)]" data-testid="commission-card">
                                                <p className="text-[12px] tracking-[0.1em] uppercase font-mono font-semibold text-[#333333] mb-1.5">
                                                    Commission
                                                </p>
                                                <p className="text-[18px] font-semibold text-[#111111] leading-snug">
                                                    {project.commission_percent}
                                                </p>
                                                <p className="text-[11px] text-[#333333] mt-1.5 font-mono leading-relaxed">
                                                    Talentgram's agency fee, deducted from your project payment. The amount you receive is after this commission.
                                                </p>
                                            </div>
                                        </div>
                                    )}

                                    {/* BUDGET — decision block */}
                                    {!isFieldHidden("budget_expectation") && (project.budget_per_day || (project.talent_budget || []).length > 0) && (
                                        <div
                                            data-testid="budget-block"
                                            data-step="2"
                                            className="mb-6"
                                        >
                                            <div className="bg-white/70 border border-[#eaeaea]/80 rounded-2xl p-5 mb-4 shadow-[0_1px_2px_rgba(0,0,0,0.02)]" data-testid="project-budget-card">
                                                <p className="text-[12px] tracking-[0.1em] uppercase font-mono font-semibold text-[#333333] mb-2">
                                                    Project Budget
                                                </p>
                                                {project.budget_per_day && (
                                                    <>
                                                        <p className="text-[18px] font-semibold text-[#111111] leading-snug">
                                                            {project.budget_per_day}
                                                        </p>
                                                        {expandIndianBudgetShorthand(project.budget_per_day) && (
                                                            <p className="text-[12px] text-[#333333] mt-0.5 font-mono" data-testid="budget-per-day-expanded">
                                                                {expandIndianBudgetShorthand(project.budget_per_day)}
                                                            </p>
                                                        )}
                                                    </>
                                                )}
                                                {Array.isArray(project.talent_budget) && project.talent_budget.length > 0 && (
                                                    <div className={`space-y-3 ${project.budget_per_day ? "border-t border-slate-100 pt-4 mt-4" : ""}`}>
                                                        {project.talent_budget.map((row, i) => {
                                                            const expanded = expandIndianBudgetShorthand(row.value);
                                                            return (
                                                            <div
                                                                key={`${row.label || ""}-${i}`}
                                                                className="flex flex-col sm:flex-row sm:items-start justify-between gap-1 sm:gap-4 text-[15px] leading-relaxed text-[#111111] font-medium"
                                                                data-testid={`talent-budget-line-${i}`}
                                                            >
                                                                <span className="text-[#333333] whitespace-pre-wrap">{row.label || "—"}</span>
                                                                <span className="text-right shrink-0">
                                                                    <span className="block text-slate-950 font-semibold">{row.value || "—"}</span>
                                                                    {expanded && (
                                                                        <span className="block text-[12px] text-[#333333] font-mono">{expanded}</span>
                                                                    )}
                                                                </span>
                                                            </div>
                                                            );
                                                        })}
                                                    </div>
                                                )}
                                            </div>

                                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        setForm({
                                                            ...form,
                                                            budget: {
                                                                status: "accept",
                                                                value: "",
                                                            },
                                                        });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                    data-testid="budget-accept-btn"
                                                    className={`px-4 py-3 rounded-full text-[13px] font-semibold border transition-all duration-200 min-h-[48px] ${
                                                        form.budget.status === "accept"
                                                            ? "bg-[#0c2340] text-white border-[#0c2340] shadow-sm"
                                                            : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                                                    }`}
                                                >
                                                    Accept Budget
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        setForm({
                                                            ...form,
                                                            budget: {
                                                                ...form.budget,
                                                                status: "custom",
                                                            },
                                                        });
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                    data-testid="budget-custom-btn"
                                                    className={`px-4 py-3 rounded-full text-[13px] font-semibold border transition-all duration-200 min-h-[48px] ${
                                                        form.budget.status === "custom"
                                                            ? "bg-[#0c2340] text-white border-[#0c2340] shadow-sm"
                                                            : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                                                    }`}
                                                >
                                                    Propose Different Budget
                                                </button>
                                            </div>
                                            {form.budget.status === "custom" && (
                                                <>
                                                    <input
                                                        type="text"
                                                        value={form.budget.value}
                                                        onChange={(e) =>
                                                            setForm({
                                                                ...form,
                                                                budget: {
                                                                    ...form.budget,
                                                                    value: e.target.value,
                                                                },
                                                            })
                                                        }
                                                        onBlur={saveForm}
                                                        placeholder="Enter your expected budget per day"
                                                        data-testid="budget-value-input"
                                                        className="w-full bg-white/60 rounded-2xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[15px] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                    />
                                                    <p className="text-[11px] text-[#333333] mt-1.5 font-mono leading-relaxed">
                                                        Proposing a different budget won't affect your consideration for this project.
                                                    </p>
                                                </>
                                            )}
                                        </div>
                                    )}

                                    {project.competitive_brand_enabled && !isFieldHidden("competitive_brand") && (
                                        <div
                                            data-testid="competitive-brand-block"
                                            data-step="2"
                                            className="mb-6"
                                        >
                                            <span className="text-[11px] text-[#111111] tracking-[0.08em] font-semibold uppercase font-mono block mb-3">
                                                Have you worked with a competitive brand?
                                            </span>
                                            <div className="grid grid-cols-2 gap-3" data-testid="form-competitive-brand">
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        setForm({
                                                            ...form,
                                                            has_competitive_brand_experience: false,
                                                            // Switching to NONE clears any previously
                                                            // entered text so stale data can never be
                                                            // submitted, and doesn't silently reappear if
                                                            // the talent flips back to YES later.
                                                            competitive_brand: "",
                                                        });
                                                        if (validationErrors.competitive_brand) setValidationErrors((e) => ({ ...e, competitive_brand: undefined }));
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                    data-testid="competitive-brand-none-btn"
                                                    className={`px-4 py-3 rounded-full text-[13px] font-semibold border transition-all duration-200 min-h-[48px] ${
                                                        form.has_competitive_brand_experience === false
                                                            ? "bg-[#0c2340] text-white border-[#0c2340] shadow-sm"
                                                            : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                                                    }`}
                                                >
                                                    None
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        setForm({ ...form, has_competitive_brand_experience: true });
                                                        if (validationErrors.competitive_brand) setValidationErrors((e) => ({ ...e, competitive_brand: undefined }));
                                                        setTimeout(saveForm, 0);
                                                    }}
                                                    data-testid="competitive-brand-yes-btn"
                                                    className={`px-4 py-3 rounded-full text-[13px] font-semibold border transition-all duration-200 min-h-[48px] ${
                                                        form.has_competitive_brand_experience === true
                                                            ? "bg-[#0c2340] text-white border-[#0c2340] shadow-sm"
                                                            : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                                                    }`}
                                                >
                                                    Yes
                                                </button>
                                            </div>
                                            {form.has_competitive_brand_experience === true && (
                                                <label className="block mt-4" data-testid="form-competitive-brand-text">
                                                    <span className="text-[11px] text-[#111111] tracking-[0.08em] font-semibold uppercase font-mono">
                                                        Competitive Brands &amp; When
                                                    </span>
                                                    <textarea
                                                        value={form.competitive_brand}
                                                        onChange={(e) => {
                                                            setForm({ ...form, competitive_brand: e.target.value });
                                                            if (validationErrors.competitive_brand_details) setValidationErrors((e2) => ({ ...e2, competitive_brand_details: undefined }));
                                                        }}
                                                        onBlur={saveForm}
                                                        rows={3}
                                                        data-testid="input-competitive-brand-text"
                                                        className="mt-2 w-full bg-white/60 rounded-2xl border border-[#eaeaea] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none py-3 px-4 text-[16px] md:text-[15px] resize-none transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
                                                        placeholder="Mention all competitive brands you have worked with and when. Example: Brand A — June 2025; Brand B — March 2026"
                                                        ref={(el) => { fieldRefs.current.competitive_brand_details = el; }}
                                                    />
                                                </label>
                                            )}
                                        </div>
                                    )}

                                    {project.medium_usage && (
                                        <div className="border-t border-slate-100 pt-8" data-step="2">
                                            <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340]/70 mb-4">Medium / Usage</p>
                                            <p className="text-[13px] leading-relaxed text-[#222222]">
                                                {project.medium_usage}
                                            </p>
                                        </div>
                                    )}

                                    {Array.isArray(project.custom_questions) && project.custom_questions.length > 0 && (
                                        <div className="border-t border-slate-100 pt-8 space-y-6" data-step="2">
                                            <p className="uppercase tracking-[0.2em] text-[10px] font-mono text-[#0c2340]/70">Additional Questions</p>
                                            {project.custom_questions.map((q) => (
                                                <PremiumFormField
                                                    key={q.id}
                                                    label={q.question}
                                                    value={
                                                        (form.custom_answers || {})[q.id] ||
                                                        ""
                                                    }
                                                    onChange={(v) =>
                                                        setForm({
                                                            ...form,
                                                            custom_answers: {
                                                                ...(form.custom_answers ||
                                                                    {}),
                                                                [q.id]: v,
                                                            },
                                                        })
                                                    }
                                                    onBlur={saveForm}
                                                    testid={`form-cq-${q.id}`}
                                                    wide
                                                />
                                            ))}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>

                        {/* Work Links moved to the Skills & Attributes step above (item 2
                            of the wizard redesign) — no longer rendered here.
                            The old "Save Details & Continue to Uploads" desktop submit
                            button that used to live here is also gone: WizardStepNav's
                            Next button on this step now does the same job (explicitly
                            calling startSubmissionDirect()) for both mobile and desktop,
                            so there's exactly one Continue action instead of two. */}
                        </>
                        )}
                    </form>
                    </div>
                </section>
                )}

                {/* SECTION 3 — UPLOADS. Media (intro video / audition takes /
                    portfolio images) has been removed from project submission
                    entirely — portfolio management now lives exclusively in
                    the talent dashboard (see the "Update Portfolio" link on
                    the Thank You screen). This section's JSX/components are
                    deliberately left in place — nothing deleted, existing
                    upload infrastructure is untouched — just never rendered
                    here. `stepVisibilityClass(2)` below is now stale (step 2
                    is Basic Profile after the uploads-step removal from
                    WIZARD_STEPS) — harmless since the whole section is
                    unreachable, kept only so this block still parses if ever
                    re-enabled. */}
                {false && emailGateUnlocked && (
                    <section
                        ref={uploadsSectionRef}
                        className={`pt-4 ${stepVisibilityClass(2)}`}
                        data-testid="uploads-section"
                        data-step="3"
                        data-wizard-step="2"
                    >
                        <div className="bg-white rounded-3xl p-5 sm:p-7 border border-[#eaeaea]/70 shadow-[0_4px_20px_rgba(15,23,42,0.04)]">
                        <div className="flex items-center justify-between mb-4 pb-2 border-b border-[#eaeaea]/30">
                            <div>
                                {/* Step 4 keeps its own Submit footer (unchanged) rather
                                    than WizardStepNav, but the wizard's own "every step
                                    supports Back" rule still applies here — a small
                                    inline Back link, not a full sticky nav bar, since
                                    Step 4 already has its own sticky Submit CTA below. */}
                                <button
                                    type="button"
                                    onClick={handleWizardBack}
                                    data-testid="wizard-back-to-step3-btn"
                                    className="inline-flex items-center gap-1 text-[11px] font-mono text-[#999] hover:text-[#333333] mb-1.5 transition-colors"
                                >
                                    <ArrowLeft className="w-3 h-3" /> Back to Project Info
                                </button>
                                <h2 className="font-display text-2xl font-bold tracking-tight text-slate-950 leading-[1.05] uppercase">
                                    AUDITION UPLOADS
                                </h2>
                            </div>
                            <div className="flex items-center gap-2 shrink-0">
                                <SectionStatusBadge section={experience.sectionStatus.find((s) => s.section === "uploads")} onClick={focusSection} />
                                <button
                                    type="button"
                                    onClick={() =>
                                        setCollapsedSections(prev => ({
                                            ...prev,
                                            uploads: !prev.uploads,
                                        }))
                                    }
                                    className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                    title={collapsedSections.uploads ? "Expand Uploads" : "Collapse Uploads"}
                                >
                                    <ChevronDown
                                        className={`h-4 w-4 transform transition-transform duration-200 ${
                                            collapsedSections.uploads ? "-rotate-90" : ""
                                        }`}
                                    />
                                </button>
                            </div>
                        </div>

                        {!collapsedSections.uploads && (
                            <div className="animate-fadeIn">

                                {/* AUDITION / TEST UPLOAD — the one genuinely new action
                                    for a returning talent, so it renders first, above the
                                    reused-photos/intro summary below. */}
                                {requirements.audition_takes_visibility !== REQUIREMENT_TIERS.HIDDEN && (
                                    <div className="mb-10" data-testid="takes-section">
                                        <div className="flex items-center justify-between mb-4">
                                            <div className="flex items-center gap-2.5">
                                                <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-[#0c2340]/8 text-[#0c2340] shrink-0">
                                                    <Video className="w-4 h-4" />
                                                </span>
                                                <h3 className="font-display text-base sm:text-lg font-bold tracking-tight text-[#0c2340] uppercase">
                                                    Audition Takes{" "}
                                                    <span className="text-[13px] font-mono font-normal normal-case text-[#333333]">
                                                        (up to {MAX_TAKES})
                                                    </span>
                                                </h3>
                                            </div>
                                            <span
                                                className="text-[11px] font-mono text-[#333333]"
                                                data-testid="takes-counter"
                                            >
                                                {takes.length}/{MAX_TAKES}
                                            </span>
                                        </div>
                                        <p className="text-[13px] leading-relaxed text-[#222222] mb-6">
                                            Upload each take as a separate video and label it (e.g., "Scene 1").
                                        </p>

                                        {takes.map((t, i) => (
                                            <PremiumTakeRow
                                                key={t.id}
                                                index={i + 1}
                                                media={t}
                                                canRename={!t._legacy}
                                                onRename={(lbl) =>
                                                    patchTakeLabel(t.id, lbl)
                                                }
                                                onRemove={() => removeMedia(t.id)}
                                                onReplace={(file) => replaceMediaFile(t, file)}
                                                uploadState={activeUploads[`take:${t.label}`]}
                                            />
                                        ))}

                                        {Object.entries(activeUploads)
                                            .filter(([key, state]) => state.category === "take" && !takes.some(t => t.label === state.label))
                                            .map(([key, state]) => (
                                                <div key={key} className="bg-white border border-[#eaeaea] rounded-3xl p-4 flex flex-col gap-3 mb-4 shadow-[0_4px_20px_rgba(15,23,42,0.03)] text-left">
                                                    <div className="flex items-center justify-between">
                                                        <div>
                                                            <span className="text-[11px] font-mono text-[#0c2340]/70 font-semibold uppercase tracking-wider mr-1">New Take:</span>
                                                            <span className="text-sm font-semibold text-[#111111]">{state.label}</span>
                                                        </div>
                                                        <span className="text-[10px] font-mono text-[#333333] inline-flex items-center gap-1.5">
                                                            {state.status !== "failed" && <Loader2 className="w-3 h-3 animate-spin" />}
                                                            {state.status === "failed" ? "Couldn't send" : "✓ Added"}
                                                        </span>
                                                    </div>
                                                    {state.status === "failed" && (
                                                        <div className="text-xs text-rose-600 font-mono mt-1 bg-rose-50/50 p-2.5 rounded-xl border border-rose-100 flex items-center justify-between gap-2">
                                                            <span>Couldn't send this file.</span>
                                                            <button
                                                                type="button"
                                                                onClick={() => retryUpload(key)}
                                                                className="px-3 py-1 bg-white border border-rose-200 text-rose-600 rounded-full hover:bg-rose-50 active:scale-[0.97] transition-all duration-150 text-[10px]"
                                                            >
                                                                Tap to retry
                                                            </button>
                                                        </div>
                                                    )}
                                                </div>
                                            ))
                                        }

                                        {canAddTake && (
                                            <PremiumAddTakeSlot
                                                number={takes.length + 1}
                                                activeUploads={activeUploads}
                                                onPick={(file, label) =>
                                                    triggerUpload(file, "take", label)
                                                }
                                                inputRef={newTakeRef}
                                            />
                                        )}
                                    </div>
                                )}

                                {libraryMedia.length > 0 && !showLibraryPicker && selectedLibrarySourceIds.size > 0 ? (
                                    <div
                                        className="mb-6 flex items-center justify-between gap-3 bg-emerald-50/60 border border-emerald-100 rounded-2xl p-4"
                                        data-testid="library-media-reused-summary"
                                    >
                                        <div className="flex items-center gap-2 text-emerald-700 text-[13px] font-medium">
                                            <Check className="w-4 h-4 shrink-0" />
                                            Using your saved photos &amp; intro ({selectedLibrarySourceIds.size})
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => setShowLibraryPicker(true)}
                                            data-testid="change-saved-media-btn"
                                            className="shrink-0 text-xs font-medium px-3 py-1.5 rounded-full border border-emerald-200 bg-white hover:bg-emerald-50 transition-colors"
                                        >
                                            Change Photos / Intro
                                        </button>
                                    </div>
                                ) : (
                                    <LibraryMediaPicker
                                        categories={LIBRARY_CATEGORIES}
                                        libraryByCategory={libraryByCategory}
                                        removedByCategory={removedByCategory}
                                        dismissedRemovedWarnings={dismissedRemovedWarnings}
                                        selectedLibrarySourceIds={selectedLibrarySourceIds}
                                        libraryBusyId={libraryBusyId}
                                        toggleLibraryMedia={toggleLibraryMedia}
                                        dismissRemovedWarning={dismissRemovedWarning}
                                        removeMedia={removeMedia}
                                        selectAllLibraryMedia={selectAllLibraryMedia}
                                        hasAnyLibraryMedia={libraryMedia.length > 0}
                                    />
                                )}

                                {/* Automatic Media Categorization (item 3) — Admin Mode only.
                                    A generic bulk-add zone, separate from every per-category
                                    zone below (which keep working unchanged for a direct
                                    single-category drop). Dropping a batch here runs the
                                    heuristic suggestion + review modal; per-category zones
                                    skip straight to upload as before. */}
                                {adminMode && (
                                    <div
                                        className={`mb-6 rounded-2xl border-2 border-dashed p-6 text-center transition-colors ${
                                            isBulkDragOver ? "border-[#0c2340]/50 bg-[#0c2340]/5" : "border-slate-300 bg-slate-50/40"
                                        }`}
                                        data-testid="bulk-categorize-dropzone"
                                        onDragOver={(e) => { e.preventDefault(); setIsBulkDragOver(true); }}
                                        onDragLeave={() => setIsBulkDragOver(false)}
                                        onDrop={handleBulkCategorizeDrop}
                                    >
                                        <p className="text-sm font-semibold text-slate-800">
                                            {categorizingBatch ? "Analyzing files…" : "Drop a batch here — we'll suggest categories"}
                                        </p>
                                        <p className="text-xs text-slate-500 mt-1">
                                            Select or drop 2+ images (or a whole folder) and review the suggested categorization before anything uploads.
                                        </p>
                                        <input
                                            ref={bulkCategorizeInputRef}
                                            type="file"
                                            accept="image/*"
                                            multiple
                                            className="hidden"
                                            onChange={(e) => {
                                                if (e.target.files?.length) runBulkCategorization(Array.from(e.target.files));
                                                e.target.value = "";
                                            }}
                                        />
                                        <button
                                            type="button"
                                            onClick={() => bulkCategorizeInputRef.current?.click()}
                                            className="mt-3 text-xs font-medium px-3 py-1.5 rounded-full border border-slate-300 bg-white hover:bg-slate-50"
                                        >
                                            Select Files
                                        </button>
                                    </div>
                                )}

                                {requirements.intro_video !== REQUIREMENT_TIERS.HIDDEN && (
                                    <>
                                    {!intro && (
                                        <MediaDestinationToggle
                                            value={mediaDestination.intro_video}
                                            onChange={(v) => setMediaDestination((d) => ({ ...d, intro_video: v }))}
                                            testidPrefix="intro"
                                        />
                                    )}
                                    <PremiumUploadSlot
                                        title="Introduction Video"
                                        note="Upload your recent professional introduction video (no contact info)."
                                        icon={Video}
                                        accept="video/*"
                                        inputRef={introRef}
                                        onPick={(f) => triggerUpload(f[0], "intro_video")}
                                        uploadState={activeUploads["intro_video"]}
                                        media={intro}
                                        onRemove={(m) => removeMedia(m.id)}
                                        testid="upload-intro"
                                        cameraCapture="user"
                                        failed={Boolean(retryQueue["intro_video"]?.failed)}
                                        onRetry={() => retryUpload("intro_video")}
                                        hint="Recommended duration: 20–60 seconds."
                                    />
                                    </>
                                )}

                                {/* NOTE: this block was relocated to render first — see
                                    the copy right after `animate-fadeIn` opens above — so
                                    the one genuinely new action for a returning talent
                                    (upload a new audition/test) is the first thing they
                                    see, ahead of the reused-photos/intro summary. */}

                                {showImagesSection && (
                                    <div className="mb-8" data-testid="images-upload-section">
                                    <div className="flex items-center justify-between mb-3">
                                        <div className="flex items-center gap-2.5">
                                            <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-[#0c2340]/8 text-[#0c2340] shrink-0">
                                                <Camera className="w-4 h-4" />
                                            </span>
                                            <h3 className="font-display text-base sm:text-lg font-bold tracking-tight text-[#0c2340] uppercase">
                                                Images{" "}
                                                <span className="text-[13px] font-mono font-normal normal-case text-[#333333]">
                                                    (optional)
                                                </span>
                                            </h3>
                                        </div>
                                        <span
                                            data-testid="image-counter"
                                            className="text-[11px] font-mono text-[#333333]"
                                        >
                                            {images.length}/{MAX_IMAGES_PER_CATEGORY}
                                        </span>
                                    </div>
                                    <p className="text-[13px] leading-relaxed text-[#222222] mb-6">
                                        Upload up to {MAX_IMAGES_PER_CATEGORY} images per category. Add your strongest recent professional looks.
                                    </p>

                                    <MediaDestinationToggle
                                        value={mediaDestination.images}
                                        onChange={(v) => setMediaDestination((d) => ({ ...d, images: v }))}
                                        testidPrefix="images"
                                    />

                                    {/* Phase 2 — optional Indian look images */}
                                    {requirements.portfolio_indian_visibility !== REQUIREMENT_TIERS.HIDDEN && (
                                        <PremiumPortfolioGroup
                                            label="Indian Look (optional)"
                                            hint="Saree, lehenga, sherwani, or any traditional/Indian-look references."
                                            items={indianImages}
                                            category="indian"
                                            allImagesCount={indianImages.length}
                                            maxImages={MAX_IMAGES_PER_CATEGORY}
                                            inputRef={indianImagesRef}
                                            uploadImages={uploadImages}
                                            removeMedia={removeMedia}
                                            activeUploads={activeUploads}
                                            onRetry={retryUpload}
                                            testidPrefix="indian"
                                            activePortfolioThumbId={activePortfolioThumbId}
                                            setActivePortfolioThumbId={setActivePortfolioThumbId}
                                            setActiveLightboxImage={setActiveLightboxImage}
                                            replaceMediaFile={replaceMediaFile}
                                            adminMode={adminMode}
                                            setMediaConsentForItem={setMediaConsentForItem}
                                        />
                                    )}

                                    {/* Phase 2 — optional Western look images */}
                                    {requirements.portfolio_western_visibility !== REQUIREMENT_TIERS.HIDDEN && (
                                        <PremiumPortfolioGroup
                                            label="Western Look (optional)"
                                            hint="Casual, formal or western-styled references."
                                            items={westernImages}
                                            category="western"
                                            allImagesCount={westernImages.length}
                                            maxImages={MAX_IMAGES_PER_CATEGORY}
                                            inputRef={westernImagesRef}
                                            uploadImages={uploadImages}
                                            removeMedia={removeMedia}
                                            activeUploads={activeUploads}
                                            onRetry={retryUpload}
                                            testidPrefix="western"
                                            activePortfolioThumbId={activePortfolioThumbId}
                                            setActivePortfolioThumbId={setActivePortfolioThumbId}
                                            setActiveLightboxImage={setActiveLightboxImage}
                                            replaceMediaFile={replaceMediaFile}
                                            adminMode={adminMode}
                                            setMediaConsentForItem={setMediaConsentForItem}
                                        />
                                    )}

                                    {/* Admin Mode — additional look categories. Not part of the
                                        talent-facing form; these exist because an admin uploading
                                        on a talent's behalf may have Selfie/Profiles/Full Length/
                                        Side Profile material to place, same as Indian/Western above. */}
                                    {adminMode && [
                                        { key: "selfie", label: "Selfie", hint: "A clear, recent selfie — no filters.", items: selfieImages, ref: selfieImagesRef },
                                        { key: "profiles", label: "Profiles", hint: "Profile-angle reference shots.", items: profilesImages, ref: profilesImagesRef },
                                        { key: "full_length", label: "Full Length", hint: "Full-body reference shots.", items: fullLengthImages, ref: fullLengthImagesRef },
                                        { key: "side_profile", label: "Side Profile", hint: "Side-angle reference shots.", items: sideProfileImages, ref: sideProfileImagesRef },
                                        { key: "ethnic", label: "Ethnic", hint: "Ethnic-look reference shots.", items: ethnicImages, ref: ethnicImagesRef },
                                        { key: "additional_portfolio", label: "Additional Portfolio", hint: "Extra portfolio material beyond the general set.", items: additionalPortfolioImages, ref: additionalPortfolioImagesRef },
                                    ].map((c) => (
                                        <PremiumPortfolioGroup
                                            key={c.key}
                                            label={`${c.label} (optional)`}
                                            hint={c.hint}
                                            items={c.items}
                                            category={c.key}
                                            allImagesCount={c.items.length}
                                            maxImages={MAX_IMAGES_PER_CATEGORY}
                                            inputRef={c.ref}
                                            uploadImages={uploadImages}
                                            removeMedia={removeMedia}
                                            activeUploads={activeUploads}
                                            onRetry={retryUpload}
                                            testidPrefix={c.key}
                                            activePortfolioThumbId={activePortfolioThumbId}
                                            setActivePortfolioThumbId={setActivePortfolioThumbId}
                                            setActiveLightboxImage={setActiveLightboxImage}
                                            replaceMediaFile={replaceMediaFile}
                                            adminMode={adminMode}
                                            setMediaConsentForItem={setMediaConsentForItem}
                                        />
                                    ))}

                                    {/* Generic Portfolio collapsible group */}
                                    {requirements.portfolio_image_visibility !== REQUIREMENT_TIERS.HIDDEN && (
                                        <div className="mb-6 bg-slate-50/50 border border-[#eaeaea]/60 rounded-2xl p-4" data-testid="portfolio-group-generic">
                                            <div
                                                className="flex items-center justify-between cursor-pointer select-none"
                                                onClick={() => setIsGenericPortfolioCollapsed(!isGenericPortfolioCollapsed)}
                                            >
                                                <div className="flex items-center gap-2">
                                                    <p className="uppercase tracking-[0.08em] text-[11px] font-semibold font-mono text-[#111111]">Portfolio (general)</p>
                                                    <span className="text-[10px] font-mono font-semibold bg-slate-200/80 text-[#222222] px-2 py-0.5 rounded-full">
                                                        {images.length}
                                                    </span>
                                                </div>
                                                <button
                                                    type="button"
                                                    className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                                    title={isGenericPortfolioCollapsed ? "Expand" : "Collapse"}
                                                >
                                                    <ChevronDown
                                                        className={`h-3.5 w-3.5 transform transition-transform duration-200 ${
                                                            isGenericPortfolioCollapsed ? "-rotate-90" : ""
                                                        }`}
                                                    />
                                                </button>
                                            </div>

                                            {!isGenericPortfolioCollapsed && (
                                                <div className="mt-4 animate-fadeIn">
                                                    <div className="grid grid-cols-3 md:grid-cols-4 gap-3 mb-4">
                                                        {images.map((m) => {
                                                            // Actions are hidden by default and revealed:
                                                            //   • Desktop — on hover (group-hover:opacity-100)
                                                            //   • Mobile  — on tap (activePortfolioThumbId === m.id)
                                                            const isActionsVisible = activePortfolioThumbId === m.id;
                                                            return (
                                                                <div
                                                                    key={m.id}
                                                                    className="relative aspect-square bg-slate-100 rounded-2xl overflow-hidden border border-[#eaeaea] group shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.1)] transition-all duration-300 hover:scale-[1.02] cursor-pointer"
                                                                    onClick={(e) => {
                                                                        // Touch devices: first tap reveals the overlay;
                                                                        // the dismiss useEffect clears it when tapping outside.
                                                                        // Desktop: hover already shows the overlay, so click
                                                                        // goes straight to lightbox.
                                                                        const isTouch = window.matchMedia("(hover: none)").matches;
                                                                        if (isTouch && !isActionsVisible) {
                                                                            e.stopPropagation();
                                                                            setActivePortfolioThumbId(m.id);
                                                                            return;
                                                                        }
                                                                        setActivePortfolioThumbId(null);
                                                                        setActiveLightboxImage(m);
                                                                    }}
                                                                >
                                                                    <img
                                                                        src={IMAGE_URL(m)}
                                                                        alt=""
                                                                        className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
                                                                    />
                                                                    {(m.profile_sync_status === "declined" || m.profile_sync_status === "synced") && (
                                                                        <MediaSyncStatusBadge status={m.profile_sync_status} className="absolute top-1.5 left-1.5 text-white bg-black/60 backdrop-blur-sm" />
                                                                    )}
                                                                    {/* Action overlay — hidden by default, revealed on
                                                                        hover (desktop) or tap (mobile). */}
                                                                    <div
                                                                        className={`absolute bottom-0 inset-x-0 h-10 bg-gradient-to-t from-black/70 via-black/45 to-transparent flex items-center justify-end px-2 gap-2 transition-opacity duration-200 ${
                                                                            isActionsVisible
                                                                                ? "opacity-100"
                                                                                : "opacity-0 group-hover:opacity-100"
                                                                        }`}
                                                                    >
                                                                        <button
                                                                            type="button"
                                                                            onClick={(e) => {
                                                                                e.stopPropagation();
                                                                                setActivePortfolioThumbId(null);
                                                                                setActiveLightboxImage(m);
                                                                            }}
                                                                            className="w-7 h-7 bg-white/90 hover:bg-white text-[#111111] rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                                                            title="Zoom"
                                                                        >
                                                                            <Search className="w-3.5 h-3.5" />
                                                                        </button>
                                                                        <button
                                                                            type="button"
                                                                            onClick={(e) => {
                                                                                e.stopPropagation();
                                                                                setActivePortfolioThumbId(null);
                                                                                const inp = document.createElement("input");
                                                                                inp.type = "file";
                                                                                inp.accept = "image/*";
                                                                                inp.onchange = (ev) => {
                                                                                    if (ev.target.files?.length) {
                                                                                        replaceMediaFile(m, ev.target.files[0]);
                                                                                    }
                                                                                };
                                                                                inp.click();
                                                                            }}
                                                                            className="w-7 h-7 bg-white/90 hover:bg-white text-[#111111] rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                                                            title="Replace"
                                                                        >
                                                                            <Upload className="w-3.5 h-3.5" />
                                                                        </button>
                                                                        <button
                                                                            type="button"
                                                                            onClick={(e) => {
                                                                                e.stopPropagation();
                                                                                setActivePortfolioThumbId(null);
                                                                                removeMedia(m.id);
                                                                            }}
                                                                            className="w-7 h-7 bg-white/90 hover:bg-rose-50 text-rose-600 rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                                                            title="Delete"
                                                                        >
                                                                            <Trash2 className="w-3.5 h-3.5" />
                                                                        </button>
                                                                    </div>
                                                                </div>
                                                            );
                                                        })}
                                                        {Object.entries(activeUploads)
                                                            .filter(([key, state]) => state.category === "image")
                                                            .map(([key, state]) => (
                                                                <div key={key} className="relative aspect-square bg-slate-50 border border-[#eaeaea] rounded-2xl flex flex-col items-center justify-center p-2 shadow-sm text-center">
                                                                    {state.status === "failed" ? (
                                                                        <>
                                                                            <span className="text-[9px] font-mono text-[#333333] truncate w-full px-1">{state.fileName}</span>
                                                                            <span className="text-[10px] font-mono font-semibold text-rose-600 mt-1">Couldn't send</span>
                                                                            <button
                                                                                type="button"
                                                                                onClick={() => retryUpload(key)}
                                                                                className="mt-1 px-2.5 py-0.5 border border-rose-200 text-rose-600 rounded-full hover:bg-rose-50 text-[9px] font-semibold"
                                                                            >
                                                                                Tap to retry
                                                                            </button>
                                                                        </>
                                                                    ) : (
                                                                        <>
                                                                            <Loader2 className="w-5 h-5 animate-spin text-[#0c2340] mb-1" />
                                                                            <span className="text-[10px] font-mono font-semibold text-[#111111] mt-1">✓ Added</span>
                                                                        </>
                                                                    )}
                                                                </div>
                                                            ))
                                                        }
                                                        {images.length < MAX_IMAGES_PER_CATEGORY && (
                                                            <button
                                                                type="button"
                                                                onClick={() =>
                                                                    imagesRef.current?.click()
                                                                }
                                                                data-testid="add-image-btn"
                                                                className="relative aspect-square rounded-2xl border border-dashed border-[#d4d4d4] hover:border-[#0c2340]/30 hover:bg-[#0c2340]/5 flex items-center justify-center text-[#333333] hover:text-[#0c2340] transition-all duration-200 overflow-hidden bg-gradient-to-b from-white to-slate-50/70 shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px]"
                                                            >
                                                                <div className="relative flex flex-col items-center gap-1">
                                                                    <Camera className="w-5 h-5" />
                                                                    <span className="text-[10px] font-mono">
                                                                        Add
                                                                    </span>
                                                                </div>
                                                            </button>
                                                        )}
                                                    </div>
                                                    <input
                                                        ref={imagesRef}
                                                        type="file"
                                                        accept="image/*"
                                                        multiple
                                                        className="hidden"
                                                        onChange={(e) => {
                                                            if (e.target.files?.length)
                                                                uploadImages(e.target.files);
                                                            e.target.value = "";
                                                        }}
                                                    />
                                                    {/* Mobile-only camera-first action */}
                                                    <input
                                                        ref={cameraImagesRef}
                                                        type="file"
                                                        accept="image/*"
                                                        capture="environment"
                                                        className="hidden"
                                                        onChange={(e) => {
                                                            if (e.target.files?.length)
                                                                uploadImages(e.target.files);
                                                            e.target.value = "";
                                                        }}
                                                    />
                                                    <div className="md:hidden grid grid-cols-2 gap-2 mt-3">
                                                        <button
                                                            type="button"
                                                            onClick={() => cameraImagesRef.current?.click()}
                                                            disabled={Object.values(activeUploads).some((u) => u.category === "image" && u.status === "uploading") || images.length >= MAX_IMAGES_PER_CATEGORY}
                                                            data-testid="add-image-camera-btn"
                                                            className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                                                        >
                                                            <Camera className="w-3.5 h-3.5" /> Take photo
                                                        </button>
                                                        <button
                                                            type="button"
                                                            onClick={() => imagesRef.current?.click()}
                                                            disabled={Object.values(activeUploads).some((u) => u.category === "image" && u.status === "uploading") || images.length >= MAX_IMAGES_PER_CATEGORY}
                                                            data-testid="add-image-library-btn"
                                                            className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                                                        >
                                                            <FolderOpen className="w-3.5 h-3.5" /> From library
                                                        </button>
                                                    </div>
                                                </div>
                                            )}
                                        </div>
                                    )}
                                </div>
                                )}
                            </div>
                        )}

                        </div>
                    </section>
                )}

                {/* Wizard Back/Next — visible for every step BEFORE the true
                    final step of THIS talent's flow; anchored here, AFTER
                    every step-content section (profile/skills/project-
                    questions/uploads all being CSS-hidden-not-unmounted, see
                    stepVisibilityClass), so it always renders at the bottom
                    of whichever one is actually visible rather than in a
                    single fixed DOM slot that only happened to be "after"
                    Project Questions and "before" Uploads. The final step
                    keeps its own existing Submit footer further down instead
                    (see isOnFinalDisplayedStep's declaration/comment above) —
                    gating this on the fixed literal `currentStep < 4` doesn't
                    hold for a returning talent, whose last displayed step is
                    2 (Uploads), not 4: at step 2 that comparison is still
                    true, so this nav rendered ALONGSIDE the finalize footer's
                    own "Next" (continue-to-final-step-btn), a second
                    competing control on the same screen. */}
                {emailGateUnlocked && !isOnFinalDisplayedStep && (
                    <WizardStepNav
                        showBack={currentStep > 1}
                        onBack={handleWizardBack}
                        onNext={handleWizardNext}
                        nextDisabled={starting}
                        nextBusy={sectionForStep(currentStep) === "projectQuestions" && starting}
                        nextLabel="Next"
                    />
                )}

                {/* READY-TO-SUBMIT FOOTER — deliberately OUTSIDE every
                    step's stepVisibilityClass-gated wrapper (unlike Phase
                    1, where this lived inside the uploads section, back
                    when uploads was always the talent's last step). Gating
                    on step position alone would hide it exactly when a new
                    talent needs it (after Skills, not after Uploads) or a
                    returning talent needs it (after Uploads, no Profile/
                    Skills at all) — `experience.readinessSummary.ready`
                    is what actually decides WHETHER it CAN show. But
                    manual-testing found `ready` alone isn't enough: it can
                    flip true while the talent is still visually on an
                    earlier page (e.g. the moment the last required upload
                    finishes, while Media is still on screen), stacking the
                    submit CTA directly underneath a page the user hasn't
                    finished looking at yet. `finalStepReached` (see its
                    declaration above) is the second, independent gate that
                    fixes this — it only flips true from an explicit
                    "Continue" click (rendered in this same spot while
                    false), so the talent always sees one deliberate action
                    ("Continue") on whatever their actual last page is,
                    THEN a real final page with the submit CTA — never both
                    at once. IDENTITY CONFIRMATION (recognizedIdentity —
                    the token-backed silent-recognition path) and ALMOST
                    DONE (emailVerified — the new-talent end-of-flow auth
                    gate) are mutually exclusive alternatives rendered on
                    that final page; the plain finalize-submission-btn
                    footer is the fallback when neither applies (project
                    questions/skills already completed OTP earlier, e.g.
                    the email-typed "Is this you?" path).

                    Deliberately NOT gated on `experience.readinessSummary.ready`
                    (WhatsApp/Drive upload architecture, 2026-08): `ready` is
                    false for the entire time a required upload is still
                    in-flight — that's precisely the case `blockingReason`
                    WAITING/FAILED/MISSING below exists to explain. Gating the
                    whole footer on `ready` made that messaging unreachable: a
                    talent who reached this, their final step, while a
                    required file was still uploading saw no Continue button,
                    no Submit button, and no explanation — a dead end. The
                    inner branches already fully cover every state (ready,
                    waiting, failed, missing); this block only needs to know
                    the talent has arrived at their real last page. */}
                {emailGateUnlocked && isOnFinalDisplayedStep && submission?.status !== "submitted" && (
                    (finalStepReached || adminMode) ? (
                    <div className="pt-4">
                        {recognizedIdentity && (
                            <div
                                className="mb-6 flex flex-col gap-5 border border-slate-100 rounded-2xl p-5 bg-slate-50/50"
                                data-testid="identity-confirmation-card"
                            >
                                <div className="flex items-center gap-4">
                                    <div className="w-14 h-14 rounded-full bg-slate-200 flex items-center justify-center border border-[#d4d4d4] shrink-0">
                                        <User className="w-6 h-6 text-[#333333]" />
                                    </div>
                                    <div className="text-left">
                                        <h4 className="font-semibold text-sm text-[#111111]">Is this you?</h4>
                                        <p className="text-xs text-[#333333] font-medium mt-1">
                                            {recognizedIdentity.name}
                                            {(() => {
                                                const locs = Array.isArray(recognizedIdentity.location)
                                                    ? recognizedIdentity.location
                                                    : (recognizedIdentity.location ? [{ city: recognizedIdentity.location }] : []);
                                                const bits = [];
                                                if (locs.length > 0) bits.push(locs.map((l) => l?.city || l).join(", "));
                                                if (recognizedIdentity.height) bits.push(recognizedIdentity.height);
                                                return bits.length > 0 ? ` · ${bits.join(" · ")}` : "";
                                            })()}
                                        </p>
                                    </div>
                                </div>
                                <div className="flex flex-col items-stretch gap-2">
                                    <button
                                        type="button"
                                        onClick={handleSubmitCtaClick}
                                        disabled={experience.submitCta.disabled || finalizing}
                                        data-testid="identity-confirm-submit-btn"
                                        className="w-full bg-slate-900 text-white px-4 py-3 rounded-full text-[13px] font-semibold hover:bg-slate-800 active:scale-[0.98] disabled:opacity-40 transition-all duration-150 inline-flex items-center justify-center gap-2 min-h-[48px]"
                                    >
                                        {finalizing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                                        Yes, submit
                                    </button>
                                    <button
                                        type="button"
                                        onClick={handleUseAnotherEmail}
                                        data-testid="identity-not-you-btn"
                                        className="w-full text-[#333333] text-xs font-medium py-1.5 hover:text-[#111111] transition-colors"
                                    >
                                        Not you? Sign in
                                    </button>
                                </div>
                            </div>
                        )}

                        {!recognizedIdentity && !emailVerified && !adminMode && (
                            <AlmostDoneAuthCard
                                email={form.email}
                                gatewayLoading={gatewayLoading}
                                otpSent={otpSent}
                                otpValue={otpValue}
                                setOtpValue={setOtpValue}
                                otpLoading={otpLoading}
                                otpResending={otpResending}
                                onSendCode={handleAlmostDoneSendCode}
                                onVerify={handleAlmostDoneVerify}
                                onResend={handleResendOtp}
                                onGoogle={handleGoogleLogin}
                            />
                        )}

                        {/* Excludes recognizedIdentity: that card above already
                            renders its own complete, dedicated submit action
                            ("Yes, submit") — showing this generic footer too
                            would put two competing submit buttons on screen
                            at once, exactly the "verification screen must not
                            introduce a competing submit action" rule this
                            page follows everywhere else. */}
                        {(!recognizedIdentity && (emailVerified || adminMode)) && (
                        <div ref={stickyFooterRef} data-sticky-footer className="sticky bottom-0 z-30 bg-gradient-to-t from-white via-white/95 to-transparent pt-4 pb-[calc(1.5rem+env(safe-area-inset-bottom))] pb-safe">
                            <p className="text-[12px] text-[#333333] text-center mb-3 max-w-md mx-auto leading-relaxed" data-testid="submission-accuracy-warning">
                                Please ensure your details, portfolio and videos are accurate and up to date. Casting decisions are based on the information submitted here.
                            </p>
                            {/* Pure renderer of `experience.submitCta` (Submission Experience
                                Model) — this button makes no readiness decisions of its own.
                                Label, disabled state, and click behavior all come from the model. */}
                            <button
                                onClick={handleSubmitCtaClick}
                                disabled={experience.submitCta.disabled}
                                data-testid="finalize-submission-btn"
                                data-cta-action={experience.submitCta.buttonAction}
                                className="w-full bg-slate-900 text-white py-4 rounded-full text-[13px] font-medium hover:bg-slate-800 active:scale-[0.97] disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center justify-center gap-2 min-h-[52px] transition-all duration-200"
                                style={{ WebkitTapHighlightColor: "transparent" }}
                            >
                                {finalizing ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <Sparkles className="w-4 h-4" />
                                )
                                }
                                {experience.submitCta.buttonLabel}
                            </button>
                            {/* Live, mutually-exclusive submit-blocked reasons — Missing, Waiting
                                for uploads, and Upload failed are three different situations for
                                the talent and never render at once. Driven entirely by
                                `experience.blockingReason` (Submission Experience Model) — no
                                special-case scanning of activeUploads happens here. */}
                            {experience.blockingReason === SUBMIT_BLOCKING_REASONS.FAILED ? (
                                experience.readinessSummary.failed.length === 1 ? (
                                    <button
                                        type="button"
                                        onClick={() => focusRequirementItem(experience.readinessSummary.failed[0])}
                                        className="w-full text-[11px] text-rose-700 text-center mt-3 font-mono underline decoration-rose-300 underline-offset-2"
                                        data-testid="upload-failed-msg"
                                        aria-live="assertive"
                                    >
                                        Your {experience.readinessSummary.failed[0].label} failed to upload. Please retry before submitting.
                                    </button>
                                ) : (
                                    <p className="text-[11px] text-rose-700 text-center mt-3 font-mono" data-testid="upload-failed-msg" aria-live="assertive">
                                        {experience.readinessSummary.failed.length} uploads need attention.
                                    </p>
                                )
                            ) : experience.blockingReason === SUBMIT_BLOCKING_REASONS.WAITING ? (
                                <p className="text-[11px] text-[#333333] text-center mt-3 font-mono" data-testid="uploads-in-progress-msg" aria-live="polite">
                                    Waiting for uploads… hang tight, this updates automatically.
                                </p>
                            ) : experience.blockingReason === SUBMIT_BLOCKING_REASONS.MISSING && (
                                <p className="text-[11px] text-[#333333] text-center mt-3 font-mono" data-testid="missing-requirements-msg" aria-live="polite">
                                    Need: First+Last name · Height · Location ·
                                    Availability · Budget
                                </p>
                            )}
                        </div>
                        )}
                    </div>
                    ) : (
                    <div className="pt-4" data-testid="continue-to-final-step">
                        <button
                            type="button"
                            onClick={() => {
                                setFinalStepReached(true);
                                if (!adminMode) writeFinalStepReached(slug);
                            }}
                            data-testid="continue-to-final-step-btn"
                            className="w-full bg-slate-900 text-white py-4 rounded-full text-[13px] font-medium hover:bg-slate-800 active:scale-[0.97] inline-flex items-center justify-center gap-2 min-h-[52px] transition-all duration-200"
                        >
                            Next
                            <ArrowRight className="w-3.5 h-3.5" />
                        </button>
                    </div>
                    )
                )}
            </div>

            {showMaterial && (
                <MaterialModal
                    project={project}
                    onClose={() => setShowMaterial(false)}
                />
            )}

            {activeLightboxImage && (
                <div
                    className="fixed inset-0 z-50 bg-black/90 backdrop-blur-sm flex items-center justify-center p-4 transition-all duration-200 animate-in fade-in"
                    onClick={() => setActiveLightboxImage(null)}
                >
                    <button
                        type="button"
                        onClick={() => setActiveLightboxImage(null)}
                        className="absolute top-4 right-4 p-2 bg-white/10 hover:bg-white/20 text-white rounded-full transition-colors min-w-[44px] min-h-[44px] flex items-center justify-center"
                    >
                        <X className="w-5 h-5" />
                    </button>
                    <img
                        src={IMAGE_URL(activeLightboxImage)}
                        alt=""
                        className="max-w-full max-h-[85vh] object-contain rounded-lg shadow-2xl animate-in zoom-in-95 duration-200"
                    />
                </div>
            )}

            {/* Sticky mobile readiness bar removed (2026-08 simplified-wizard
                UX): `WizardProgressBar` (top, sticky) is now the single
                persistent status element on every viewport — a second
                sticky bar at the bottom duplicated that role. Per-field
                validation still surfaces inline on its own step when NEXT
                is blocked. */}

            {categorizationBatch && (
                <CategorizationReviewModal
                    groups={categorizationBatch.groups}
                    uncategorized={categorizationBatch.uncategorized}
                    onConfirm={handleCategorizationConfirm}
                    onCancel={() => setCategorizationBatch(null)}
                />
            )}

            {/* Talent Profile Migration, Phase 4 — reusable-media consent.
                No backdrop-dismiss / no close button: the talent must make an
                explicit choice (default is pre-selected to "only this
                project", so simply confirming never auto-updates anything).
                They can still navigate away and resume later — resuming
                re-shows this exact dialog (pendingMediaConsent comes fresh
                from the server on every load), it is never lost.
                UX-polish fix — gated on `pendingConsentAwaitingChoice`, NOT
                the raw `pendingMediaConsent`: a normal upload always has a
                pre-chosen destination by this point (see `mediaDestination`
                and the auto-resolve effect above), so this dialog now only
                has something to ask about for the genuine edge cases where
                no destination is known (a draft resumed from before this
                feature existed, or similar) — it can no longer flash into
                view mid-upload racing against that effect's own resolve
                call. */}
            {pendingConsentAwaitingChoice.length > 0 && !adminMode && (
                <div
                    className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
                    data-testid="media-consent-dialog"
                >
                    <div className="bg-white rounded-2xl shadow-2xl max-w-md w-full p-6">
                        <h3 className="font-display text-xl font-bold text-slate-950 mb-2">
                            You uploaded {pendingConsentSummary.join(", ")}
                        </h3>
                        <p className="text-sm text-slate-600 mb-5">
                            How would you like to use {pendingMediaConsent.length === 1 ? "it" : "them"}?
                        </p>

                        <label className="flex items-start gap-3 p-3 rounded-xl border border-slate-200 hover:bg-slate-50 cursor-pointer mb-2">
                            <input
                                type="radio"
                                name="media-consent-choice"
                                checked={mediaConsentChoice === "only_this_project"}
                                onChange={() => setMediaConsentChoice("only_this_project")}
                                className="mt-1"
                                data-testid="media-consent-only-project"
                            />
                            <span className="text-sm text-slate-800 font-medium">Use only for this project</span>
                        </label>

                        <label className="flex items-start gap-3 p-3 rounded-xl border border-slate-200 hover:bg-slate-50 cursor-pointer mb-5">
                            <input
                                type="radio"
                                name="media-consent-choice"
                                checked={mediaConsentChoice === "update_profile"}
                                onChange={() => setMediaConsentChoice("update_profile")}
                                className="mt-1"
                                data-testid="media-consent-update-profile"
                            />
                            <span className="text-sm text-slate-800">
                                <span className="font-medium">Update my Talent Profile</span>
                                <br />
                                <span className="text-xs text-slate-500">
                                    Updating your Talent Profile will also update your Dashboard and future project prefills.
                                </span>
                            </span>
                        </label>

                        <button
                            type="button"
                            disabled={mediaConsentSubmitting}
                            onClick={() => submitMediaConsent(mediaConsentChoice)}
                            className="w-full py-3 rounded-full bg-slate-950 text-white font-medium text-sm hover:bg-slate-800 transition-colors disabled:opacity-50"
                            data-testid="media-consent-confirm"
                        >
                            {mediaConsentSubmitting ? "Saving…" : "Confirm"}
                        </button>
                    </div>
                </div>
            )}
        </main>
    );
}

// Pure renderer of one `experience.sectionStatus` entry (see
// lib/readinessStatus.js's summarizeSections) — a per-section rollup badge
// for the three collapsible section headers (profile / projectQuestions /
// uploads). Makes no decisions of its own; SECTION_STATUS is already fully
// resolved by the Readiness Engine before it reaches here.
const SECTION_STATUS_BADGE_META = {
    [SECTION_STATUS.COMPLETE]: { text: () => "Complete", className: "bg-emerald-50 text-emerald-700 border-emerald-200" },
    [SECTION_STATUS.IN_PROGRESS]: { text: (s) => `${s.requiredCompleted}/${s.requiredTotal}`, className: "bg-[#0c2340]/5 text-[#0c2340] border-[#0c2340]/15" },
    [SECTION_STATUS.ATTENTION]: { text: () => "Needs Attention", className: "bg-rose-50 text-rose-700 border-rose-200" },
    [SECTION_STATUS.INCOMPLETE]: { text: (s) => `${s.requiredCompleted}/${s.requiredTotal}`, className: "bg-slate-50 text-[#666666] border-slate-200" },
    [SECTION_STATUS.OPTIONAL]: { text: () => "Optional", className: "bg-slate-50 text-[#999999] border-slate-200" },
};

function SectionStatusBadge({ section, onClick }) {
    if (!section) return null;
    const meta = SECTION_STATUS_BADGE_META[section.status] || SECTION_STATUS_BADGE_META[SECTION_STATUS.INCOMPLETE];
    return (
        <button
            type="button"
            onClick={() => onClick?.(section)}
            data-testid={`section-status-${section.section}`}
            data-status={section.status}
            className={`shrink-0 inline-flex items-center px-2.5 py-1 rounded-full border text-[10px] font-mono font-semibold uppercase tracking-wide transition-all duration-150 hover:brightness-95 active:scale-[0.97] ${meta.className}`}
        >
            {meta.text(section)}
        </button>
    );
}

function Info({ label, value, wide }) {
    if (!value) return null;
    return (
        <div className={wide ? "col-span-1 sm:col-span-2" : ""}>
            <div className="text-[10px] tracking-[0.2em] uppercase font-mono text-[#333333] mb-1">
                {label}
            </div>
            <div className="text-[13px] font-medium text-[#111111]">{value}</div>
        </div>
    );
}

// P1 fix: this badge used to be gated on a media item's `origin` field
// ("project" = freshly uploaded here vs. "global" = pulled from the
// Library) — a "where did this come from" flag set once at upload time
// and never touched again. That's a different question from "where does
// this live now", which is exactly what a talent reads this badge as
// meaning. Because `origin` never changes, choosing "Update my Talent
// Profile" in the consent dialog left the badge permanently reading
// "Only in this project" even after the item was successfully synced —
// the display never reflected the actual persisted consent decision.
// Driven by `profile_sync_status` instead (the one field
// apply_media_consent_decision() — routers/submissions.py — actually
// writes "declined"/"synced" to), so the badge always matches what was
// really saved: on upload, on resume/refresh (build_talent_submission_view
// recomputes the same media array either way), and immediately after the
// talent answers the dialog (submitMediaConsent's response replaces
// `submission.media` via applySubmissionResponse — no separate refetch
// needed). Renders nothing while `profile_sync_status` is still "pending"
// (the dialog hasn't been answered yet) or absent (from-library items,
// non-reusable categories, or submissions predating this consent system).
function MediaSyncStatusBadge({ status, className = "" }) {
    const isSynced = status === "synced";
    return (
        <span
            data-testid={isSynced ? "media-sync-badge-synced" : "media-sync-badge-declined"}
            className={`inline-flex items-center text-[9px] font-mono font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-full whitespace-nowrap ${className}`}
        >
            {isSynced ? "Saved to dashboard" : "Only in this project"}
        </span>
    );
}

// UX-polish fix — the "what's this upload for?" question now has to be
// asked BEFORE the talent picks a file, not after the upload completes
// (see `mediaDestination` and its auto-resolve effect in SubmissionPage).
// Rendered above the Introduction Video slot and above the Images block;
// never shown for Audition Takes, which are never reusable outside a
// project by design. "My Media Library" renders first — it's the default
// destination, and the visual order should match that.
function MediaDestinationToggle({ value, onChange, testidPrefix }) {
    return (
        <div className="flex flex-wrap items-center gap-2.5 mb-4" data-testid={`${testidPrefix}-destination-toggle`}>
            <span className="text-[11px] font-mono text-[#333333] uppercase tracking-wider shrink-0">
                What&apos;s this for?
            </span>
            <div className="flex gap-2">
                <button
                    type="button"
                    onClick={() => onChange("library")}
                    data-testid={`${testidPrefix}-destination-library-btn`}
                    className={`px-3 py-1.5 rounded-full text-[12px] font-semibold border transition-all duration-200 ${
                        value === "library"
                            ? "bg-[#0c2340] text-white border-[#0c2340]"
                            : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                    }`}
                >
                    My Media Library
                </button>
                <button
                    type="button"
                    onClick={() => onChange("project")}
                    data-testid={`${testidPrefix}-destination-project-btn`}
                    className={`px-3 py-1.5 rounded-full text-[12px] font-semibold border transition-all duration-200 ${
                        value === "project"
                            ? "bg-[#0c2340] text-white border-[#0c2340]"
                            : "bg-white border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111]"
                    }`}
                >
                    This Project
                </button>
            </div>
        </div>
    );
}

function PremiumPortfolioGroup({
    label,
    hint,
    items,
    category,
    allImagesCount,
    maxImages,
    inputRef,
    uploadImages,
    removeMedia,
    activeUploads = {},
    onRetry,
    testidPrefix,
    activePortfolioThumbId,
    setActivePortfolioThumbId,
    setActiveLightboxImage,
    replaceMediaFile,
    adminMode,
    setMediaConsentForItem,
}) {
    const reachedCap = allImagesCount >= maxImages;
    const [isCollapsed, setIsCollapsed] = useState(() => {
        return typeof window !== "undefined" && window.innerWidth < 768;
    });
    // Drag & drop — drop a batch of images (e.g. 20-100 at once) straight
    // into this category's zone; auto-placed via the same `uploadImages`
    // handler the click-to-add input already uses (unique-slotKey-per-file
    // fix included), so drag-drop and click-to-add share one upload path.
    const [isDragOver, setIsDragOver] = useState(false);
    const handleDrop = async (e) => {
        e.preventDefault();
        setIsDragOver(false);
        if (reachedCap) return;
        // Folder drop (item 2/10) — walks any dropped directories via the
        // File System Entries API and flattens to a plain file list; a
        // non-folder drop resolves immediately to the same list `e.dataTransfer.files`
        // already gave us. Same uploadImages() call either way.
        const dt = e.dataTransfer;
        const files = await collectDroppedFiles(dt);
        if (files?.length) uploadImages(files, category);
    };
    return (
        <div
            className={`mb-6 bg-slate-50/50 border rounded-2xl p-4 transition-colors ${isDragOver ? "border-[#0c2340]/40 bg-[#0c2340]/5" : "border-[#eaeaea]/60"}`}
            data-testid={`portfolio-group-${testidPrefix}`}
            onDragOver={(e) => { e.preventDefault(); if (!reachedCap) setIsDragOver(true); }}
            onDragLeave={() => setIsDragOver(false)}
            onDrop={handleDrop}
        >
            <div
                className="flex items-center justify-between cursor-pointer select-none"
                onClick={() => setIsCollapsed(!isCollapsed)}
            >
                <div className="flex items-center gap-2">
                    <p className="uppercase tracking-[0.08em] text-[11px] font-semibold font-mono text-[#111111]">{label}</p>
                    <span className="text-[10px] font-mono font-semibold bg-slate-200/80 text-[#222222] px-2 py-0.5 rounded-full">
                        {items.length}
                    </span>
                </div>
                <button
                    type="button"
                    className="p-1 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                    title={isCollapsed ? "Expand" : "Collapse"}
                >
                    <ChevronDown
                        className={`h-3.5 w-3.5 transform transition-transform duration-200 ${
                            isCollapsed ? "-rotate-90" : ""
                        }`}
                    />
                </button>
            </div>
            {!isCollapsed && (
                <div className="mt-4 animate-fadeIn">
                    {hint && (
                        <p className="text-[12px] text-[#222222] mb-4 leading-relaxed">
                            {hint}
                        </p>
                    )}
                    <div className="grid grid-cols-3 md:grid-cols-4 gap-3">
                        {items.map((m) => {
                            const isActionsVisible = activePortfolioThumbId === m.id;
                            return (
                                <div
                                    key={m.id}
                                    className="relative aspect-square bg-slate-100 rounded-2xl overflow-hidden border border-[#eaeaea] group shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.1)] transition-all duration-300 hover:scale-[1.02] cursor-pointer"
                                    data-testid={`${testidPrefix}-image-${m.id}`}
                                    onClick={(e) => {
                                        const isTouch = window.matchMedia("(hover: none)").matches;
                                        if (isTouch && !isActionsVisible) {
                                            e.stopPropagation();
                                            setActivePortfolioThumbId(m.id);
                                            return;
                                        }
                                        setActivePortfolioThumbId(null);
                                        setActiveLightboxImage(m);
                                    }}
                                >
                                    <img
                                        src={IMAGE_URL(m)}
                                        alt=""
                                        className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
                                    />
                                    {(m.profile_sync_status === "declined" || m.profile_sync_status === "synced") && (
                                        <MediaSyncStatusBadge status={m.profile_sync_status} className="absolute top-1.5 left-1.5 text-white bg-black/60 backdrop-blur-sm" />
                                    )}
                                    {/* Project-Specific Media Override (item 5) — only shown for a
                                        just-uploaded item still awaiting a consent decision. Checking
                                        it promotes THIS item to the Talent Profile; leaving it
                                        unchecked keeps it project-only (the finalize-time safety net
                                        resolves any still-pending items to project-only). */}
                                    {adminMode && m.profile_sync_status === "pending" && (
                                        <label
                                            className="absolute top-1.5 right-1.5 flex items-center gap-1 bg-white/90 backdrop-blur-sm rounded-full pl-1.5 pr-2 py-1 text-[9px] font-medium text-slate-700 shadow-sm cursor-pointer"
                                            onClick={(e) => e.stopPropagation()}
                                            title="Save this item to the talent's Master Profile"
                                        >
                                            <input
                                                type="checkbox"
                                                className="w-3 h-3"
                                                data-testid={`${testidPrefix}-save-to-master-${m.id}`}
                                                onChange={(e) => setMediaConsentForItem(m.id, e.target.checked)}
                                            />
                                            Master
                                        </label>
                                    )}
                                    <div
                                        className={`absolute bottom-0 inset-x-0 h-10 bg-gradient-to-t from-black/70 via-black/45 to-transparent flex items-center justify-end px-2 gap-2 transition-opacity duration-200 ${
                                            isActionsVisible
                                                ? "opacity-100"
                                                : "opacity-0 group-hover:opacity-100"
                                        }`}
                                    >
                                        <button
                                            type="button"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                setActivePortfolioThumbId(null);
                                                setActiveLightboxImage(m);
                                            }}
                                            className="w-7 h-7 bg-white/90 hover:bg-white text-[#111111] rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                            title="Zoom"
                                        >
                                            <Search className="w-3.5 h-3.5" />
                                        </button>
                                        <button
                                            type="button"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                setActivePortfolioThumbId(null);
                                                const inp = document.createElement("input");
                                                inp.type = "file";
                                                inp.accept = "image/*";
                                                inp.onchange = (ev) => {
                                                    if (ev.target.files?.length) {
                                                        replaceMediaFile(m, ev.target.files[0]);
                                                    }
                                                };
                                                inp.click();
                                            }}
                                            className="w-7 h-7 bg-white/90 hover:bg-white text-[#111111] rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                            title="Replace"
                                        >
                                            <Upload className="w-3.5 h-3.5" />
                                        </button>
                                        <button
                                            type="button"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                setActivePortfolioThumbId(null);
                                                removeMedia(m.id);
                                            }}
                                            data-testid={`${testidPrefix}-image-remove-${m.id}`}
                                            className="w-7 h-7 bg-white/90 hover:bg-rose-50 text-rose-600 rounded-full shadow-sm flex items-center justify-center transition-all active:scale-[0.9]"
                                            title="Delete"
                                        >
                                            <Trash2 className="w-3.5 h-3.5" />
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                        {Object.entries(activeUploads)
                            .filter(([key, state]) => state.category === category)
                            .map(([key, state]) => (
                                <div key={key} className="relative aspect-square bg-slate-50 border border-[#eaeaea] rounded-2xl flex flex-col items-center justify-center p-2 shadow-sm text-center">
                                    {state.status === "failed" ? (
                                        <>
                                            <span className="text-[10px] font-mono text-[#333333] truncate w-full px-1 mb-1">Couldn't send this file.</span>
                                            <button
                                                type="button"
                                                onClick={() => onRetry && onRetry(key)}
                                                className="mt-1 px-2.5 py-0.5 border border-rose-200 text-rose-600 rounded-full hover:bg-rose-50 text-[9px] font-semibold"
                                            >
                                                Tap to retry
                                            </button>
                                        </>
                                    ) : (
                                        <>
                                            <Loader2 className="w-5 h-5 animate-spin text-[#0c2340] mb-1" />
                                            <span className="text-[10px] font-mono font-semibold text-[#111111] mt-1">✓ Added</span>
                                        </>
                                    )}
                                </div>
                            ))
                        }
                        {!reachedCap && (
                            <button
                                type="button"
                                onClick={() => inputRef.current?.click()}
                                data-testid={`add-${testidPrefix}-image-btn`}
                                className="relative aspect-square rounded-2xl border border-dashed border-[#d4d4d4] hover:border-[#0c2340]/30 hover:bg-[#0c2340]/5 flex items-center justify-center text-[#333333] hover:text-[#0c2340] transition-all duration-200 overflow-hidden bg-gradient-to-b from-white to-slate-50/70 shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px]"
                            >
                                <div className="relative flex flex-col items-center gap-1">
                                    <Plus className="w-5 h-5" />
                                    <span className="text-[10px] font-mono">Add</span>
                                </div>
                            </button>
                        )}
                    </div>
                </div>
            )}
            <input
                ref={inputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(e) => {
                    if (e.target.files?.length)
                        uploadImages(e.target.files, category);
                    e.target.value = "";
                }}
            />
        </div>
    );
}

// ---------------------------------------------------------------------------
// Work-links helpers
// ---------------------------------------------------------------------------
const WORK_LINKS_URL_RE = /https?:\/\/[^\s]+/;

function parseStoredWorkLink(stored) {
    if (typeof stored === "string" && stored.includes(" || ")) {
        const idx = stored.indexOf(" || ");
        const url = stored.slice(idx + 4).trim().replace(/[.,;:!?)\]>]+$/, "");
        return { label: stored.slice(0, idx).trim(), url };
    }
    const url = (stored || "").replace(/[.,;:!?)\]>]+$/, "");
    return { label: "", url };
}


function parseWorkLinksText(text) {
    return text
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
            const match = WORK_LINKS_URL_RE.exec(line);
            if (!match) return null;
            // Strip trailing punctuation that may have been captured by the greedy [^\s]+ match
            const url = match[0].replace(/[.,;:!?)\]>]+$/, "");
            const before = line.slice(0, match.index).replace(new RegExp("[-:" + "\\s" + "|]+$"), "").trim();
            return before ? `${before} || ${url}` : url;
        })
        .filter(Boolean);
}


function linksToText(links) {
    return (links || [])
        .map((w) => {
            const { label, url } = parseStoredWorkLink(w);
            return label ? `${label} - ${url}` : url;
        })
        .join("\n");
}

function WorkLinksEditor({ links, onChange }) {
    const [draft, setDraft] = useState(() => linksToText(links));

    // Keep draft in sync if links change externally (e.g. prefill)
    const linksKey = JSON.stringify(links || []);
    const prevLinksKey = React.useRef(linksKey);
    React.useEffect(() => {
        if (prevLinksKey.current !== linksKey) {
            setDraft(linksToText(JSON.parse(linksKey)));
            prevLinksKey.current = linksKey;
        }
    }, [linksKey]);

    const parsed = parseWorkLinksText(draft);

    const handleChange = (e) => {
        const text = e.target.value;
        setDraft(text);
        onChange(parseWorkLinksText(text));
    };

    return (
        <div className="mt-2 space-y-3" data-testid="work-links-editor">
            <textarea
                value={draft}
                onChange={handleChange}
                data-testid="work-link-input"
                rows={5}
                placeholder={
                    "Paste all your work links here, one per line.\n" +
                    "Examples:\n" +
                    "Puma Campaign - https://instagram.com/reel/abc\n" +
                    "Pepsi - https://youtu.be/xyz\n" +
                    "https://vimeo.com/showreel"
                }
                className="w-full bg-white/60 border border-[#eaeaea] rounded-2xl p-4 text-[16px] md:text-[14px] text-[#111111] placeholder:text-[#333333] focus:ring-4 focus:ring-[#0c2340]/10 focus:border-[#0c2340]/40 outline-none transition-all duration-200 resize-y font-mono leading-relaxed shadow-[0_1px_2px_rgba(0,0,0,0.03)]"
            />
            <div className="flex items-center gap-2">
                <span
                    className={`text-[11px] font-mono px-2 py-0.5 rounded-full border ${
                        parsed.length > 0
                            ? "text-emerald-700 bg-emerald-50 border-emerald-100"
                            : "text-[#333333] bg-slate-50 border-slate-100"
                    }`}
                >
                    Detected Links: {parsed.length}
                </span>
                {parsed.length > 0 && (
                    <span className="text-[10px] text-[#333333]">
                        {parsed.map((s) => parseStoredWorkLink(s).label || "Unlabeled").join(" · ")}
                    </span>
                )}
            </div>
            {parsed.length > 0 && (
                <div className="space-y-1.5 pt-1" data-testid="work-links-preview">
                    {parsed.map((stored, i) => {
                        const { label, url } = parseStoredWorkLink(stored);
                        return (
                            <div
                                key={i}
                                className="flex items-center gap-2 px-3 py-2 bg-white/60 border border-[#eaeaea] rounded-xl"
                                data-testid={`work-link-row-${i}`}
                            >
                                {label && (
                                    <span className="text-[11px] text-[#333333] font-medium shrink-0 max-w-[120px] truncate">
                                        {label}
                                    </span>
                                )}
                                <a
                                    href={url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-[11px] font-mono text-[#222222] hover:text-[#111111] truncate underline underline-offset-2 flex-1 min-w-0"
                                >
                                    {url}
                                </a>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

function PremiumFormField({
    label,
    value,
    onChange,
    onBlur,
    type = "text",
    required,
    placeholder,
    testid,
    wide,
    hint,
    max,
    disabled,
    className = "",
    error,
    inputRef,
    autoCapitalize,
    autoCorrect,
    spellCheck,
    inputMode: inputModeProp,
    autoComplete: autoCompleteProp,
}) {
    const [localValue, setLocalValue] = useState(value || "");

    // Sync local state when prop value changes externally (e.g. from prefill)
    useEffect(() => {
        setLocalValue(value || "");
    }, [value]);

    // Debounce synchronization to parent state to avoid re-rendering parent tree on every keystroke
    useEffect(() => {
        const handler = setTimeout(() => {
            if (localValue !== (value || "")) {
                onChange(localValue);
            }
        }, 200);
        return () => clearTimeout(handler);
    }, [localValue, onChange, value]);

    const handleBlur = (e) => {
        onChange(localValue);
        if (onBlur) onBlur(e);
    };

    return (
        <label className={`block ${wide ? "md:col-span-2" : ""}`}>
            <span className="text-[11px] text-[#111111] tracking-[0.08em] font-semibold uppercase font-mono">
                {label}
            </span>
            {type === "date" ? (
                <DobInput
                    inputRef={inputRef}
                    value={value || ""}
                    onChange={onChange}
                    onBlur={onBlur}
                    max={max}
                    disabled={disabled}
                    testid={testid}
                    className={`mt-2 w-full bg-white/60 rounded-2xl border focus:ring-4 focus:ring-[#0c2340]/10 outline-none py-3 px-4 text-[16px] md:text-[15px] text-[#111111] placeholder:text-[#333333] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)] disabled:text-[#333333] ${
                        error
                            ? "border-rose-400 focus:border-rose-400 focus:ring-rose-400/10 bg-rose-50/30"
                            : "border-[#eaeaea] focus:border-[#0c2340]/40 bg-white/60"
                    } ${className}`}
                />
            ) : (
            <input
                ref={inputRef}
                type={type}
                value={localValue}
                onChange={(e) => setLocalValue(e.target.value)}
                onBlur={handleBlur}
                required={required}
                placeholder={placeholder}
                max={max}
                disabled={disabled}
                inputMode={
                    inputModeProp ||
                    (type === "email"
                        ? "email"
                        : type === "tel"
                          ? "tel"
                          : type === "number"
                            ? "numeric"
                            : undefined)
                }
                enterKeyHint="next"
                autoComplete={
                    autoCompleteProp ||
                    (type === "email"
                        ? "email"
                        : type === "tel"
                          ? "tel"
                          : undefined)
                }
                autoCapitalize={autoCapitalize}
                autoCorrect={autoCorrect}
                spellCheck={spellCheck}
                data-testid={testid}
                className={`mt-2 w-full bg-white/60 rounded-2xl border focus:ring-4 focus:ring-[#0c2340]/10 outline-none py-3 px-4 text-[16px] md:text-[15px] text-[#111111] placeholder:text-[#333333] transition-all duration-200 shadow-[0_1px_2px_rgba(0,0,0,0.03)] disabled:text-[#333333] ${
                    error
                        ? "border-rose-400 focus:border-rose-400 focus:ring-rose-400/10 bg-rose-50/30"
                        : "border-[#eaeaea] focus:border-[#0c2340]/40 bg-white/60"
                } ${className}`}
            />
            )}
            {error && (
                <span className="block text-[11px] text-rose-500 mt-1.5 font-mono animate-in fade-in duration-150">
                    ⚠ {error}
                </span>
            )}
            {!error && hint && (
                <span className="block text-[10.5px] text-[#333333] mt-1 font-mono">
                    {hint}
                </span>
            )}
        </label>
    );
}

function PremiumUploadSlot({
    title,
    required,
    note,
    icon: Icon,
    error,
    accept,
    inputRef,
    onPick,
    uploadState, // replacement state mapping
    media,
    onRemove,
    testid,
    compact,
    cameraCapture, // "user" | "environment" — shows a camera-first option on mobile
    onRetry,       // optional: shown when this slot has a failed retry queue entry
    failed,
    hint,
}) {
    const hasFile = Boolean(media);
    const cameraRef = useRef(null);
    const isVideo = (accept || "").includes("video");
    const isPending = uploadState && uploadState.status !== "completed";
    const [isVideoCollapsed, setIsVideoCollapsed] = useState(() => {
        return isVideo && hasFile && typeof window !== "undefined" && window.innerWidth < 768;
    });

    // WhatsApp/Drive-style upload architecture (2026-08): the talent is never
    // shown a phase name (Optimizing/Compressing/Processing/Uploading) or a
    // percentage here — this button only renders BEFORE the media is
    // confirmed (`hasFile`, below, swaps in a completely different "media
    // card" branch once it's genuinely attached), so every in-flight status
    // (queued/compressing/uploading/processing) reads as one minimal
    // acknowledgment: the file was selected and is being sent in the
    // background. A failed upload is the one state that DOES need to say
    // something actionable — see the "Couldn't send this file" retry button
    // below, not this label.
    const uploadStatusText = !uploadState || uploadState.status === "failed"
        ? "Tap to upload"
        : "✓ Added";

    return (
        <div
            className={`${compact ? "mb-4" : "mb-10"} ${
                error ? "rounded-2xl ring-2 ring-rose-400/60 bg-rose-50/20 p-4" : ""
            }`}
        >
            {!compact && (
                <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2.5">
                        {Icon && (
                            <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-[#0c2340]/8 text-[#0c2340] shrink-0">
                                <Icon className="w-4 h-4" />
                            </span>
                        )}
                        <h3 className="font-display text-base sm:text-lg font-bold tracking-tight text-[#0c2340] uppercase">
                            {title}
                            {required && (
                                <span className="text-rose-500"> *</span>
                            )}
                        </h3>
                    </div>
                    {hasFile && (
                        <span className="inline-flex items-center gap-1 text-[10px] tracking-[0.2em] uppercase font-mono text-emerald-600">
                            <Check className="w-3 h-3" /> Uploaded
                        </span>
                    )}
                </div>
            )}
            {!compact && note && (
                <p className="text-[12px] leading-relaxed text-[#333333] mb-5">
                    {note}
                </p>
            )}
            {!compact && hint && (
                <p className="text-[11px] text-[#333333] font-mono mb-5">
                    {hint}
                </p>
            )}
            {hasFile ? (
                isVideo ? (
                    <div className="bg-white border border-[#eaeaea] rounded-3xl p-4 flex flex-col gap-3 shadow-[0_4px_20px_rgba(15,23,42,0.03)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.05)] text-left">
                        <div className="flex items-center justify-between border-b border-slate-100 pb-2.5">
                            <div className="flex items-center gap-2">
                                <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-slate-100 text-[#111111]">
                                    <Video className="w-4 h-4" />
                                </span>
                                <div>
                                    <h4 className="font-bold text-sm text-[#111111]">{title || "Video"}</h4>
                                    <p className="text-[11px] text-[#333333] truncate max-w-[180px] font-mono">{media.original_filename || "video_file"}</p>
                                </div>
                            </div>
                            <div className="flex items-center gap-2 shrink-0">
                                {(media.profile_sync_status === "declined" || media.profile_sync_status === "synced") && (
                                    <MediaSyncStatusBadge status={media.profile_sync_status} className="text-[#333333] bg-slate-100 border border-[#eaeaea]" />
                                )}
                                <button
                                    type="button"
                                    onClick={() => setIsVideoCollapsed(!isVideoCollapsed)}
                                    className="p-1.5 border border-[#eaeaea] hover:border-[#d4d4d4] hover:bg-slate-50 rounded-full text-[#333333] transition-all duration-200"
                                    title={isVideoCollapsed ? "Expand preview" : "Collapse preview"}
                                >
                                    <ChevronDown
                                        className={`h-4 w-4 transform transition-transform duration-200 ${
                                            isVideoCollapsed ? "-rotate-90" : ""
                                        }`}
                                    />
                                </button>
                            </div>
                        </div>

                        {!isVideoCollapsed && (
                            media.status === "processing" ? (
                                <div className="relative rounded-2xl overflow-hidden bg-slate-900 border border-slate-100 flex flex-col items-center justify-center p-8 min-h-[160px] w-full animate-fadeIn">
                                    <div className="w-8 h-8 rounded-full border-2 border-emerald-500 border-t-transparent animate-spin mb-3"></div>
                                    <p className="text-xs font-mono text-[#eaeaea] animate-pulse">Finishing up...</p>
                                </div>
                            ) : (
                                <div className="relative rounded-2xl overflow-hidden bg-slate-900 border border-slate-100 flex items-center justify-center max-h-[240px] animate-fadeIn">
                                    <HlsVideo
                                        src={media.url}
                                        poster={posterUrl(media) || thumbnailUrl(media)}
                                        controls
                                        playsInline
                                        preload="metadata"
                                        className="w-full max-h-[240px] object-contain rounded-2xl"
                                    />
                                    {media.duration && (
                                        <span className="absolute bottom-3 right-3 bg-black/75 backdrop-blur-sm text-white text-[10px] font-mono font-medium px-2 py-0.5 rounded-full shadow-sm">
                                            {formatDuration(media.duration)}
                                        </span>
                                    )}
                                </div>
                            )
                        )}
                        <div className="flex flex-col gap-1.5 px-1">
                            {formatMediaTimestamp(media) && (
                                <span className="text-[11px] text-[#333333] font-mono">
                                    Updated {formatMediaTimestamp(media)}
                                </span>
                            )}
                        </div>
                        <div className="flex items-center gap-2 pt-2 border-t border-slate-100">
                            {isPending ? (
                                <div className="w-full px-1 flex items-center gap-2 text-xs font-mono text-[#333333]">
                                    {uploadState.status === "failed" ? (
                                        <span className="text-rose-600">Couldn't send the replacement. Tap to retry.</span>
                                    ) : (
                                        <>
                                            <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />
                                            <span>✓ Added — replacing in the background</span>
                                        </>
                                    )}
                                </div>
                            ) : (
                                <div className="flex items-center gap-2 w-full">
                                    <button
                                        type="button"
                                        onClick={() => inputRef.current?.click()}
                                        className="flex-1 border border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111] hover:bg-slate-50 px-4 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-1.5 min-h-[40px] bg-white transition-all active:scale-[0.98]"
                                    >
                                        <Upload className="w-3.5 h-3.5" />
                                        Replace
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => onRemove(media)}
                                        className="flex-1 border border-rose-200 hover:border-rose-300 text-rose-600 hover:bg-rose-50 px-4 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-1.5 min-h-[40px] bg-white transition-all active:scale-[0.98]"
                                    >
                                        <Trash2 className="w-3.5 h-3.5" />
                                        Delete
                                    </button>
                                </div>
                            )}
                        </div>
                    </div>
                ) : (
                    <div className="bg-white/60 border border-[#eaeaea] rounded-2xl p-3 flex items-center gap-3 shadow-[0_1px_2px_rgba(0,0,0,0.02)]">
                        <Icon className="w-4 h-4 text-[#333333] shrink-0" />
                        <div className="min-w-0 flex-1">
                            <div className="text-[13px] truncate">
                                {compact && (
                                    <span className="font-display mr-2 text-[#111111]">
                                        {title}
                                        {required && (
                                            <span className="text-rose-500">
                                                {" "}*
                                            </span>
                                        )}
                                    </span>
                                )}
                                <span className="text-[#333333] font-mono text-[11px]">
                                    {media.original_filename || "file"}
                                </span>
                            </div>
                        </div>
                        <button
                            onClick={() => onRemove(media)}
                            className="text-[#333333] hover:text-rose-500 p-1 min-w-[44px] min-h-[44px] flex items-center justify-center transition-colors"
                        >
                            <Trash2 className="w-4 h-4" />
                        </button>
                    </div>
                )
            ) : (
                <>
                    {/* Mobile: camera-first dual buttons. Desktop: single
                        upload trigger. The camera input carries `capture`
                        which makes iOS/Android jump straight into the
                        recorder UI. */}
                    {cameraCapture && (
                        <div className="md:hidden grid grid-cols-2 gap-2 mb-3">
                            <button
                                type="button"
                                onClick={() => cameraRef.current?.click()}
                                disabled={isPending}
                                data-testid={`${testid}-camera-btn`}
                                className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3.5 text-[13px] rounded-full flex items-center justify-center gap-2 min-h-[52px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                            >
                                <Camera className="w-4 h-4" />
                                {isVideo ? "Record" : "Take photo"}
                            </button>
                            <button
                                type="button"
                                onClick={() => inputRef.current?.click()}
                                disabled={isPending}
                                data-testid={`${testid}-library-btn`}
                                className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3.5 text-[13px] rounded-full flex items-center justify-center gap-2 min-h-[52px] active:scale-[0.97] transition-all duration-200 bg-white/60"
                            >
                                <FolderOpen className="w-4 h-4" />
                                From library
                            </button>
                        </div>
                    )}
                    <button
                        onClick={() => inputRef.current?.click()}
                        disabled={isPending}
                        data-testid={`${testid}-btn`}
                        className={`w-full bg-gradient-to-b from-white to-slate-50/70 border border-[#eaeaea] hover:border-[#0c2340]/30 p-4 text-left min-h-[60px] flex items-center gap-3 transition-all duration-200 relative overflow-hidden rounded-2xl shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] hover:-translate-y-[1px] ${cameraCapture ? "hidden md:flex" : ""}`}
                    >
                        {/* WhatsApp/Drive-style upload architecture (2026-08): no
                            percentage-fill progress bar here — a subtle spinner is
                            "still sending in the background"; the label above it is
                            the only status the talent needs ("✓ Added"), never a
                            number they're expected to watch tick up. */}
                        {uploadState && uploadState.status !== "failed" ? (
                            <Loader2 className="w-4 h-4 animate-spin relative text-[#222222]" />
                        ) : (
                            <Upload className="w-4 h-4 text-[#333333] relative" />
                        )}
                        {compact ? (
                            <span className="text-[13px] flex-1 relative text-[#111111]">
                                <span className="font-display mr-2">
                                    {title}
                                    {required && (
                                        <span className="text-rose-500"> *</span>
                                    )}
                                </span>
                                <span className="text-[#333333] text-[11px]">
                                    {uploadStatusText}
                                </span>
                            </span>
                        ) : (
                            <span className="text-[13px] text-[#222222] relative">
                                {uploadStatusText}
                            </span>
                        )}
                    </button>
                    {failed && onRetry && (
                        <button
                            type="button"
                            onClick={onRetry}
                            data-testid={`${testid}-retry-btn`}
                            className="mt-3 w-full text-[11px] px-4 py-2.5 border border-rose-200 text-rose-600 hover:bg-rose-50 rounded-full inline-flex items-center justify-center gap-2 min-h-[44px] transition-all duration-200"
                        >
                            <Loader2 className="w-3.5 h-3.5" />
                            Couldn't send this file. Tap to retry.
                        </button>
                    )}
                </>
            )}
            <input
                ref={inputRef}
                type="file"
                accept={accept}
                className="hidden"
                onChange={(e) => {
                    if (e.target.files?.length) onPick(e.target.files);
                    e.target.value = "";
                }}
            />
            {cameraCapture && (
                <input
                    ref={cameraRef}
                    type="file"
                    accept={accept}
                    capture={cameraCapture}
                    className="hidden"
                    onChange={(e) => {
                        if (e.target.files?.length) onPick(e.target.files);
                        e.target.value = "";
                    }}
                />
            )}
        {error && (
            <p className="mt-2 text-[11px] text-rose-500 font-mono animate-in fade-in duration-150">
                ⚠ {error}
            </p>
        )}
        </div>
    );
}

// --------------------------------------------------------------------------
// Renamable take row (existing take) — supports inline label edit + remove
// --------------------------------------------------------------------------
function PremiumTakeRow({ index, media, canRename, onRename, onRemove, onReplace, uploadState }) {
    const [label, setLabel] = useState(media.label || `Take ${index}`);
    const [dirty, setDirty] = useState(false);
    const localInputRef = useRef(null);

    useEffect(() => {
        setLabel(media.label || `Take ${index}`);
        setDirty(false);
    }, [media.label, media.id, index]);

    const save = () => {
        const val = (label || "").trim();
        if (!val) return;
        if (val !== (media.label || "")) onRename(val);
        setDirty(false);
    };

    const isPending = uploadState && uploadState.status !== "completed";

    return (
        <div
            className="bg-white border border-[#eaeaea] rounded-3xl p-4 flex flex-col gap-3 mb-4 shadow-[0_4px_20px_rgba(15,23,42,0.03)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.05)] text-left"
            data-testid={`take-row-${index}`}
        >
            <div className="relative rounded-2xl overflow-hidden bg-slate-900 border border-slate-100 flex items-center justify-center max-h-[220px]">
                <HlsVideo
                    src={media.url}
                    poster={posterUrl(media) || thumbnailUrl(media)}
                    controls
                    playsInline
                    preload="metadata"
                    className="w-full max-h-[220px] object-contain rounded-2xl"
                />
                {media.duration && (
                    <span className="absolute bottom-3 right-3 bg-black/75 backdrop-blur-sm text-white text-[10px] font-mono font-medium px-2 py-0.5 rounded-full shadow-sm">
                        {formatDuration(media.duration)}
                    </span>
                )}
            </div>

            <div className="flex-1 min-w-0 px-1">
                <div className="flex items-center gap-2">
                    <span className="text-[11px] font-mono text-[#0c2340]/70 font-semibold uppercase tracking-wider">Label:</span>
                    {canRename ? (
                        <input
                            value={label}
                            onChange={(e) => {
                                  setLabel(e.target.value);
                                  setDirty(true);
                            }}
                            onBlur={save}
                            onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                    e.preventDefault();
                                    e.currentTarget.blur();
                                }
                            }}
                            placeholder={`Take ${index}`}
                            className={`bg-transparent outline-none text-[13px] font-semibold flex-1 py-1 px-2 rounded-lg border transition-all duration-200 ${dirty ? "border-[#0c2340]/30 bg-[#0c2340]/5" : "border-slate-100 bg-slate-50/50"} focus:border-[#0c2340]/40 text-[#111111]`}
                            data-testid={`take-label-${index}`}
                        />
                    ) : (
                        <div className="text-[13px] font-semibold text-[#111111] py-1">
                            {label}
                            <span className="ml-2 text-[10px] text-[#333333] font-mono font-normal">
                                (legacy)
                            </span>
                        </div>
                    )}
                </div>
                <div className="text-[10px] font-mono text-[#333333] truncate mt-2 flex flex-col gap-0.5">
                    <span>{media.original_filename || "file"}</span>
                    {formatMediaTimestamp(media) && (
                        <span className="text-[#333333] font-medium mt-0.5">
                            Previously uploaded · Last updated: {formatMediaTimestamp(media)}
                        </span>
                    )}
                </div>
            </div>

            <div className="flex items-center gap-2 pt-2 border-t border-slate-100">
                {isPending ? (
                    <div className="w-full px-1 flex items-center gap-2 text-xs font-mono text-[#333333]">
                        {uploadState.status === "failed" ? (
                            <span className="text-rose-600">Couldn't send the replacement. Tap to retry.</span>
                        ) : (
                            <>
                                <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />
                                <span>✓ Added — replacing in the background</span>
                            </>
                        )}
                    </div>
                ) : (
                    <>
                        <button
                            type="button"
                            onClick={() => localInputRef.current?.click()}
                            className="flex-1 border border-[#eaeaea] hover:border-[#d4d4d4] text-[#111111] hover:bg-slate-50 px-4 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-1.5 min-h-[40px] bg-white transition-all active:scale-[0.98]"
                        >
                            <Upload className="w-3.5 h-3.5" />
                            Replace
                        </button>
                        <button
                            type="button"
                            onClick={onRemove}
                            className="flex-1 border border-rose-200 hover:border-rose-300 text-rose-600 hover:bg-rose-50 px-4 py-2.5 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-1.5 min-h-[40px] bg-white transition-all active:scale-[0.98]"
                            data-testid={`take-remove-${index}`}
                        >
                            <Trash2 className="w-3.5 h-3.5" />
                            Delete
                        </button>
                    </>
                )}
            </div>

            <input
                ref={localInputRef}
                type="file"
                accept="video/*"
                className="hidden"
                onChange={(e) => {
                    if (e.target.files?.length) onReplace(e.target.files[0]);
                    e.target.value = "";
                }}
            />
        </div>
    );
}

// --------------------------------------------------------------------------
// Add-a-new-take slot — user picks a file, we upload with the label they type
// (falls back to "Take N" if empty).
// --------------------------------------------------------------------------
function PremiumAddTakeSlot({ number, required, onPick, inputRef }) {
    const [label, setLabel] = useState("");
    const cameraRef = useRef(null);
    const fallback = `Take ${number}`;
    const triggerLib = () => inputRef.current?.click();
    const triggerCam = () => cameraRef.current?.click();

    return (
        <div
            className="bg-gradient-to-b from-white to-slate-50/70 border border-[#eaeaea] hover:border-[#0c2340]/30 rounded-2xl p-3 relative overflow-hidden shadow-[0_1px_2px_rgba(0,0,0,0.02)] hover:shadow-[0_12px_28px_-8px_rgba(0,0,0,0.08)] transition-all duration-200 hover:-translate-y-[1px]"
            data-testid={`add-take-${number}`}
        >
            <div className="flex items-center gap-2 relative">
                <input
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder={`${fallback} — add a label`}
                    className="flex-1 bg-transparent outline-none text-[16px] md:text-[13px] py-1.5 px-3 rounded-xl border border-[#eaeaea] focus:border-[#0c2340]/40 focus:ring-2 focus:ring-[#0c2340]/10 transition-all duration-200 text-[#111111]"
                    enterKeyHint="done"
                    data-testid={`new-take-label-${number}`}
                />
                <button
                    type="button"
                    onClick={triggerLib}
                    className="hidden md:inline-flex relative text-[11px] px-4 py-2 border border-[#eaeaea] hover:border-[#d4d4d4] rounded-full items-center gap-1 disabled:opacity-40 min-h-[44px] bg-white/60 text-[#222222] transition-all duration-200"
                    data-testid={`new-take-upload-${number}`}
                >
                    <Plus className="w-3 h-3" />
                    {"Upload"}
                    {required && <span className="text-rose-500">*</span>}
                </button>
            </div>
            {/* Mobile-only camera-first dual buttons */}
            <div className="md:hidden grid grid-cols-2 gap-2 mt-3 relative">
                <button
                    type="button"
                    onClick={triggerCam}
                    className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60 text-[#222222]"
                    data-testid={`new-take-camera-${number}`}
                >
                    <Camera className="w-3.5 h-3.5" /> Record
                </button>
                <button
                    type="button"
                    onClick={triggerLib}
                    className="border border-[#eaeaea] hover:border-[#d4d4d4] p-3 text-[12px] rounded-full inline-flex items-center justify-center gap-2 min-h-[48px] active:scale-[0.97] transition-all duration-200 bg-white/60 text-[#222222]"
                    data-testid={`new-take-library-${number}`}
                >
                    <FolderOpen className="w-3.5 h-3.5" /> Library
                    {required && <span className="text-rose-500">*</span>}
                </button>
            </div>
            <input
                ref={inputRef}
                type="file"
                accept="video/*"
                className="hidden"
                onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) onPick(f, (label || "").trim() || fallback);
                    e.target.value = "";
                    setLabel("");
                }}
            />
            <input
                ref={cameraRef}
                type="file"
                accept="video/*"
                capture="user"
                className="hidden"
                onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) onPick(f, (label || "").trim() || fallback);
                    e.target.value = "";
                    setLabel("");
                }}
            />
        </div>
    );
}

// ALMOST DONE — new-talent end-of-flow authentication card. Renders only
// once the submission is otherwise fully ready (see the READY-TO-SUBMIT
// FOOTER block in SubmissionPage). The email is already known (collected
// with zero friction at "UPLOAD TEST" — handleInlineLookup's not-found
// branch); this just proves ownership of it via the exact same OTP UI
// pattern Step A/B already use, right before the talent's first submit.
function AlmostDoneAuthCard({
    email,
    gatewayLoading,
    otpSent,
    otpValue,
    setOtpValue,
    otpLoading,
    otpResending,
    onSendCode,
    onVerify,
    onResend,
    onGoogle,
}) {
    return (
        <div
            className="mb-6 flex flex-col gap-4 border border-slate-100 rounded-2xl p-5 bg-slate-50/50 text-left"
            data-testid="almost-done-auth-card"
        >
            <div>
                <h4 className="font-display text-lg font-bold text-slate-900">Almost Done</h4>
                <p className="text-xs text-[#333333] mt-1">
                    Confirm your identity to save your profile and submit your application.
                </p>
            </div>

            {otpSent ? (
                <div className="flex flex-col gap-3">
                    <label className="text-xs font-semibold text-[#111111] uppercase tracking-wider">
                        Enter Verification Code
                    </label>
                    <p className="text-xs text-[#333333]">
                        We've sent a code to {email}
                    </p>
                    <input
                        type="text"
                        inputMode="numeric"
                        maxLength={6}
                        value={otpValue}
                        onChange={(e) => setOtpValue(e.target.value.replace(/\D/g, ""))}
                        placeholder="6-digit code"
                        style={{ fontSize: "16px" }}
                        className="w-full px-4 py-3 bg-white border border-[#eaeaea] rounded-xl text-[#111111] placeholder:text-[#333333] focus:border-slate-500 focus:outline-none transition duration-150 h-[48px] text-center tracking-[0.3em] font-mono"
                        data-testid="almost-done-otp-input"
                    />
                    <button
                        type="button"
                        onClick={onVerify}
                        disabled={otpLoading}
                        data-testid="almost-done-verify-btn"
                        className="w-full bg-slate-900 text-white px-4 py-3 rounded-full text-[13px] font-semibold hover:bg-slate-800 active:scale-[0.98] disabled:opacity-40 transition-all duration-150 inline-flex items-center justify-center gap-2 min-h-[48px]"
                    >
                        {otpLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                        {otpLoading ? "Verifying…" : "Verify & Submit"}
                    </button>
                    <button
                        type="button"
                        onClick={onResend}
                        disabled={otpResending}
                        data-testid="almost-done-resend-btn"
                        className="w-full text-[#333333] text-xs font-medium py-1 hover:text-[#111111] transition-colors disabled:opacity-40"
                    >
                        {otpResending ? "Resending…" : "Resend code"}
                    </button>
                </div>
            ) : (
                <div className="flex flex-col gap-3">
                    <button
                        type="button"
                        onClick={onGoogle}
                        className="w-full bg-white border border-[#eaeaea] hover:bg-slate-50 text-[#111111] py-3 px-4 rounded-xl text-xs font-semibold inline-flex items-center justify-center gap-2.5 transition duration-150 shadow-sm active:scale-[0.98]"
                        data-testid="almost-done-google-btn"
                    >
                        <svg className="w-4 h-4" viewBox="0 0 24 24">
                            <path fill="#EA4335" d="M12 5.04c1.78 0 3.38.61 4.64 1.8l3.46-3.46C17.99 1.19 15.21 0 12 0 7.31 0 3.28 2.69 1.34 6.61l4.08 3.16C6.4 7.02 9.01 5.04 12 5.04z" />
                            <path fill="#4285F4" d="M23.49 12.27c0-.81-.07-1.59-.2-2.36H12v4.51h6.46c-.29 1.48-1.14 2.73-2.4 3.58l3.73 2.89c2.18-2.01 3.7-4.97 3.7-8.62z" />
                            <path fill="#FBBC05" d="M5.42 14.78c-.24-.72-.38-1.49-.38-2.28s.14-1.56.38-2.28L1.34 7.06C.48 8.79 0 10.74 0 12.8s.48 4.01 1.34 5.74l4.08-3.76z" />
                            <path fill="#34A853" d="M12 24c3.24 0 5.97-1.07 7.96-2.91l-3.73-2.89c-1.04.7-2.36 1.11-4.23 1.11-3.01 0-5.6-1.98-6.51-4.73L1.34 17.68C3.28 21.6 7.31 24 12 24z" />
                        </svg>
                        Continue with Google
                    </button>
                    <div className="flex items-center my-0.5">
                        <div className="flex-grow border-t border-[#eaeaea]"></div>
                        <span className="mx-4 text-[10px] text-[#888888] font-mono uppercase tracking-wider">or</span>
                        <div className="flex-grow border-t border-[#eaeaea]"></div>
                    </div>
                    <button
                        type="button"
                        onClick={onSendCode}
                        disabled={gatewayLoading}
                        data-testid="almost-done-send-code-btn"
                        className="w-full bg-slate-900 text-white px-4 py-3 rounded-full text-[13px] font-semibold hover:bg-slate-800 active:scale-[0.98] disabled:opacity-40 transition-all duration-150 inline-flex items-center justify-center gap-2 min-h-[48px]"
                    >
                        {gatewayLoading ? "Sending code…" : `Send code to ${email}`}
                    </button>
                </div>
            )}
        </div>
    );
}

function FeedbackRow({ fb }) {
    const isVoice = fb.type === "voice";
    return (
        <div
            className="bg-white/60 border border-[#eaeaea] rounded-2xl p-5 shadow-[0_1px_2px_rgba(0,0,0,0.02)] transition-all duration-200 hover:shadow-[0_8px_25px_-6px_rgba(0,0,0,0.06)]"
            data-testid={`talent-feedback-${fb.id}`}
        >
            <div className="flex items-center justify-between gap-3 mb-3">
                <span className="inline-flex items-center gap-1.5 text-[10px] tracking-[0.2em] uppercase font-mono text-[#333333]">
                    {isVoice ? (
                        <Mic className="w-3 h-3" />
                    ) : (
                        <MessageSquare className="w-3 h-3" />
                    )}
                    {isVoice ? "Voice" : "Text"}
                </span>
                <span className="text-[10px] font-mono text-[#333333]">
                    Received {timeAgo(fb.approved_at || fb.created_at)}
                </span>
            </div>
            {isVoice ? (
                <VoiceFeedbackPlayer src={fb.content_url} testId={`talent-feedback-audio-${fb.id}`} />
            ) : (
                <p
                    className="text-[13px] leading-relaxed text-[#111111] whitespace-pre-wrap"
                    data-testid={`talent-feedback-text-${fb.id}`}
                >
                    {fb.text}
                </p>
            )}
        </div>
    );
}

// P2 — a bare native `<audio controls>` looked visually inconsistent with
// the rest of the designed feedback card (and sat at a static 0:00/0:00
// until pressed). Same single <audio> element underneath for actual
// playback; only the chrome around it is custom. Scoped to this file's own
// FeedbackRow only — components/shared/FeedbackRow.jsx (ProjectDetail's
// copy) is untouched.
function VoiceFeedbackPlayer({ src, testId }) {
    const audioRef = useRef(null);
    const [isPlaying, setIsPlaying] = useState(false);
    const [currentTime, setCurrentTime] = useState(0);
    const [duration, setDuration] = useState(0);

    const togglePlay = () => {
        const el = audioRef.current;
        if (!el) return;
        if (isPlaying) el.pause();
        else el.play();
    };

    const formatTime = (s) => {
        if (!Number.isFinite(s) || s < 0) return "0:00";
        const m = Math.floor(s / 60);
        const sec = Math.floor(s % 60);
        return `${m}:${String(sec).padStart(2, "0")}`;
    };

    const progress = duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0;

    return (
        <div
            className="flex items-center gap-3 bg-slate-50/70 border border-slate-100 rounded-xl px-4 py-3"
            data-testid={testId}
        >
            <button
                type="button"
                onClick={togglePlay}
                aria-label={isPlaying ? "Pause voice note" : "Play voice note"}
                className="shrink-0 w-9 h-9 rounded-full bg-[#0c2340] text-white flex items-center justify-center hover:opacity-90 active:scale-95 transition-all duration-150"
            >
                {isPlaying ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5 ml-0.5" />}
            </button>
            <div className="flex-1 min-w-0 h-1.5 rounded-full bg-slate-200 overflow-hidden">
                <div
                    className="h-full bg-[#0c2340] transition-[width] duration-150"
                    style={{ width: `${progress}%` }}
                />
            </div>
            <span className="shrink-0 text-[10px] font-mono text-[#333333] tabular-nums" style={{ fontVariantNumeric: "tabular-nums" }}>
                {formatTime(currentTime)} / {formatTime(duration)}
            </span>
            <audio
                ref={audioRef}
                src={src}
                className="hidden"
                onPlay={() => setIsPlaying(true)}
                onPause={() => setIsPlaying(false)}
                onEnded={() => setIsPlaying(false)}
                onLoadedMetadata={(e) => setDuration(e.currentTarget.duration || 0)}
                onTimeUpdate={(e) => {
                    setCurrentTime(e.currentTarget.currentTime);
                    setDuration(e.currentTarget.duration || 0);
                }}
            />
        </div>
    );
}

function timeAgo(iso) {
    if (!iso) return "";
    const ts = new Date(iso).getTime();
    if (Number.isNaN(ts)) return "";
    const diff = (Date.now() - ts) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

class SubmissionErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false, error: null };
    }

    static getDerivedStateFromError(error) {
        return { hasError: true, error };
    }

    componentDidCatch(error, errorInfo) {
        console.error("SubmissionPage crashed:", error, errorInfo);
    }

    render() {
        if (this.state.hasError) {
            return (
                <div className="min-h-dvh flex items-center justify-center bg-slate-50 px-4 py-12 sm:px-6 lg:px-8">
                    <div className="max-w-md w-full space-y-8 p-8 bg-white rounded-3xl border border-[#eaeaea] shadow-sm text-center">
                        <div className="w-16 h-16 bg-rose-50 text-rose-600 rounded-full flex items-center justify-center mx-auto mb-4">
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-8 h-8">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 7.5h.008v.008H12v-.008Z" />
                            </svg>
                        </div>
                        <h2 className="text-xl font-semibold text-slate-950 tracking-tight">Something went wrong</h2>
                        <p className="mt-2 text-sm text-[#333333] leading-relaxed">
                            An unexpected error occurred while loading this page. Don't worry, your progress has not been lost. Please try reloading.
                        </p>
                        <div className="mt-6 flex flex-col gap-2">
                            <button
                                onClick={() => window.location.reload()}
                                className="w-full bg-slate-900 text-white py-3 rounded-full text-[13px] font-medium hover:bg-slate-800 active:scale-[0.97] transition-all duration-200"
                            >
                                Reload Page
                            </button>
                            <button
                                onClick={() => {
                                    localStorage.clear();
                                    window.location.reload();
                                }}
                                className="w-full text-[#333333] py-3 rounded-full text-[12px] font-medium hover:text-[#111111] transition-all duration-200"
                            >
                                Clear Cache & Reload
                            </button>
                        </div>
                    </div>
                </div>
            );
        }
        return this.props.children;
    }
}

export default function SubmissionPageWithErrorBoundary(props) {
    return (
        <SubmissionErrorBoundary>
            <SubmissionPage {...props} />
        </SubmissionErrorBoundary>
    );
}
