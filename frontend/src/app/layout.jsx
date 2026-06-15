import "@/index.css";
import "@/App.css";
import { Toaster } from "sonner";

import { headers } from "next/headers";

/**
 * viewport-fit=cover is REQUIRED for env(safe-area-inset-*) to work on
 * iOS Safari (notch, Dynamic Island, home-indicator bar). Without it the
 * browser clips content to the safe area rectangle and ignores the env().
 */
export function generateViewport() {
  return {
    width: "device-width",
    initialScale: 1,
    viewportFit: "cover",
  };
}

export async function generateMetadata() {
  const headersList = await headers();
  const host = headersList.get("host") || "talentgramagency.com";
  const domain = host.replace(/^www\./, "");

  let title = "Talentgram Agency";
  let description = "India - UAE";
  const baseSiteUrl = `https://${domain}`;
  const versionString = `v=${Date.now()}`;
  let ogImageUrl = `https://talentgramagency.com/og-image?${versionString}`;

  if (domain.startsWith("apply.")) {
    title = "Talentgram Agency — Apply Portal";
    description = "India - UAE";
    ogImageUrl = `https://talentgramagency.com/og-image?portal=apply&${versionString}`;
  } else if (domain.startsWith("review.")) {
    title = "Talentgram Agency — Review Centre";
    description = "Secure access portal for casting directors and clients to review, shortlist, and approve talent portfolios.";
    ogImageUrl = `https://talentgramagency.com/og-image?portal=review&${versionString}`;
  } else if (domain.startsWith("submit.")) {
    title = "Talentgram Agency — Submission Portal";
    description = "Submit your video auditions, polaroids, and details for active casting projects.";
  } else if (domain.startsWith("links.")) {
    title = "Talentgram Agency — Portfolios";
    description = "Explore premium artist headshots, intro videos, and work reels.";
  }

  const { sanitizeMetadata } = require("@/lib/sanitize");
  const cleanTitle = sanitizeMetadata(title);
  const cleanDescription = sanitizeMetadata(description);

  return {
    title: cleanTitle,
    description: cleanDescription,
    metadataBase: new URL(baseSiteUrl),
    alternates: {
      canonical: "/",
    },
    openGraph: {
      title: cleanTitle,
      description: cleanDescription,
      url: baseSiteUrl,
      siteName: "Talentgram",
      type: "website",
      images: [
        {
          url: ogImageUrl,
          width: 1200,
          height: 630,
          alt: cleanTitle,
        }
      ],
    },
    manifest: '/site.webmanifest',
    icons: {
      icon: '/favicon.ico',
      apple: '/apple-touch-icon.png',
    },
    twitter: {
      card: "summary_large_image",
      title: cleanTitle,
      description: cleanDescription,
      images: [ogImageUrl],
    }
  };
}

import { UploadManagerProvider } from "@/context/UploadManagerContext";

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;500;600;700&display=swap" rel="stylesheet" />
      </head>
      <body className="tg-grain">
        <Toaster
          theme="dark"
          position="top-center"
          offset={16}
          mobileOffset={64}
          toastOptions={{
            style: {
              background: "#0c0c0c",
              border: "1px solid rgba(255,255,255,0.12)",
              color: "#f5f5f0",
              fontFamily: "Manrope, sans-serif",
            },
          }}
        />
        <UploadManagerProvider>
          {children}
        </UploadManagerProvider>
      </body>
    </html>
  );
}
