import "@/index.css";
import "@/App.css";
import { Toaster } from "sonner";

import { headers } from "next/headers";

export async function generateMetadata() {
  const headersList = await headers();
  const host = headersList.get("host") || "talentgramagency.com";
  const domain = host.replace(/^www\./, "");

  let title = "Talentgram | Premium Casting & Talent Platform";
  let description = "Scout, manage, submit, and review professional talent portfolios across India & UAE.";
  const baseSiteUrl = `https://${domain}`;
  const versionString = `v=${Date.now()}`;
  let ogImageUrl = `https://talentgramagency.com/og-image?${versionString}`;

  if (domain.startsWith("apply.")) {
    title = "Apply to Join Talentgram | Premium Talent Network";
    description = "Apply to join our exclusive casting database. Scout. Manage. Place. Audition for premium projects in India and UAE.";
    ogImageUrl = `https://talentgramagency.com/og-image?portal=apply&${versionString}`;
  } else if (domain.startsWith("review.")) {
    title = "Talentgram Review Centre";
    description = "Secure access portal for casting directors and clients to review, shortlist, and approve talent portfolios.";
    ogImageUrl = `https://talentgramagency.com/og-image?portal=review&${versionString}`;
  } else if (domain.startsWith("submit.")) {
    title = "Talentgram Audition Submission Portal";
    description = "Submit your video auditions, polaroids, and details for active casting projects.";
  } else if (domain.startsWith("links.")) {
    title = "Talentgram Portfolios";
    description = "Explore premium artist headshots, intro videos, and work reels.";
  }

  return {
    title,
    description,
    metadataBase: new URL(baseSiteUrl),
    alternates: {
      canonical: "/",
    },
    openGraph: {
      title,
      description,
      url: baseSiteUrl,
      siteName: "Talentgram",
      type: "website",
      images: [
        {
          url: ogImageUrl,
          width: 1200,
          height: 630,
          alt: title,
        }
      ],
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: [ogImageUrl],
    }
  };
}

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
        {children}
      </body>
    </html>
  );
}
