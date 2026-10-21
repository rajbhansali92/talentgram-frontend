import React from "react";
import { Navigate } from "react-router-dom";
import { isAdmin } from "@/lib/api";
import { ROUTE_META } from "@/components/workspace/routeMeta";
import Dashboard from "@/pages/Dashboard";
import TalentList from "@/pages/TalentList";
import TalentEdit from "@/pages/TalentEdit";
import ProjectList from "@/pages/ProjectList";
import ProjectEdit from "@/pages/ProjectEdit";
import ProductionOverview from "@/pages/ProductionOverview";
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
import ImportWizard from "@/pages/ImportWizard";
import CastingDesk from "@/pages/CastingDesk";
import SimpleAssistant from "@/pages/SimpleAssistant";

// One page per route entry in ROUTE_META (a test keeps the two lists identical).
const PAGES = {
  "": <Dashboard />,
  "simple-assistant": <SimpleAssistant />,
  "casting-desk": <CastingDesk />,
  "casting-desk/:sessionId": <CastingDesk />,
  talents: <TalentList />,
  "talents/new": <TalentEdit />,
  "talents/:id": <TalentEdit />,
  applications: <Applications />,
  projects: <ProjectList />,
  "projects/new": <ProjectEdit />,
  "projects/:id": <ProjectEdit />,
  "projects/:id/submissions": <SubmissionReviewCenter />,
  production: <ProductionOverview />,
  links: <LinkHistory />,
  "links/new": <LinkGenerator />,
  "links/:id/results": <LinkResults />,
  "links/:id/edit": <LinkGenerator />,
  users: <UserManagement />,
  feedback: <AdminFeedback />,
  marketing: <MarketingHub />,
  notifications: <NotificationsPage />,
  workflow: <WorkflowPage />,
  storage: <StorageDashboard />,
  whatsapp: <WhatsAppEnginePage />,
  diagnostics: <SubmissionDiagnostics />,
  imports: <ImportWizard />,
};

/** Route-level guard for admin-only pages (the sidebar hiding them is only cosmetic): anyone else is sent to the Overview. Read fresh on every render. */
export function RequireAdmin({ children }) {
  return isAdmin() ? children : <Navigate to="/admin" replace />;
}

export const ADMIN_PAGE_ROUTES = ROUTE_META.map((m) => ({
  path: m.path,
  abs: m.abs,
  element: m.adminOnly ? <RequireAdmin>{PAGES[m.path]}</RequireAdmin> : PAGES[m.path],
}));
export const ADMIN_PAGE_KEYS = Object.keys(PAGES);
