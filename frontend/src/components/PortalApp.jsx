import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import DashboardLayout from "@/components/DashboardLayout";
import PortalGateway from "@/pages/PortalGateway";
import PortalHome from "@/pages/PortalHome";
import PortalProjects from "@/pages/PortalProjects";
import ProjectDetail from "@/pages/ProjectDetail";
import PortalProfile from "@/pages/PortalProfile";
import PortalSettings from "@/pages/PortalSettings";

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

                {/* Permanent Dashboard shell (Phase 2 item 1) — DashboardLayout
                    stays mounted across these routes, only <Outlet/> content
                    changes. PortalHome/PortalProfile are the same existing
                    components, just wrapped in the shared shell instead of
                    rendering their own header. */}
                <Route element={<DashboardLayout />}>
                    <Route path="/portal/home" element={<PortalHome />} />
                    <Route path="/portal/projects" element={<PortalProjects />} />
                    {/* Project Detail (Phase 2 item 4) — two path segments,
                        so this never collides with the single-segment
                        /portal/:slug login-gateway route above. */}
                    <Route path="/portal/projects/:slug" element={<ProjectDetail />} />
                    <Route path="/portal/profile" element={<PortalProfile />} />
                    <Route path="/portal/settings" element={<PortalSettings />} />
                </Route>

                <Route path="*" element={<Navigate to="/portal/home" replace />} />
            </Routes>
        </BrowserRouter>
    );
}
