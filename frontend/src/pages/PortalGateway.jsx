import React, { useState, useEffect } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, Sparkles, MapPin, User, Mail, ChevronRight } from "lucide-react";
import Logo from "@/components/Logo";
import { toast } from "sonner";
import { api as axios } from "@/lib/api";

export default function PortalGateway() {
    const { slug } = useParams();
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const emailParam = searchParams.get("email");

    const [email, setEmail] = useState("");
    const [loading, setLoading] = useState(false);
    const [recognitionState, setRecognitionState] = useState(null); // talent data or null

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

    const handleContinueToPortal = () => {
        if (!recognitionState || !recognitionState.email) return;
        
        // Save simple local storage session
        localStorage.setItem("talentgram_portal_email", recognitionState.email);
        toast.success(`Welcome back, ${recognitionState.name}!`);
        
        // Route to home
        navigate("/portal/home");
    };

    const handleUseAnotherEmail = () => {
        setRecognitionState(null);
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
                                Continue your Talentgram submission
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
                        </div>

                        <p className="text-[11px] text-black/40 text-center tracking-wide mt-2">
                            New talents can continue using the same flow.
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
                                        DOB: {recognitionState.dob}
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
                            <button
                                onClick={handleContinueToPortal}
                                className="w-full inline-flex items-center justify-center gap-2 bg-black text-white px-6 py-3 rounded-lg text-sm font-medium hover:opacity-90 active:scale-[0.99] transition-all duration-150 h-[48px]"
                            >
                                Continue to Portal
                                <ChevronRight className="w-4 h-4" />
                            </button>
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
