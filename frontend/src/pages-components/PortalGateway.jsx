import React, { useState, useEffect } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, Sparkles, MapPin, User, Mail, ChevronRight } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { api as axios, PORTAL_TOKEN_KEY } from "@/lib/api";
import { isoToDisplay } from "@/lib/dob";

export default function PortalGateway() {
    const { slug: slugParam } = useParams();
    // No :slug segment means this is the standalone Dashboard login entry
    // (/portal/login) — reuse the existing "apply"-style project-less OTP/
    // Google verification path (see auth.py's `slug == "portal"` branch)
    // rather than requiring a real project context.
    const isStandalone = !slugParam;
    const slug = slugParam || "portal";
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const emailParam = searchParams.get("email");

    const [email, setEmail] = useState("");
    const [loading, setLoading] = useState(false);
    const [recognitionState, setRecognitionState] = useState(null); // talent data or null

    // OTP gate (Path A): portal access requires proof of email ownership.
    const [otpSent, setOtpSent] = useState(false);
    const [otpCode, setOtpCode] = useState("");
    const [otpBusy, setOtpBusy] = useState(false);

    useEffect(() => {
        // Prefill email if provided in query param
        if (emailParam) {
            setEmail(emailParam);
        }
    }, [emailParam]);

    const handleLookup = async (e) => {
        if (e) e.preventDefault();
        const trimmedEmail = email.trim().toLowerCase();
        if (!trimmedEmail || !trimmedEmail.includes("@")) {
            toast.error("Please enter a valid email address");
            return;
        }

        setLoading(true);
        try {
            const { data } = await axios.post("/portal/lookup", { email: trimmedEmail });
            
            if (data.exists) {
                // Set recognition payload
                setRecognitionState(data.talent);
            } else if (isStandalone) {
                // No project context to send an unrecognized email to —
                // building first-time onboarding from a bare dashboard login
                // is out of scope here (see TALENT_MIGRATION_PLAN.md Phase 1
                // item 2). Direct them back to a real entry point instead.
                toast.error("We couldn't find an account for that email. Use your invite or project link to get started.");
            } else {
                // New talent: proceed to submission flow prefilled
                toast.success("Welcome! Directing you to the submission form.");
                navigate(`/submit/${slug}?email=${encodeURIComponent(trimmedEmail)}`);
            }
        } catch (error) {
            console.error("Lookup error:", error);
            toast.error("An error occurred. Please try again.");
        } finally {
            setLoading(false);
        }
    };

    // Step 2a: request an OTP to prove ownership before granting portal access.
    const handleContinueToPortal = async () => {
        if (!recognitionState || !recognitionState.email) return;
        setOtpBusy(true);
        try {
            await axios.post("/auth/otp/send", { email: recognitionState.email });
            setOtpSent(true);
            toast.success("We sent a verification code to your email.");
        } catch (error) {
            console.error("OTP send error:", error);
            toast.error(error?.response?.data?.detail || "Could not send a code. Please try again.");
        } finally {
            setOtpBusy(false);
        }
    };

    // Step 2b: verify the OTP, persist the portal session token, enter portal.
    const handleVerifyOtp = async () => {
        const code = otpCode.trim();
        if (!/^\d{6}$/.test(code)) {
            toast.error("Please enter the 6-digit verification code.");
            return;
        }
        setOtpBusy(true);
        try {
            const { data } = await axios.post("/auth/otp/verify", {
                email: recognitionState.email,
                otp: code,
                slug,
            });
            if (!data?.portal_token) {
                toast.error("Unable to open your portal. Please contact support.");
                return;
            }
            localStorage.setItem(PORTAL_TOKEN_KEY, data.portal_token);
            localStorage.setItem("talentgram_portal_email", recognitionState.email);
            toast.success(`Welcome back, ${recognitionState.name}!`);
            navigate("/portal/home");
        } catch (error) {
            console.error("OTP verify error:", error);
            toast.error(error?.response?.data?.detail || "Invalid or expired verification code.");
        } finally {
            setOtpBusy(false);
        }
    };

    // Standalone Dashboard login only. Reuses the exact same client-side
    // Google OAuth redirect construction as ApplicationPage.jsx's
    // handleGoogleLogin — not a new auth mechanism, just a different `state`
    // value so GoogleCallback.jsx routes the result back here instead of
    // /apply. Backend already grants a portal_token for this state with no
    // change needed (only OTP's verify endpoint required the new "portal"
    // branch — see auth.py).
    const handleGoogleLogin = () => {
        const clientId = process.env.REACT_APP_GOOGLE_CLIENT_ID || "339414275037-rrm7uugj1t4gq2b02q9r51d9l6m39vbe.apps.googleusercontent.com";
        const redirectUri = `${window.location.origin}/google-callback`;
        const state = "portal";
        const scope = "openid profile email";
        window.location.href = `https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=${clientId}&redirect_uri=${encodeURIComponent(redirectUri)}&scope=${encodeURIComponent(scope)}&state=${encodeURIComponent(state)}`;
    };

    const handleUseAnotherEmail = () => {
        setRecognitionState(null);
        setOtpSent(false);
        setOtpCode("");
        setEmail("");
    };

    return (
        <div 
            className="min-h-dvh bg-white text-black flex flex-col justify-between px-6 py-8 md:px-12 select-none"
            data-testid="portal-gateway-page"
        >
            {/* Header / Logo */}
            <div className="w-full flex justify-center py-4">
                <Logo size={96} className="mx-auto" forceVariant="black" />
            </div>

            {/* Main Area */}
            <div className="flex-1 flex flex-col items-center justify-center max-w-md w-full mx-auto my-8">
                {!recognitionState ? (
                    /* Step 1: Input State */
                    <form onSubmit={handleLookup} className="w-full flex flex-col items-center gap-6">
                        <div className="text-center flex flex-col gap-2">
                            <h1 className="text-2xl md:text-3xl font-medium tracking-tight text-black">
                                {isStandalone ? "Sign in to your Talentgram Dashboard" : "Continue your Talentgram submission"}
                            </h1>
                            <p className="text-sm text-black/50">
                                Returning talents can continue instantly using their saved profile.
                            </p>
                        </div>

                        <div className="w-full flex flex-col gap-4">
                            <div className="relative">
                                <Mail className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-black/40" />
                                <input
                                    type="email"
                                    name="email"
                                    value={email}
                                    onChange={(e) => setEmail(e.target.value)}
                                    placeholder="Enter your email address"
                                    disabled={loading}
                                    style={{ fontSize: "16px" }}
                                    className="w-full pl-12 pr-4 py-3 bg-white border border-black/15 rounded-lg text-black placeholder:text-black/30 focus:border-black/50 focus:outline-none transition-all duration-150 h-[48px]"
                                    required
                                />
                            </div>

                            <button
                                type="submit"
                                disabled={loading}
                                className="w-full inline-flex items-center justify-center gap-2 bg-black text-white px-6 py-3 rounded-lg text-sm font-medium hover:opacity-90 active:scale-[0.99] transition-all duration-150 h-[48px]"
                            >
                                {loading ? "Verifying..." : "Continue"}
                                <ArrowRight className="w-4 h-4" strokeWidth={1.5} />
                            </button>

                            {isStandalone && (
                                <>
                                    <div className="flex items-center gap-3 text-[10px] uppercase tracking-wider text-black/35">
                                        <div className="flex-1 h-px bg-black/10" />
                                        or
                                        <div className="flex-1 h-px bg-black/10" />
                                    </div>
                                    <button
                                        type="button"
                                        onClick={handleGoogleLogin}
                                        className="w-full inline-flex items-center justify-center gap-2 border border-black/15 hover:border-black/40 text-black px-6 py-3 rounded-lg text-sm font-medium transition-all duration-150 h-[48px]"
                                    >
                                        Continue with Google
                                    </button>
                                </>
                            )}
                        </div>

                        <p className="text-[11px] text-black/40 text-center tracking-wide mt-2">
                            {isStandalone
                                ? "New here? Use your invite or project link to get started."
                                : "New talents can continue using the same flow."}
                        </p>
                    </form>
                ) : (
                    /* Step 2: Recognition State */
                    <div className="w-full flex flex-col items-center gap-8 animate-in fade-in zoom-in-95 duration-300">
                        <div className="text-center flex flex-col gap-2">
                            <h2 className="text-2xl md:text-3xl font-medium tracking-tight text-black">
                                Is this you?
                            </h2>
                            <p className="text-sm text-black/50">
                                We found your existing profile in our system.
                            </p>
                        </div>

                        {/* Quiet Luxury Talent Card */}
                        <div className="w-full bg-[#fafafa] border border-black/5 rounded-2xl p-6 flex flex-col gap-5 text-left relative overflow-hidden">
                            <div className="flex items-center gap-4">
                                {recognitionState.image_url ? (
                                    <img
                                        src={recognitionState.image_url}
                                        alt={recognitionState.name}
                                        className="w-16 h-16 rounded-full object-cover border border-black/10"
                                    />
                                ) : (
                                    <div className="w-16 h-16 rounded-full bg-black/5 flex items-center justify-center border border-black/10">
                                        <User className="w-6 h-6 text-black/35" />
                                    </div>
                                )}
                                <div>
                                    <h3 className="font-semibold text-lg text-black">{recognitionState.name}</h3>
                                    {recognitionState.location && (
                                        <div className="flex items-center gap-1 text-xs text-black/55 mt-0.5">
                                            <MapPin className="w-3.5 h-3.5" />
                                            <span>{recognitionState.location}</span>
                                        </div>
                                    )}
                                </div>
                            </div>

                            {/* Additional metadata tags */}
                            <div className="flex flex-wrap gap-2 pt-2 border-t border-black/5 text-xs text-black/65">
                                {recognitionState.height && (
                                    <span className="bg-black/5 px-2.5 py-1 rounded-full">
                                        Height: {recognitionState.height}
                                    </span>
                                )}
                                {recognitionState.dob && (
                                    <span className="bg-black/5 px-2.5 py-1 rounded-full">
                                        DOB: {isoToDisplay(recognitionState.dob) || recognitionState.dob}
                                    </span>
                                )}
                                {recognitionState.interested_in && recognitionState.interested_in.map((cat, index) => (
                                    <span key={index} className="bg-black/5 px-2.5 py-1 rounded-full border border-black/5">
                                        {cat}
                                    </span>
                                ))}
                            </div>
                        </div>

                        {/* CTAs */}
                        <div className="w-full flex flex-col gap-3">
                            {!otpSent ? (
                                <button
                                    onClick={handleContinueToPortal}
                                    disabled={otpBusy}
                                    className="w-full inline-flex items-center justify-center gap-2 bg-black text-white px-6 py-3 rounded-lg text-sm font-medium hover:opacity-90 active:scale-[0.99] transition-all duration-150 h-[48px] disabled:opacity-60"
                                >
                                    {otpBusy ? "Sending code..." : "Continue to Portal"}
                                    <ChevronRight className="w-4 h-4" />
                                </button>
                            ) : (
                                <>
                                    <p className="text-xs text-black/55 text-center">
                                        Enter the 6-digit code we emailed to {recognitionState.email}.
                                    </p>
                                    <input
                                        type="text"
                                        inputMode="numeric"
                                        maxLength={6}
                                        value={otpCode}
                                        onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, ""))}
                                        placeholder="••••••"
                                        style={{ fontSize: "16px", letterSpacing: "0.4em" }}
                                        className="w-full text-center px-4 py-3 bg-white border border-black/15 rounded-lg text-black placeholder:text-black/25 focus:border-black/50 focus:outline-none transition-all duration-150 h-[48px]"
                                    />
                                    <button
                                        onClick={handleVerifyOtp}
                                        disabled={otpBusy}
                                        className="w-full inline-flex items-center justify-center gap-2 bg-black text-white px-6 py-3 rounded-lg text-sm font-medium hover:opacity-90 active:scale-[0.99] transition-all duration-150 h-[48px] disabled:opacity-60"
                                    >
                                        {otpBusy ? "Verifying..." : "Verify & enter portal"}
                                        <ChevronRight className="w-4 h-4" />
                                    </button>
                                </>
                            )}
                            <button
                                onClick={handleUseAnotherEmail}
                                className="w-full inline-flex items-center justify-center gap-2 border border-black/15 hover:border-black/40 text-black/80 px-6 py-3 rounded-lg text-sm transition-all duration-150 h-[48px]"
                            >
                                Use another email
                            </button>
                        </div>
                    </div>
                )}
            </div>

            {/* Footer */}
            <footer className="w-full text-center text-[11px] tracking-[0.08em] uppercase text-black/45 py-4">
                <span>© Talentgram Portal</span>
            </footer>
        </div>
    );
}
