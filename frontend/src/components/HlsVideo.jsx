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
        if (!video || !src) return;

        let hls = null;
        const isHls = src.includes(".m3u8");

        if (isHls && !video.canPlayType("application/vnd.apple.mpegurl")) {
            let cancelled = false;
            import("hls.js")
                .then(({ default: Hls }) => {
                    if (cancelled || !videoRef.current) return;
                    if (Hls.isSupported()) {
                        hls = new Hls();
                        hls.loadSource(src);
                        hls.attachMedia(videoRef.current);
                    } else {
                        // No MSE support — last-resort direct assignment.
                        videoRef.current.src = src;
                    }
                })
                .catch(() => {
                    if (videoRef.current) videoRef.current.src = src;
                });
            return () => {
                cancelled = true;
                if (hls) hls.destroy();
            };
        }

        // Native HLS or progressive MP4.
        video.src = src;
        return () => {
            if (hls) hls.destroy();
        };
    }, [src]);

    return <video ref={videoRef} {...rest} />;
}
