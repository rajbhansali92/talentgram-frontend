import React from "react";
import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Toaster } from "sonner";
import AdminLogin from "@/pages/AdminLogin";
import AdminLayout from "@/components/AdminLayout";
import Dashboard from "@/pages/Dashboard";
import TalentList from "@/pages/TalentList";
import TalentEdit from "@/pages/TalentEdit";
import ProjectList from "@/pages/ProjectList";
import ProjectEdit from "@/pages/ProjectEdit";
import LinkHistory from "@/pages/LinkHistory";
import LinkGenerator from "@/pages/LinkGenerator";
import LinkResults from "@/pages/LinkResults";
import ClientView from "@/pages/ClientView";
import SubmissionPage from "@/pages/SubmissionPage";
import ApplicationPage from "@/pages/ApplicationPage";
import Applications from "@/pages/Applications";
import UserManagement from "@/pages/UserManagement";
import SignupPage from "@/pages/SignupPage";
import Landing from "@/pages/Landing";
import ProtectedRoute from "@/components/ProtectedRoute";

function App() {
    return (
        <div className="App tg-grain">
            <Toaster
                theme="dark"
                position="top-right"
                toastOptions={{
                    style: {
                        background: "#0c0c0c",
                        border: "1px solid rgba(255,255,255,0.12)",
                        color: "#f5f5f0",
                        fontFamily: "Manrope, sans-serif",
                    },
                }}
            />
            <BrowserRouter>
                <Routes>
                    <Route path="/" element={<Landing />} />
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
                        <Route path="links" element={<LinkHistory />} />
                        <Route path="links/new" element={<LinkGenerator />} />
                        <Route
                            path="links/:id/results"
                            element={<LinkResults />}
                        />
                        <Route
                            path="links/:id/edit"
                            element={<LinkGenerator />}
                        />
                        <Route path="users" element={<UserManagement />} />
                    </Route>
                    <Route path="/l/:slug" element={<ClientView />} />
                    <Route path="/submit/:slug" element={<SubmissionPage />} />
                    <Route path="/apply" element={<ApplicationPage />} />
                    <Route path="/signup" element={<SignupPage />} />
                    <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
            </BrowserRouter>
        </div>
    );
}

export default App;
