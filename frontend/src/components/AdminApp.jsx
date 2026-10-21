import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import ProtectedRoute from "@/components/ProtectedRoute";
import AdminLayout from "@/components/AdminLayout";
import AdminLogin from "@/pages/AdminLogin";
import { ADMIN_PAGE_ROUTES } from "@/components/workspace/adminRoutes";


export default function AdminApp() {
    return (
        <BrowserRouter>
            <Routes>
                <Route path="/admin/login" element={<AdminLogin />} />
                <Route
                    path="/admin"
                    element={
                        <ProtectedRoute>
                            <AdminLayout />
                        </ProtectedRoute>
                    }
                >
                    {/* Every admin URL and the page it serves. AdminLayout renders the pages itself, one live pane per open tab (see
                        workspace/WorkspaceHost, which reads the same ADMIN_PAGE_ROUTES), so these elements are what a bare <Outlet /> layout would show. */}
                    {ADMIN_PAGE_ROUTES.map((r) => (r.path ? <Route key={r.path} path={r.path} element={r.element} /> : <Route key="index" index element={r.element} />))}
                </Route>
                <Route path="*" element={<Navigate to="/admin" replace />} />
            </Routes>
        </BrowserRouter>
    );
}
