import React from "react";
import TalentMediaPage from "@/pages-components/TalentMediaPage";

// Private, token-gated page — never indexed or previewed with talent details.
export const metadata = {
  title: "Talentgram — Talent Media",
  description: "Talentgram",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default function Page() {
  return <TalentMediaPage />;
}
