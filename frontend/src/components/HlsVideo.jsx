import React, { useEffect, useRef } from "react";

/**
 * Drop-in replacement for a plain <video> that also plays Cloudflare Stream
 * HLS (.m3u8) sources on browsers without native HLS (Chrome, Firefox).
 *
 * - Safari / iOS: native HLS — the URL is assigned directly to `src`.
 * - Others with an .m3u8 URL: hls.js is lazy-loaded and attached.
 * - Progressive MP4 (e.g. an R2 raw preview while a clip is still
 *   transcoding, or a legacy Cloudinary URL): assigned directly to `src`.
 *
 * All other props (poster, controls, className, …) are forwarded verbatim,
 * so existing markup/styling is preserved.
 */
export default function HlsVideo({ src, ...rest }) {
    const videoRef = useRef(null);

    useEffect(() => {
        const video = videoRef.current;
        if (!video) return;
        
        console.log("[HlsVideo] Mounted / updated with src:", src);
        if (!src) return;

        let hls = null;
        const isHls = src.includes(".m3u8");
        console.log("[HlsVideo] isHls check:", isHls, "canPlayType native HLS:", video.canPlayType("application/vnd.apple.mpegurl"));

        if (isHls && !video.canPlayType("application/vnd.apple.mpegurl")) {
            let cancelled = false;
            console.log("[HlsVideo] Loading hls.js library dynamically...");
            import("hls.js")
                .then(({ default: Hls }) => {
                    if (cancelled || !videoRef.current) return;
                    if (Hls.isSupported()) {
                        console.log("[HlsVideo] hls.js is supported. Instantiating player...");
                        hls = new Hls();
                        
                        // Attach diagnostics listeners
                        hls.on(Hls.Events.MANIFEST_PARSED, (event, data) => {
                            console.log("[HlsVideo] EVENT: MANIFEST_PARSED", data);
                            if (rest.autoPlay) {
                                console.log("[HlsVideo] autoPlay is true, triggering play() post-manifest-parse");
                                videoRef.current.play().catch(err => {
                                    console.warn("[HlsVideo] Play failed post-manifest-parse:", err);
                                });
                            }
                        });
                        hls.on(Hls.Events.LEVEL_LOADED, (event, data) => {
                            console.log("[HlsVideo] EVENT: LEVEL_LOADED", data);
                        });
                        hls.on(Hls.Events.ERROR, (event, data) => {
                            console.error("[HlsVideo] EVENT: ERROR", data);
                        });

                        hls.loadSource(src);
                        hls.attachMedia(videoRef.current);
                    } else {
                        console.warn("[HlsVideo] hls.js is not supported, falling back to native src");
                        // No MSE support — last-resort direct assignment.
                        videoRef.current.src = src;
                    }
                })
                .catch((err) => {
                    console.error("[HlsVideo] Failed to load hls.js library:", err);
                    if (videoRef.current) videoRef.current.src = src;
                });
            return () => {
                cancelled = true;
                if (hls) {
                    console.log("[HlsVideo] Cleaning up hls instance");
                    hls.destroy();
                }
            };
        }

        // Native HLS or progressive MP4.
        console.log("[HlsVideo] Assigning native src directly:", src);
        video.src = src;
        return () => {
            if (hls) {
                console.log("[HlsVideo] Cleaning up hls instance");
                hls.destroy();
            }
        };
    }, [src]);

    return <video ref={videoRef} {...rest} />;
}
