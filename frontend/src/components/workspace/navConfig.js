import {
  LayoutDashboard, Users, Link2, Clapperboard, Film, UserPlus, Shield, MessageSquare, Briefcase, AlertCircle, ListTodo, Cloud, MessageCircle,
  FileSpreadsheet, Sparkles, Radar,
} from "lucide-react";
import { SIMPLE_ASSISTANT_ENABLED } from "@/lib/simpleAssistant";

// Centralized navigation — the single source for the sidebar, the mobile drawer and the workspace "Go to" launcher.
export const NAV_ITEMS = {
  base: [
    { to: "/admin", label: "Overview", icon: LayoutDashboard, end: true },
    ...(SIMPLE_ASSISTANT_ENABLED
      ? [{ to: "/admin/simple-assistant", label: "Assistant", icon: Radar }]
      : []),
    { to: "/admin/casting-desk", label: "AI Casting Desk", icon: Sparkles },
    { to: "/admin/talents", label: "Talents", icon: Users },
    { to: "/admin/applications", label: "Applications", icon: UserPlus },
    { to: "/admin/projects", label: "Projects", icon: Clapperboard },
    { to: "/admin/production", label: "Production Desk", icon: Film },
    { to: "/admin/links", label: "Links", icon: Link2 },
    { to: "/admin/marketing", label: "Marketing", icon: Briefcase },
    { to: "/admin/feedback", label: "Feedback", icon: MessageSquare },
    { to: "/admin/workflow", label: "Workflow", icon: ListTodo },
    { to: "/admin/whatsapp", label: "WhatsApp", icon: MessageCircle },
  ],
  adminOnly: [
    { to: "/admin/users", label: "Users", icon: Shield, adminOnly: true },
    { to: "/admin/storage", label: "Storage", icon: Cloud, adminOnly: true },
    { to: "/admin/diagnostics", label: "Diagnostics", icon: AlertCircle, adminOnly: true },
    { to: "/admin/imports", label: "Data Center", icon: FileSpreadsheet, adminOnly: true },
  ],
};
