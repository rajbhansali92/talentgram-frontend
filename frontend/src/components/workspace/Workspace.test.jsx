import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, act, within, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useNavigate, useLocation } from "react-router-dom";

// ------------------------------------------------------------------------------------------------------------------------------ mocks
vi.mock("@/lib/api", () => ({
  getAdmin: () => globalThis.__admin,
  isAdmin: () => globalThis.__admin?.role === "admin",
  clearAdminSession: vi.fn(),
  adminApi: { get: vi.fn(), post: vi.fn() },
}));
vi.mock("@/components/Logo", () => ({ default: () => null }));
vi.mock("@/components/NotificationBell", () => ({ default: () => null }));
vi.mock("@/components/ActionQueuePanel", () => ({ default: () => null }));

// Stub pages: each has its own state, a window key handler, publishes a selection to the assistant bridge, and can report unsaved work / a title.
vi.mock("@/components/workspace/adminRoutes", async () => {
  const React = await import("react");
  const RR = await import("react-router-dom");
  const WC = await import("@/components/workspace/WorkspaceContext");
  const AC = await import("@/lib/assistantContext");
  const { ROUTE_META } = await import("@/components/workspace/routeMeta");
  function Reporter({ dirty }) { WC.useWorkspaceDirty(dirty); return null; }
  function Stub({ name }) {
    const [draft, setDraft] = React.useState("");
    const [keys, setKeys] = React.useState(0);
    const loc = RR.useLocation();
    const params = RR.useParams();
    const ids = React.useMemo(() => [`t-${name}`], [name]);
    globalThis.__ws = WC.useWorkspace();
    if (globalThis.__crash === name) throw new Error("boom");
    React.useEffect(() => { const h = () => setKeys((k) => k + 1); window.addEventListener("keydown", h); return () => window.removeEventListener("keydown", h); }, []);
    AC.usePublishAssistantContext({ projectId: name, talentIds: ids });
    WC.useTabTitle(name === "projects/:id" ? "MK Jewels" : null);
    const h = React.createElement;
    const reports = name === "talents/:id" || name === "projects/:id";                  // other pages never report anything (like most real pages)
    return h("div", { "data-testid": `page-${name}` },
      reports ? h(Reporter, { dirty: !!draft && name === "talents/:id" }) : null,
      h("span", { "data-testid": `loc-${name}` }, loc.pathname + loc.search),
      h("span", { "data-testid": `params-${name}` }, JSON.stringify(params)),
      h("input", { "aria-label": `draft ${name}`, value: draft, onChange: (e) => setDraft(e.target.value) }),
      h("span", { "data-testid": `keys-${name}` }, String(keys)),
      h(RR.Link, { to: "/admin/projects/p9", "data-testid": `link-${name}` }, "go to project"),
      h("div", { "data-testid": `scroller-${name}`, style: { overflow: "auto", height: 40 } }, "scroll area"));
  }
  const ADMIN_PAGE_ROUTES = ROUTE_META.map((m) => ({ path: m.path, abs: m.abs, element: React.createElement(Stub, { name: m.path || "overview" }) }));
  return { ADMIN_PAGE_ROUTES, ADMIN_PAGE_KEYS: ROUTE_META.map((m) => m.path), RequireAdmin: ({ children }) => children };
});

if (typeof window.ResizeObserver === "undefined") window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
if (typeof window.matchMedia !== "function") window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
if (typeof window.localStorage === "undefined" || typeof window.localStorage.getItem !== "function") {
  const store = {};
  Object.defineProperty(window, "localStorage", { value: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; }, clear: () => { Object.keys(store).forEach((k) => delete store[k]); } }, configurable: true });
}

import { adminApi } from "@/lib/api";
import AdminLayout from "@/components/AdminLayout";
import { getAssistantContext, setAssistantContext } from "@/lib/assistantContext";
import { NAV_ITEMS } from "@/components/workspace/navConfig";
import { ROUTE_META, isPermittedPath, matchRoute, normalizePathname, tabTitleFor } from "@/components/workspace/routeMeta";
import { clearSaved, destinationsFor, filterDestinations, fullPath, parseTo, readSaved, samePath, writeSaved } from "@/components/workspace/paths";

