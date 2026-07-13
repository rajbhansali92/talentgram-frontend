import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import ProtectedRoute from "@/components/ProtectedRoute";
import AdminLayout from "@/components/AdminLayout";
import AdminLogin from "@/pages/AdminLogin";
import Dashboard from "@/pages/Dashboard";
import TalentList from "@/pages/TalentList";
import TalentEdit from "@/pages/TalentEdit";
import ProjectList from "@/pages/ProjectList";
import ProjectEdit from "@/pages/ProjectEdit";
import SubmissionReviewCenter from "@/pages/SubmissionReviewCenter";
import LinkHistory from "@/pages/LinkHistory";
import LinkGenerator from "@/pages/LinkGenerator";
import LinkResults from "@/pages/LinkResults";
import Applications from "@/pages/Applications";
import UserManagement from "@/pages/UserManagement";
import AdminFeedback from "@/pages/AdminFeedback";
import MarketingHub from "@/pages/MarketingHub";
import NotificationsPage from "@/pages/NotificationsPage";
import WorkflowPage from "@/pages/WorkflowPage";
import StorageDashboard from "@/pages/StorageDashboard";
import WhatsAppEnginePage from "@/pages/WhatsAppEnginePage";
import SubmissionDiagnostics from "@/pages/SubmissionDiagnostics";


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
                    <Route index element={<Dashboard />} />
                    <Route path="talents" element={<TalentList />} />
                    <Route path="talents/new" element={<TalentEdit />} />
                    <Route path="talents/:id" element={<TalentEdit />} />
                    <Route path="applications" element={<Applications />} />
                    <Route path="projects" element={<ProjectList />} />
                    <Route path="projects/new" element={<ProjectEdit />} />
                    <Route path="projects/:id" element={<ProjectEdit />} />
                    <Route path="projects/:id/submissions" element={<SubmissionReviewCenter />} />
                    <Route path="links" element={<LinkHistory />} />
                    <Route path="links/new" element={<LinkGenerator />} />
                    <Route path="links/:id/results" element={<LinkResults />} />
                    <Route path="links/:id/edit" element={<LinkGenerator />} />
                    <Route path="users" element={<UserManagement />} />
                    <Route path="feedback" element={<AdminFeedback />} />
                    <Route path="marketing" element={<MarketingHub />} />
                    <Route path="notifications" element={<NotificationsPage />} />
                    <Route path="workflow" element={<WorkflowPage />} />
                    <Route path="storage" element={<StorageDashboard />} />
                    <Route path="whatsapp" element={<WhatsAppEnginePage />} />
                    <Route path="diagnostics" element={<SubmissionDiagnostics />} />
                </Route>
                <Route path="*" element={<Navigate to="/admin" replace />} />
            </Routes>
        </BrowserRouter>
    );
}
