import { describe, it, expect, vi, afterEach } from "vitest";
import { render, cleanup, waitFor, screen } from "@testing-library/react";
import SignupPage from "./SignupPage";

// Regression (2026-09-19): SignupPage used react-router-dom's useNavigate/
// useSearchParams, but it's mounted by a Next.js App Router page
// (src/app/signup/page.jsx) with no <Router> wrapper anywhere in the tree.
// A direct navigation to the invite link — exactly how a real invitee opens
// it from email — crashed the whole app with "useLocation() may be used
// only in the context of a <Router> component." Fixed by switching to
// next/navigation's useRouter/useSearchParams, matching the established
// pattern already used by ResetPasswordPage.jsx and GoogleCallback.jsx.
// This test renders SignupPage with NO router wrapper of any kind, exactly
// reproducing the crash scenario, and asserts it renders cleanly instead.

const mockGet = vi.fn(() => "test-invite-token");
vi.mock("next/navigation", () => ({
    useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
    useSearchParams: () => ({ get: mockGet }),
}));

vi.mock("@/lib/api", () => ({
    api: { post: vi.fn(() => Promise.resolve({ data: { name: "Test User", email: "test@example.com", role: "team" } })) },
    saveAdminSession: vi.fn(),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

afterEach(cleanup);

describe("SignupPage — rendered with no react-router Router (real-world direct-navigation case)", () => {
    it("renders the invite form without throwing", async () => {
        render(<SignupPage />);
        await waitFor(() => expect(screen.getByTestId("signup-form")).toBeTruthy());
        expect(screen.getByText(/Welcome, Test\./)).toBeTruthy();
    });
});