// ------------------------------------------------------------------------------------------------------------------------------ harness
function Probe() {
  globalThis.__nav = useNavigate();
  globalThis.__loc = useLocation();
  return null;
}
function renderApp(path = "/admin/talents", admin = { id: "u1", role: "admin", name: "A", email: "a@x.com" }) {
  globalThis.__admin = admin;
  return render(
    <MemoryRouter initialEntries={Array.isArray(path) ? path : [path]} initialIndex={Array.isArray(path) ? path.length - 1 : 0}>
      <Routes>
        <Route path="/admin" element={<AdminLayout />}>
          <Route index element={null} />
          <Route path="*" element={null} />
        </Route>
      </Routes>
      <Probe />
    </MemoryRouter>,
  );
}
const tabs = () => screen.getAllByRole("tab").map((t) => t.textContent);
const activeTab = () => screen.getAllByRole("tab").find((t) => t.getAttribute("aria-selected") === "true")?.textContent;
const sidebarLink = (name) => within(screen.getByTestId("admin-sidebar")).getByText(name).closest("a");
const url = () => globalThis.__loc.pathname + globalThis.__loc.search;
const paneOf = (testid) => screen.getByTestId(testid).closest("[data-tab-pane]");
const hidden = (testid) => getComputedStyle(paneOf(testid)).display === "none";
const draft = (name) => screen.getByLabelText(`draft ${name}`);
const openTab = (to) => act(() => { globalThis.__ws.open(to); });
const goTo = async () => { fireEvent.click(await screen.findByTestId("goto-launcher")); return screen.findByTestId("goto-panel"); };

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear();
  adminApi.get.mockReset(); adminApi.post.mockReset();
  adminApi.get.mockImplementation((u) => (u === "/personal-assistant/access" ? Promise.resolve({ data: { allowed: true } }) : u === "/projects" ? Promise.resolve({ data: [{ id: "p1", brand_name: "MK Jewels - film 1" }, { id: "p2", brand_name: "Other Film" }] }) : Promise.resolve({ data: [] })));
  globalThis.__crash = null;
  setAssistantContext({ projectId: null, talentIds: [] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

// ================================================================================================================ opening and activating
describe("opening pages as tabs", () => {
  it("the page you are on is a tab with a readable title; the app is never without one", () => {
    renderApp("/admin/talents");
    expect(tabs()).toEqual(["Talents"]);
    expect(activeTab()).toBe("Talents");
    expect(screen.getByTestId("page-talents")).toBeTruthy();
    expect(screen.getByRole("tablist").getAttribute("aria-label")).toBe("Open pages");
  });

  it("a sidebar link opens its page in a NEW tab and activates it — the previous page stays alive but hidden", () => {
    renderApp("/admin/talents");
    fireEvent.click(sidebarLink("Projects"));
    expect(tabs()).toEqual(["Talents", "Projects"]);
    expect(activeTab()).toBe("Projects");
    expect(url()).toBe("/admin/projects");
    expect(hidden("page-talents")).toBe(true);                                   // still in the DOM …
    expect(hidden("page-projects")).toBe(false);                                 // … only the active one is shown
    expect(within(screen.getByTestId("admin-sidebar")).getByText("Projects").closest("a").getAttribute("aria-current")).toBe("page");
  });

  it("opening a page that is already open activates its tab instead of adding another", () => {
    renderApp("/admin/talents");
    fireEvent.click(sidebarLink("Projects"));
    fireEvent.click(sidebarLink("Talents"));
    expect(tabs()).toEqual(["Talents", "Projects"]);
    expect(activeTab()).toBe("Talents");
    fireEvent.click(sidebarLink("Projects"));
    fireEvent.click(sidebarLink("Projects"));
    expect(tabs()).toEqual(["Talents", "Projects"]);
    expect(activeTab()).toBe("Projects");
  });

  it("a ctrl / cmd click on a sidebar link is left to the browser (no tab is added by it)", () => {
    renderApp("/admin/talents");
    fireEvent.click(sidebarLink("Projects"), { ctrlKey: true });
    fireEvent.click(sidebarLink("Links"), { metaKey: true });
    expect(tabs()).toEqual(["Talents"]);
    sidebarLink("Projects").getAttribute("href") === "/admin/projects" || (() => { throw new Error("href lost"); })();
  });

  it("every sidebar link marks itself for the pages' unsaved-changes guards (it opens a tab, it does not leave the page)", () => {
    renderApp("/admin/talents");
    const anchors = [...screen.getByTestId("admin-sidebar").querySelectorAll("a")];
    expect(anchors.length).toBeGreaterThan(10);
    anchors.forEach((a) => expect(a.hasAttribute("data-workspace-nav")).toBe(true));
  });

  it("each tab keeps its own route, query string and params; links inside a page navigate that tab only", () => {
    renderApp("/admin/talents");
    openTab("/admin/talents/abc?x=1");                                          // a different route of the same area is its own tab
    expect(tabs()).toEqual(["Talents", "Talent"]);
    expect(screen.getByTestId("loc-talents/:id").textContent).toBe("/admin/talents/abc?x=1");
    expect(screen.getByTestId("params-talents/:id").textContent).toBe('{"id":"abc"}');
    expect(screen.getByTestId("loc-talents").textContent).toBe("/admin/talents");   // the other tab still describes ITS location
    fireEvent.click(screen.getByTestId("link-talents/:id"));                     // an in-page link: same tab, new page inside it
    expect(tabs()).toEqual(["Talents", "MK Jewels"]);                                // the projects page names its tab
    expect(activeTab()).toBe("MK Jewels");
    expect(url()).toBe("/admin/projects/p9");
  });
});

// ================================================================================================================ the launcher
describe("Go to", () => {
  it("sits beside the assistant's button and opens a compact, labelled, searchable list", async () => {
    renderApp("/admin/talents");
    const btn = await screen.findByTestId("goto-launcher");
    expect(btn.getAttribute("aria-label")).toBe("Go to");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    const assistant = await screen.findByTestId("pa-launcher");
    expect(btn.className).toMatch(/right-\[4\.5rem\]/);                           // just left of the assistant's right-4 / sm:right-6 button …
    expect(assistant.className).toMatch(/right-4/);
    expect(btn.className.match(/bottom-\[[^\]]+\]/)[0]).toBe(assistant.className.match(/bottom-\[[^\]]+\]/)[0]);      // … on the same line
    const panel = await goTo();
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(panel.getAttribute("role")).toBe("dialog");
    expect(within(panel).getByRole("combobox", { name: "Search pages" })).toBeTruthy();
    expect(within(panel).getByRole("listbox", { name: "Pages" })).toBeTruthy();
    expect(panel.className).toMatch(/w-\[min\(22rem,calc\(100vw-2rem\)\)\]/);   // never wider than the screen
  });

  it("choosing a page opens it in a new IN-APP tab, closes the launcher, and never opens a browser window", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    renderApp("/admin/talents");
    const panel = await goTo();
    fireEvent.click(within(panel).getByText("Links"));
    expect(tabs()).toEqual(["Talents", "Links"]);
    expect(activeTab()).toBe("Links");
    expect(screen.queryByTestId("goto-panel")).toBeNull();
    expect(open).not.toHaveBeenCalled();
    expect(url()).toBe("/admin/links");
  });

  it("search narrows the list; Enter opens the highlighted page; ↑ ↓ move; Escape closes and returns focus", async () => {
    renderApp("/admin/talents");
    await goTo();
    const input = screen.getByTestId("goto-search");
    fireEvent.change(input, { target: { value: "generate" } });
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Generate Link"]);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(tabs()).toEqual(["Talents", "Generate Link"]);
    expect(url()).toBe("/admin/links/new");

    await goTo();
    const input2 = screen.getByTestId("goto-search");
    fireEvent.change(input2, { target: { value: "global talent" } });
    expect(screen.getAllByRole("option")[0].textContent).toBe("Talents");
    fireEvent.change(input2, { target: { value: "p" } });
    const before = screen.getAllByRole("option").findIndex((o) => o.getAttribute("aria-selected") === "true");
    fireEvent.keyDown(input2, { key: "ArrowDown" });
    expect(screen.getAllByRole("option").findIndex((o) => o.getAttribute("aria-selected") === "true")).toBe(before + 1);
    fireEvent.keyDown(input2, { key: "ArrowUp" });
    expect(screen.getAllByRole("option").findIndex((o) => o.getAttribute("aria-selected") === "true")).toBe(before);
    fireEvent.change(input2, { target: { value: "zzzzz" } });
    expect(screen.getByText(/No page matches/)).toBeTruthy();
    fireEvent.keyDown(input2, { key: "Escape" });
    expect(screen.queryByTestId("goto-panel")).toBeNull();
    expect(document.activeElement).toBe(screen.getByTestId("goto-launcher"));
    expect(tabs()).toEqual(["Talents", "Generate Link"]);                         // Escape opened nothing
  });

  it("choosing an already-open page activates its tab", async () => {
    renderApp("/admin/talents");
    fireEvent.click(sidebarLink("Projects"));
    const panel = await goTo();
    fireEvent.click(within(panel).getByText("Talents"));
    expect(tabs()).toEqual(["Talents", "Projects"]);
    expect(activeTab()).toBe("Talents");
  });

  it("typing a project name also offers its Pipeline and Submission Review (project names are fetched only then)", async () => {
    renderApp("/admin/talents");
    await goTo();
    expect(adminApi.get).not.toHaveBeenCalledWith("/projects");
    fireEvent.change(screen.getByTestId("goto-search"), { target: { value: "mk jew" } });
    await waitFor(() => expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["MK Jewels - film 1 — Pipeline", "MK Jewels - film 1 — Submission Review"]));
    fireEvent.click(screen.getByText("MK Jewels - film 1 — Submission Review"));
    expect(url()).toBe("/admin/projects/p1/submissions");
    expect(activeTab()).toBe("Submission Review");
  });

  it("a team member is never offered an admin-only page; an admin is", async () => {
    renderApp("/admin/talents", { id: "u2", role: "team" });
    await goTo();
    const names = screen.getAllByRole("option").map((o) => o.textContent);
    ["Users", "Storage", "Diagnostics", "Data Center"].forEach((n) => expect(names).not.toContain(n));
    expect(names).toEqual(expect.arrayContaining(["Talents", "Projects", "Generate Link"]));
    cleanup();
    renderApp("/admin/talents", { id: "u1", role: "admin" });
    await goTo();
    const adminNames = screen.getAllByRole("option").map((o) => o.textContent);
    ["Users", "Storage", "Diagnostics", "Data Center"].forEach((n) => expect(adminNames).toContain(n));
  });

  it("steps aside while the assistant panel is open, and the assistant keeps working (task centre reachable)", async () => {
    renderApp("/admin/talents");
    fireEvent.click(await screen.findByTestId("pa-launcher"));
    await screen.findByTestId("pa-panel");
    expect(screen.queryByTestId("goto-launcher")).toBeNull();
    expect(screen.getByTestId("pa-tab-tasks")).toBeTruthy();
  });
});

