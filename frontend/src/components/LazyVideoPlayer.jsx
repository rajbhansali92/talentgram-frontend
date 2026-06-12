import React, { useState, useRef, useEffect } from "react";
import { API } from "@/lib/api";

/**
 * Premium Lazy Video Player component.
 * Renders a Cloudinary poster image with a highly aesthetic glassmorphism play overlay.
 * Replaces the poster with an interactive video player only upon user interaction (click),
 * preventing unnecessary heavy video preloads or bandwidth waste.
 */
export default function LazyVideoPlayer({ src, poster, label, className = "", mediaId, slug, talentId }) {
    const [isPlaying, setIsPlaying] = useState(false);
    const videoRef = useRef(null);
    const lastTrackedTimeRef = useRef(0);
    const hasPlayedRef = useRef(false);
    const hasCompletedRef = useRef(false);

    useEffect(() => {
        if (!isPlaying || !videoRef.current || !slug || !mediaId) return;

        let intervalId = null;

        const trackVideoEvent = (action) => {
            const sid = sessionStorage.getItem("client_session_id") || "guest-session";
            fetch(`${API}/public/links/${slug}/track`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    event_type: "watch_video",
                    session_id: sid,
                    media_id: mediaId,
                    talent_id: talentId,
                    video_action: action
                })
            }).catch(() => {});
        };

        const startTracking = () => {
            if (intervalId) return;
            lastTrackedTimeRef.current = videoRef.current.currentTime;
            intervalId = setInterval(() => {
                if (!videoRef.current) return;
                const current = videoRef.current.currentTime;
                const delta = current - lastTrackedTimeRef.current;
                if (delta > 0 && !videoRef.current.paused) {
                    let sid = sessionStorage.getItem("client_session_id") || "guest-session";
                    fetch(`${API}/public/links/${slug}/track`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            event_type: "watch_video",
                            session_id: sid,
                            media_id: mediaId,
                            talent_id: talentId,
                            watch_time: delta
                        })
                    }).catch(() => {});
                }
                lastTrackedTimeRef.current = current;
            }, 3000);
        };

        const stopTracking = () => {
            if (intervalId) {
                clearInterval(intervalId);
                intervalId = null;
            }
        };

        const handlePlayEvent = () => {
            if (!hasPlayedRef.current) {
                trackVideoEvent("play");
                hasPlayedRef.current = true;
            } else if (hasCompletedRef.current) {
                trackVideoEvent("replay");
                hasCompletedRef.current = false;
            }
            startTracking();
        };

        const handleEndedEvent = () => {
            trackVideoEvent("completion");
            hasCompletedRef.current = true;
            stopTracking();
        };

        const video = videoRef.current;
        video.addEventListener("play", handlePlayEvent);
        video.addEventListener("pause", stopTracking);
        video.addEventListener("ended", handleEndedEvent);

        if (!video.paused) {
            handlePlayEvent();
        }

        return () => {
            video.removeEventListener("play", handlePlayEvent);
            video.removeEventListener("pause", stopTracking);
            video.removeEventListener("ended", handleEndedEvent);
            stopTracking();
        };
    }, [isPlaying, slug, mediaId, talentId]);

    if (!src) return null;

    if (isPlaying) {
        return (
            <div className={`relative w-full h-full aspect-video rounded-xl overflow-hidden bg-black ${className}`}>
                <video
                    ref={videoRef}
                    src={src}
                    controls
                    autoPlay
                    playsInline
                    className="w-full h-full object-contain"
                />
            </div>
        );
    }

    return (
        <div
            onClick={() => setIsPlaying(true)}
            className={`group relative w-full h-full aspect-video rounded-xl overflow-hidden cursor-pointer bg-gradient-to-br from-neutral-900 to-black select-none ${className}`}
            style={{ minHeight: "180px" }}
        >
            {/* Poster image */}
            {poster ? (
                <img
                    src={poster}
                    alt={label || "Video audition preview"}
                    loading="lazy"
                    className="absolute inset-0 w-full h-full object-cover transition-all duration-500 group-hover:scale-105"
                />
            ) : (
                /* Fallback abstract background if no poster is generated */
                <div className="absolute inset-0 w-full h-full flex flex-col items-center justify-center p-4 bg-gradient-to-br from-neutral-850 via-neutral-900 to-black">
                    <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(245,158,11,0.06)_0%,transparent_70%)]" />
                    <span className="text-xs font-semibold tracking-wider text-amber-500/60 uppercase mb-1">
                        Audition Tape
                    </span>
                    <span className="text-sm font-medium text-neutral-400 text-center max-w-[80%] truncate">
                        {label || "Click to play audition"}
                    </span>
                </div>
            )}

            {/* Premium glassmorphic backdrop-filter play overlay */}
            <div className="absolute inset-0 flex items-center justify-center bg-black/20 transition-colors duration-300 group-hover:bg-black/35">
                <div className="flex items-center justify-center w-14 h-14 rounded-full bg-white/10 backdrop-blur-md border border-white/20 text-white shadow-2xl transition-all duration-300 transform scale-95 group-hover:scale-110 group-hover:bg-amber-500 group-hover:border-amber-400 group-hover:shadow-amber-500/35">
                    <svg
                        xmlns="http://www.w3.org/2000/svg"
                        viewBox="0 0 24 24"
                        fill="currentColor"
                        className="w-6 h-6 ml-0.5 transition-transform duration-300 group-hover:scale-105"
                    >
                        <path fillRule="evenodd" d="M4.5 5.653c0-1.427 1.529-2.33 2.779-1.643l11.54 6.347c1.295.712 1.295 2.573 0 3.286L7.28 19.99c-1.25.687-2.779-.217-2.779-1.643V5.653Z" clipRule="evenodd" />
                    </svg>
                </div>
            </div>

            {/* Label overlay (bottom-left badge) */}
            {label && (
                <div className="absolute bottom-3 left-3 px-2.5 py-1 text-xs font-medium text-white/95 rounded-md bg-black/45 backdrop-blur-sm border border-white/5 transition-all duration-300 group-hover:bg-black/60">
                    {label}
                </div>
            )}
        </div>
    );
}
