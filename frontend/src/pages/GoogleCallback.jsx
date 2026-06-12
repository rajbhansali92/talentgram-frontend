import React, { useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api as axios } from "@/lib/api";
import { toast } from "sonner";
import { Loader2 } from "lucide-react";

export default function GoogleCallback() {
    const [searchParams] = useSearchParams();
    const navigate = useNavigate();
    const ranRef = useRef(false);

    useEffect(() => {
        if (ranRef.current) return;
        ranRef.current = true;

        const code = searchParams.get("code");
        const state = searchParams.get("state") || ""; // Holds project slug

        if (!code) {
            toast.error("Google authentication failed. No authorization code received.");
            navigate(state ? `/submit/${state}` : "/");
            return;
        }

        const exchangeCode = async () => {
            try {
                // Determine origin dynamically for redirect_uri
                const redirect_uri = `${window.location.origin}/google-callback`;
                
                const { data } = await axios.post("/auth/google", {
                    code,
                    redirect_uri,
                    slug: state
                });

                const getRedirectPath = (s) => {
                    if (!s) return "/";
                    if (s === "apply") return "/apply";
                    return `/submit/${s}`;
                };

                if (data.existing) {
                    if (state === "apply" && data.token && data.application_id) {
                        const ref = {
                            aid: data.application_id,
                            token: data.token,
                            basics: {
                                first_name: data.talent?.first_name || "",
                                last_name: data.talent?.last_name || "",
                                email: data.email,
                                phone: data.talent?.phone || ""
                            },
                            savedAt: Date.now()
                        };
                        localStorage.setItem("tg_application", JSON.stringify(ref));
                        toast.success("Welcome back! Resuming your application.");
                    } else if (data.token && data.submission_id) {
                        // Existing talent with submission -> resume and unlock
                        const ref = { id: data.submission_id, token: data.token };
                        localStorage.setItem(`tg_submission_${state}`, JSON.stringify(ref));
                        localStorage.setItem(`tg_atk_${state}`, data.token);
                        toast.success("Welcome back! Resuming your audition submission.");
                    } else {
                        // Existing talent, no submission/application yet -> prefill details
                        localStorage.setItem("talentgram_google_email", data.email);
                        localStorage.setItem("talentgram_google_first_name", data.first_name || "");
                        localStorage.setItem("talentgram_google_last_name", data.last_name || "");
                        localStorage.setItem("talentgram_google_profile_data", JSON.stringify(data));
                        toast.success(`Welcome back, ${data.first_name || "Talent"}!`);
                    }
                } else {
                    // New talent -> save Google identity details for onboarding
                    localStorage.setItem("talentgram_google_email", data.email);
                    localStorage.setItem("talentgram_google_first_name", data.name?.split(" ")[0] || "");
                    localStorage.setItem("talentgram_google_last_name", data.name?.split(" ").slice(1).join(" ") || "");
                    localStorage.setItem("talentgram_google_avatar", data.picture || "");
                    toast.success("Successfully authenticated with Google. Welcome to Talentgram!");
                }

                navigate(getRedirectPath(state));
            } catch (err) {
                console.error("Google authentication error:", err);
                toast.error(err?.response?.data?.detail || "Google authentication failed. Please try again.");
                const redirectPath = state === "apply" ? "/apply" : (state ? `/submit/${state}` : "/");
                navigate(redirectPath);
            }
        };

        exchangeCode();
    }, [searchParams, navigate]);

    return (
        <div className="min-h-screen bg-[#050505] text-white flex flex-col items-center justify-center px-4">
            <div className="text-center max-w-sm">
                <Loader2 className="w-8 h-8 animate-spin mx-auto text-white/80 mb-4" />
                <h2 className="font-display text-xl mb-2">Authenticating with Google</h2>
                <p className="text-xs text-white/40 tracking-wider font-mono">
                    Please wait while we verify your identity...
                </p>
            </div>
        </div>
    );
}