// ================================================================================================================ closing
describe("closing tabs", () => {
  const three = () => { renderApp("/admin/talents"); openTab("/admin/projects"); openTab("/admin/links"); };

  it("closing an inactive tab leaves the active page (and its work) untouched", () => {
    three();
    fireEvent.change(draft("links"), { target: { value: "keep me" } });
    fireEvent.click(screen.getByLabelText("Close Talents"));
    expect(tabs()).toEqual(["Projects", "Links"]);
    expect(activeTab()).toBe("Links");
    expect(url()).toBe("/admin/links");
    expect(draft("links").value).toBe("keep me");
    expect(screen.queryByTestId("page-talents")).toBeNull();                       // the closed page is gone for good
  });

  it("closing the active tab selects its right neighbour, else the one on its left", () => {
    three();
    fireEvent.click(screen.getAllByRole("tab")[1]);                                // Projects (middle)
    fireEvent.click(screen.getByLabelText("Close Projects"));
    expect(activeTab()).toBe("Links");
    expect(url()).toBe("/admin/links");
    fireEvent.click(screen.getByLabelText("Close Links"));                         // the last one: falls back to its left
    expect(tabs()).toEqual(["Talents"]);
    expect(activeTab()).toBe("Talents");
    expect(url()).toBe("/admin/talents");
  });

  it("closing the last tab opens a safe default page — never a blank app", () => {
    renderApp("/admin/talents");
    fireEvent.click(screen.getByLabelText("Close Talents"));
    expect(tabs()).toEqual(["Overview"]);
    expect(url()).toBe("/admin");
    expect(screen.getByTestId("page-overview")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Close Overview"));                      // and again
    expect(tabs()).toEqual(["Overview"]);
    expect(screen.getByTestId("page-overview")).toBeTruthy();
  });

  it("the close button is separate from the tab: closing never activates, activating never closes", () => {
    three();
    const close = screen.getByLabelText("Close Projects");
    expect(close.closest('[role="tab"]')).toBeNull();                              // not nested inside the tab button
    fireEvent.click(close);
    expect(activeTab()).toBe("Links");                                             // the tab behind it did not become active first
    expect(tabs()).toEqual(["Talents", "Links"]);
  });

  it("middle-click closes a tab", () => {
    three();
    fireEvent(screen.getByTestId(`ws-tab-${globalThis.__ws.tabs[0].id}`), new MouseEvent("auxclick", { bubbles: true, cancelable: true, button: 1 }));
    expect(tabs()).toEqual(["Projects", "Links"]);
  });

  it("keyboard: arrows / Home / End move between tabs, Enter activates, Delete closes", () => {
    three();
    const [first, second] = screen.getAllByRole("tab");
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(document.activeElement).toBe(second);
    fireEvent.keyDown(second, { key: "End" });
    expect(document.activeElement).toBe(screen.getAllByRole("tab")[2]);
    fireEvent.keyDown(document.activeElement, { key: "Home" });
    expect(document.activeElement).toBe(first);
    fireEvent.click(second);
    expect(activeTab()).toBe("Projects");
    fireEvent.keyDown(second, { key: "Delete" });
    expect(tabs()).toEqual(["Talents", "Links"]);
    screen.getAllByRole("tab").forEach((t) => expect(["0", "-1"]).toContain(t.getAttribute("tabindex")));      // roving tab stop
  });
});

// ================================================================================================================ preserving work
describe("work is preserved while you switch", () => {
  it("drafts in different tabs survive switching back and forth", () => {
    renderApp("/admin/talents");
    fireEvent.change(draft("talents"), { target: { value: "priya" } });
    fireEvent.click(sidebarLink("Projects"));
    fireEvent.change(draft("projects"), { target: { value: "film 1" } });
    fireEvent.click(sidebarLink("Talents"));
    expect(draft("talents").value).toBe("priya");
    fireEvent.click(sidebarLink("Projects"));
    expect(draft("projects").value).toBe("film 1");
    fireEvent.click(sidebarLink("Talents"));
    expect(draft("talents").value).toBe("priya");
  });

  it("a hidden page's effects are paused: it does not answer keystrokes meant for the visible page, and resumes when shown", () => {
    renderApp("/admin/talents");
    fireEvent.click(sidebarLink("Projects"));
    fireEvent.keyDown(window, { key: "a" });
    fireEvent.keyDown(window, { key: "b" });
    expect(screen.getByTestId("keys-projects").textContent).toBe("2");
    fireEvent.click(sidebarLink("Talents"));
    expect(screen.getByTestId("keys-talents").textContent).toBe("0");              // it heard nothing while hidden
    fireEvent.keyDown(window, { key: "c" });
    expect(screen.getByTestId("keys-talents").textContent).toBe("1");
    expect(screen.getByTestId("keys-projects").textContent).toBe("2");             // and the page now hidden does not hear it
  });

  it("the assistant only ever sees the VISIBLE tab's selection / project", () => {
    renderApp("/admin/talents");
    expect(getAssistantContext()).toMatchObject({ projectId: "talents", talentIds: ["t-talents"] });
    fireEvent.click(sidebarLink("Projects"));
    expect(getAssistantContext()).toMatchObject({ projectId: "projects", talentIds: ["t-projects"] });
    fireEvent.click(sidebarLink("Talents"));
    expect(getAssistantContext()).toMatchObject({ projectId: "talents", talentIds: ["t-talents"] });
  });

  it("window scroll is saved per tab and restored; a new tab starts at the top", () => {
    let y = 0;
    const scrollTo = vi.fn((_x, to) => { y = to; });                                   // like a browser, scrolling moves scrollY
    window.scrollTo = scrollTo;
    Object.defineProperty(window, "scrollY", { configurable: true, get: () => y });
    renderApp("/admin/talents");
    y = 480;
    fireEvent.click(sidebarLink("Projects"));
    expect(scrollTo).toHaveBeenLastCalledWith(0, 0);
    y = 120;
    fireEvent.click(sidebarLink("Talents"));
    expect(scrollTo).toHaveBeenLastCalledWith(0, 480);
    fireEvent.click(sidebarLink("Projects"));
    expect(scrollTo).toHaveBeenLastCalledWith(0, 120);
    delete window.scrollY;
  });

  it("an inner scroll area is put back where it was left", () => {
    renderApp("/admin/talents");
    const area = screen.getByTestId("scroller-talents");
    area.scrollTop = 150;
    fireEvent.scroll(area);
    fireEvent.click(sidebarLink("Projects"));
    area.scrollTop = 0;                                                            // what a browser does to a hidden (display: none) element
    fireEvent.click(sidebarLink("Talents"));
    expect(screen.getByTestId("scroller-talents").scrollTop).toBe(150);
  });

  it("a page may name its tab (while it is on that route); the name is not persisted", () => {
    renderApp("/admin/projects/p1");
    expect(tabs()).toEqual(["MK Jewels"]);
    expect(sessionStorage.getItem("tg_workspace_v1")).not.toContain("MK Jewels");
    expect(JSON.parse(sessionStorage.getItem("tg_workspace_v1")).tabs[0].title).toBe("Project");
  });

  it("many tabs: no limit on tabs, a bounded number of live pages (a page with unsaved work is never dropped), the bar scrolls", () => {
    renderApp("/admin/talents/abc");
    fireEvent.change(draft("talents/:id"), { target: { value: "unsaved" } });             // dirty page
    const pages = ["projects", "links", "applications", "workflow", "whatsapp", "production", "marketing", "feedback", "notifications", "casting-desk", "users", "storage", "diagnostics", "imports", "simple-assistant"];
    pages.forEach((p) => openTab(`/admin/${p}`));
    expect(screen.getAllByRole("tab")).toHaveLength(16);
    expect(document.querySelectorAll("[data-tab-pane]").length).toBeLessThanOrEqual(12);
    expect(screen.getByTestId("page-talents/:id")).toBeTruthy();                           // the one with unsaved work is still alive
    expect(draft("talents/:id").value).toBe("unsaved");
    fireEvent.click(screen.getAllByRole("tab")[1]);                                         // an evicted page simply re-opens fresh
    expect(activeTab()).toBe("Projects");
    expect(screen.getByTestId("page-projects")).toBeTruthy();
    expect(screen.getByRole("tablist").className).toMatch(/overflow-x-auto/);
  });
});

// ================================================================================================================ unsaved work
describe("unsaved changes", () => {
  it("closing a tab with unsaved work asks first; cancelling keeps the tab and the work", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderApp("/admin/talents/abc");
    fireEvent.change(draft("talents/:id"), { target: { value: "half-typed" } });
    expect(screen.getByRole("img", { name: "Unsaved changes" })).toBeTruthy();       // the tab shows a marker
    fireEvent.click(screen.getByLabelText("Close Talent"));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toMatch(/unsaved changes/i);
    expect(tabs()).toEqual(["Talent"]);
    expect(draft("talents/:id").value).toBe("half-typed");
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByLabelText("Close Talent"));
    expect(tabs()).toEqual(["Overview"]);                                             // closed on confirmation
  });

  it("a clean tab closes without a prompt; a hidden dirty tab still counts as dirty", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderApp("/admin/talents/abc");
    fireEvent.change(draft("talents/:id"), { target: { value: "x" } });
    openTab("/admin/links");                                                          // the dirty page is now hidden
    expect(screen.getByRole("img", { name: "Unsaved changes" })).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Close Links"));                             // clean → no prompt
    expect(confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Close Talent"));                            // hidden but dirty → prompt
    expect(confirm).toHaveBeenCalledTimes(1);
  });

  it("leaving the window with unsaved work in ANY tab is blocked by the browser's prompt; with none it is not", () => {
    renderApp("/admin/talents/abc");
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    fireEvent.change(draft("talents/:id"), { target: { value: "x" } });
    openTab("/admin/links");
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });

  it("a tab that moves on to a page that reports nothing is no longer treated as dirty", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderApp("/admin/talents/abc");
    fireEvent.change(draft("talents/:id"), { target: { value: "x" } });
    expect(screen.getByRole("img", { name: "Unsaved changes" })).toBeTruthy();
    act(() => globalThis.__nav("/admin/links"));                                       // the same tab now shows a page that never reports
    expect(tabs()).toEqual(["Links"]);
    expect(screen.queryByRole("img", { name: "Unsaved changes" })).toBeNull();
    fireEvent.click(screen.getByLabelText("Close Links"));
    expect(confirm).not.toHaveBeenCalled();
    const e = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(false);
  });

  it("navigating the tab away from the page that was dirty clears the flag", () => {
    renderApp("/admin/talents/abc");
    fireEvent.change(draft("talents/:id"), { target: { value: "x" } });
    fireEvent.click(screen.getByTestId("link-talents/:id"));                          // the page's own link leaves it
    expect(screen.queryByRole("img", { name: "Unsaved changes" })).toBeNull();
  });
});

