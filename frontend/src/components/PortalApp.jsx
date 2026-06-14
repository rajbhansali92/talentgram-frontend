import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import PortalGateway from "@/pages/PortalGateway";
import PortalHome from "@/pages/PortalHome";
import PortalProfile from "@/pages/PortalProfile";

export default function PortalApp() {
    return (
        <BrowserRouter>
            <Routes>
                <Route path="/portal/:slug" element={<PortalGateway />} />
                <Route path="/portal/home" element={<PortalHome />} />
                <Route path="/portal/profile" element={<PortalProfile />} />
                <Route path="*" element={<Navigate to="/portal/home" replace />} />
            </Routes>
        </BrowserRouter>
    );
}
