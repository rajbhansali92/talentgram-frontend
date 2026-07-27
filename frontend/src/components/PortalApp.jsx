import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import PortalGateway from "@/pages/PortalGateway";
import PortalHome from "@/pages/PortalHome";
import PortalProfile from "@/pages/PortalProfile";

export default function PortalApp() {
    return (
        <BrowserRouter>
            <Routes>
                {/* Standalone Dashboard entry — no project slug required.
                    Reuses PortalGateway itself (same OTP/Google auth), not a
                    new component; static route ranks above /portal/:slug in
                    react-router v6's matcher regardless of declaration order. */}
                <Route path="/portal/login" element={<PortalGateway />} />
                <Route path="/portal/:slug" element={<PortalGateway />} />
                <Route path="/portal/home" element={<PortalHome />} />
                <Route path="/portal/profile" element={<PortalProfile />} />
                <Route path="*" element={<Navigate to="/portal/home" replace />} />
            </Routes>
        </BrowserRouter>
    );
}