// ================================================================================================================ history, deep links, refresh
describe("browser history, deep links and refresh", () => {
  it("Back / Forward return to the tab that owned that entry and never close a tab", () => {
    renderApp("/admin/talents");
    openTab("/admin/projects");
    openTab("/admin/links");
    act(() => globalThis.__nav(-1));
    expect(activeTab()).toBe("Projects");
    expect(tabs()).toEqual(["Talents", "Projects", "Links"]);
    act(() => globalThis.__nav(-1));
    expect(activeTab()).toBe("Talents");
    act(() => globalThis.__nav(1));
    act(() => globalThis.__nav(1));
    expect(activeTab()).toBe("Links");
    expect(tabs()).toEqual(["Talents", "Projects", "Links"]);
  });

  it("going Back to an entry whose tab was closed opens it as its own tab and touches no other tab's work", () => {
    renderApp("/admin/talents");
    fireEvent.change(draft("talents"), { target: { value: "mine" } });
    openTab("/admin/projects");
    openTab("/admin/links");
    fireEvent.click(screen.getByLabelText("Close Projects"));
    act(() => globalThis.__nav(-1));                                                   // the /projects entry, whose tab is gone
    expect(tabs()).toEqual(["Talents", "Links", "Projects"]);
    expect(activeTab()).toBe("Projects");
    fireEvent.click(screen.getAllByRole("tab")[0]);
    expect(draft("talents").value).toBe("mine");
  });

  it("a deep link opens that page (with its params and query) as the active tab", () => {
    renderApp("/admin/projects/p7/submissions?filter=hold");
    expect(tabs()).toEqual(["Submission Review"]);
    expect(screen.getByTestId("loc-projects/:id/submissions").textContent).toBe("/admin/projects/p7/submissions?filter=hold");
    expect(screen.getByTestId("params-projects/:id/submissions").textContent).toBe('{"id":"p7"}');
  });

  it("a refresh restores the open tabs and routes (pages load lazily, the active one first)", () => {
    const first = renderApp("/admin/talents");
    openTab("/admin/projects");
    openTab("/admin/links");
    first.unmount();
    renderApp("/admin/links");                                                         // a refresh: the URL is the active tab's
    expect(tabs()).toEqual(["Talents", "Projects", "Links"]);
    expect(activeTab()).toBe("Links");
    expect(screen.getByTestId("page-links")).toBeTruthy();
    expect(screen.queryByTestId("page-talents")).toBeNull();                           // not loaded until you open it
    fireEvent.click(screen.getAllByRole("tab")[0]);
    expect(screen.getByTestId("page-talents")).toBeTruthy();
    expect(url()).toBe("/admin/talents");
  });

  it("a refresh on a different URL than the saved active tab adds no duplicate for a page that is already open", () => {
    const first = renderApp("/admin/talents");
    openTab("/admin/projects");
    first.unmount();
    renderApp("/admin/talents");
    expect(tabs()).toEqual(["Talents", "Projects"]);
    expect(activeTab()).toBe("Talents");
  });
});

// ================================================================================================================ permissions, sign-out, account switching
describe("permissions and private state", () => {
  it("a restored tab the account may no longer open is skipped", () => {
    sessionStorage.setItem("tg_workspace_v1", JSON.stringify({ uid: "u2", activeId: "x", tabs: [{ id: "x", path: "/admin/users", title: "Users" }, { id: "y", path: "/admin/links", title: "Links" }, { id: "z", path: "/admin/zzz", title: "?" }] }));
    renderApp("/admin/talents", { id: "u2", role: "team" });
    expect(tabs()).toEqual(["Links", "Talents"]);
  });

  it("a denied entry URL (admin-only page, team member) gets no tab of its own: the default page is used and the URL is redirected", async () => {
    renderApp("/admin/users", { id: "u2", role: "team" });
    expect(tabs()).toEqual(["Overview"]);
    await waitFor(() => expect(url()).toBe("/admin"));
    expect(screen.queryByTestId("page-users")).toBeNull();
    expect(screen.getByTestId("page-overview")).toBeTruthy();
  });

  it("opening a page the account may not open is refused", () => {
    renderApp("/admin/talents", { id: "u2", role: "team" });
    let ok;
    act(() => { ok = globalThis.__ws.open("/admin/users"); });
    expect(ok).toBe(false);
    act(() => { ok = globalThis.__ws.open("/admin/does-not-exist"); });
    expect(ok).toBe(false);
    expect(tabs()).toEqual(["Talents"]);
  });

  it("one account's saved tabs are never shown to another account in the same browser window", () => {
    const first = renderApp("/admin/talents", { id: "u1", role: "admin" });
    openTab("/admin/projects");
    openTab("/admin/links");
    first.unmount();
    renderApp("/admin/talents", { id: "u2", role: "admin" });
    expect(tabs()).toEqual(["Talents"]);
  });

  it("signing out clears the saved tabs (the real sign-out and the session-expiry path)", async () => {
    const real = await vi.importActual("@/lib/api");
    sessionStorage.setItem("tg_workspace_v1", JSON.stringify({ uid: "u1", activeId: "a", tabs: [{ id: "a", path: "/admin/links", title: "Links" }] }));
    real.clearAdminSession();
    expect(sessionStorage.getItem("tg_workspace_v1")).toBeNull();
  });

  it("only routes are persisted — never a draft, a selection or a page-given title", () => {
    renderApp("/admin/projects/p1");
    fireEvent.change(draft("projects/:id"), { target: { value: "SECRET DRAFT TEXT" } });
    openTab("/admin/talents/abc?name=Private");
    const raw = sessionStorage.getItem("tg_workspace_v1");
    expect(raw).not.toContain("SECRET");
    expect(raw).not.toContain("MK Jewels");
    expect(Object.keys(JSON.parse(raw))).toEqual(["uid", "activeId", "tabs"]);
  });
});

// ================================================================================================================ robustness
describe("robustness", () => {
  it("a page that crashes takes down only its own tab", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderApp("/admin/talents");
    fireEvent.change(draft("talents"), { target: { value: "safe" } });
    globalThis.__crash = "projects";
    openTab("/admin/projects");
    expect(screen.getByRole("alert").textContent).toMatch(/hit an error/);
    expect(screen.getByTestId("admin-sidebar")).toBeTruthy();                           // the shell is intact
    fireEvent.click(screen.getAllByRole("tab")[0]);
    expect(draft("talents").value).toBe("safe");
  });

  it("the tab bar is fixed with an in-flow spacer (sticky never engages in this app), and the layout keeps the existing controls", async () => {
    renderApp("/admin/talents");
    const bar = screen.getByTestId("workspace-tabbar");
    expect(bar.className).toMatch(/\bfixed\b/);
    expect(bar.className).toMatch(/top-14 md:top-0/);                                    // under the mobile header, at the very top on desktop
    expect(bar.previousElementSibling.getAttribute("aria-hidden")).toBe("true");         // the spacer
    const root = bar.closest("main").parentElement;
    expect(root.style.getPropertyValue("--tg-tabbar-h")).toBe("2.25rem");
    expect(screen.getByTestId("admin-sidebar")).toBeTruthy();
    expect(screen.getByLabelText("Open navigation menu")).toBeTruthy();                   // the phone header and drawer are intact
    expect(await screen.findByTestId("pa-launcher")).toBeTruthy();
    expect(await screen.findByTestId("goto-launcher")).toBeTruthy();
  });

  it("long titles truncate but stay readable through the tab's title attribute and the close button's label", () => {
    renderApp("/admin/projects/p1");
    const tab = screen.getByRole("tab");
    expect(tab.getAttribute("title")).toBe("MK Jewels");
    expect(tab.querySelector("span.truncate")).toBeTruthy();
    expect(tab.closest("[data-testid^='ws-tab-']").className).toMatch(/max-w-\[11rem\]/);
    expect(screen.getByLabelText("Close MK Jewels")).toBeTruthy();
  });
});

// ================================================================================================================ pure helpers
describe("route metadata", () => {
  it("every sidebar destination is a real, titled route", () => {
    [...NAV_ITEMS.base, ...NAV_ITEMS.adminOnly].forEach((i) => {
      expect(matchRoute(i.to), i.to).toBeTruthy();
      expect(tabTitleFor(i.to)).toBeTruthy();
    });
  });
  it("admin-only is derived from the same list that hides the sidebar entries (they can never disagree)", () => {
    const fromNav = new Set(NAV_ITEMS.adminOnly.map((i) => i.to));
    ROUTE_META.forEach((m) => expect(m.adminOnly, m.abs).toBe(fromNav.has(m.abs) || [...fromNav].some((p) => m.abs.startsWith(`${p}/`))));
    expect(ROUTE_META.filter((m) => m.adminOnly).map((m) => m.abs).sort()).toEqual(["/admin/diagnostics", "/admin/imports", "/admin/storage", "/admin/users"]);
  });
  it("matches parameterised routes to their titles, most specific first", () => {
    expect(tabTitleFor("/admin")).toBe("Overview");
    expect(tabTitleFor("/admin/talents")).toBe("Talents");
    expect(tabTitleFor("/admin/talents/new")).toBe("New Talent");
    expect(tabTitleFor("/admin/talents/abc123")).toBe("Talent");
    expect(tabTitleFor("/admin/projects/p1")).toBe("Project");
    expect(tabTitleFor("/admin/projects/p1/submissions")).toBe("Submission Review");
    expect(tabTitleFor("/admin/links/new")).toBe("Generate Link");
    expect(tabTitleFor("/admin/links/l1/edit")).toBe("Edit Link");
    expect(tabTitleFor("/admin/nope")).toBe("Page");
  });
  it("normalises trailing slashes and compares paths without query / hash", () => {
    expect(normalizePathname("/admin/talents/")).toBe("/admin/talents");
    expect(samePath("/admin/talents/", "/admin/talents")).toBe(true);
    expect(matchRoute("/admin/talents/")?.abs).toBe("/admin/talents");
  });
});

describe("permissions", () => {
  it("unknown paths are never permitted; admin-only pages only for admins", () => {
    expect(isPermittedPath("/admin/nope", "admin")).toBe(false);
    expect(isPermittedPath("/login", "admin")).toBe(false);
    ["/admin/users", "/admin/storage", "/admin/diagnostics", "/admin/imports"].forEach((p) => {
      expect(isPermittedPath(p, "admin"), p).toBe(true);
      expect(isPermittedPath(p, "team"), p).toBe(false);
      expect(isPermittedPath(p, undefined), p).toBe(false);
    });
    expect(isPermittedPath("/admin/projects/p1/submissions", "team")).toBe(true);
  });
  it("the launcher offers a team member nothing admin-only, and an admin everything the sidebar has", () => {
    const team = destinationsFor("team").map((d) => d.to);
    const admin = destinationsFor("admin").map((d) => d.to);
    NAV_ITEMS.adminOnly.forEach((i) => { expect(team).not.toContain(i.to); expect(admin).toContain(i.to); });
    NAV_ITEMS.base.forEach((i) => { expect(team).toContain(i.to); expect(admin).toContain(i.to); });
    ["/admin/links/new", "/admin/talents/new", "/admin/projects/new"].forEach((p) => expect(team).toContain(p));
    expect(new Set(admin).size).toBe(admin.length);                                   // no duplicates
    destinationsFor("team").forEach((d) => expect(isPermittedPath(d.to, "team")).toBe(true));
  });
});

describe("launcher search", () => {
  const all = destinationsFor("admin");
  it("finds pages by name, by the words people use for them, and by group", () => {
    expect(filterDestinations(all, "generate").map((d) => d.label)).toEqual(["Generate Link"]);
    expect(filterDestinations(all, "global talent").map((d) => d.to)).toContain("/admin/talents");
    expect(filterDestinations(all, "roster pdf").map((d) => d.to)).toContain("/admin/links/new");
    expect(filterDestinations(all, "PROJ").map((d) => d.label)).toEqual(expect.arrayContaining(["Projects", "New Project"]));
    expect(filterDestinations(all, "zzzzz")).toEqual([]);
    expect(filterDestinations(all, "  ")).toEqual(all);
  });
});

describe("parsing and persistence", () => {
  it("parses a target into path, query and hash", () => {
    expect(parseTo("/admin/links/new?x=1#h")).toEqual({ pathname: "/admin/links/new", search: "?x=1", hash: "#h" });
    expect(fullPath(parseTo("/admin/talents?q=a"))).toBe("/admin/talents?q=a");
  });
  it("persists ONLY the route and a generic title per tab, scoped to the account", () => {
    writeSaved("u1", [{ id: "a", loc: { pathname: "/admin/talents/abc", search: "?q=x", hash: "" }, titleOverride: { path: "/admin/talents/abc", text: "Priya Secret Name" } }], "a");
    const raw = sessionStorage.getItem("tg_workspace_v1");
    expect(JSON.parse(raw)).toEqual({ uid: "u1", activeId: "a", tabs: [{ id: "a", path: "/admin/talents/abc?q=x", title: "Talent" }] });
    expect(raw).not.toContain("Priya");                                                // a page-given name (private data) is never written
    expect(readSaved("u1").tabs).toHaveLength(1);
    expect(readSaved("someone-else")).toBeNull();                                      // another account never reads it
    clearSaved();
    expect(readSaved("u1")).toBeNull();
  });
  it("garbage in storage is ignored, never thrown", () => {
    sessionStorage.setItem("tg_workspace_v1", "{not json");
    expect(readSaved("u1")).toBeNull();
    sessionStorage.setItem("tg_workspace_v1", JSON.stringify({ uid: "u1", tabs: "nope" }));
    expect(readSaved("u1")).toBeNull();
  });
});
