import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type components } from "@workspace/api-client";

import { createNativeFetchTransport } from "../../api/native-fetch-transport";
import {
  createAuthenticationClient,
  readCsrfTokenFromCookieSource,
  type AuthenticationClient,
} from "../../api/authentication-client";
import {
  CSRF_COOKIE_VALUE,
  MOCK_API_BASE_URL,
  authenticationFailedResponse,
  installMockCsrfCookie,
  mockApi,
  rateLimitedResponse,
  recoveryCodesResponse,
  recoveryLimitedResponse,
  sessionResponse,
  totpEnrollmentData,
} from "../../testing/api-mock-builders";

type TotpEnrollmentOfferData = components["schemas"]["TotpEnrollmentOfferData"];
import { TotpChallenge, TotpEnrollmentOffer } from "./TotpChallenge";

const server = setupServer();

function createTestClient(): AuthenticationClient {
  return createAuthenticationClient({
    apiClient: createApiClient({
      baseUrl: MOCK_API_BASE_URL,
      transport: createNativeFetchTransport(globalThis.fetch),
    }),
    readCsrfToken: () => readCsrfTokenFromCookieSource(document.cookie),
  });
}

function testEnrollment(): TotpEnrollmentOfferData {
  const data = totpEnrollmentData();
  if (!data.enrollment) {
    throw new Error("test fixture enrollment offer missing");
  }
  return data.enrollment;
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => installMockCsrfCookie());
afterEach(() => {
  server.resetHandlers();
  document.cookie = "admin_csrf_local=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});
afterAll(() => server.close());

describe("TotpChallenge", () => {
  it("verifies the code and reports the active session", async () => {
    const user = userEvent.setup();
    const seenRequests: Request[] = [];
    server.use(
      mockApi("post", "/api/auth/totp/verify", async ({ request }) => {
        seenRequests.push(request);
        return sessionResponse("active");
      }),
    );
    const onActiveSession = vi.fn();
    render(
      <TotpChallenge
        client={createTestClient()}
        password="correct horse battery staple!"
        onActiveSession={onActiveSession}
        onRecoveryLimited={vi.fn()}
      />,
    );
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(onActiveSession).toHaveBeenCalledTimes(1));
    expect(seenRequests[0]?.headers.get("x-csrf-token")).toBe(CSRF_COOKIE_VALUE);
    expect(await new Response(seenRequests[0]?.body ?? "").text()).toBe('{"code":"123456"}');
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });

  it("shows a generic failure and moves focus to the first actionable error", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/totp/verify", () => authenticationFailedResponse()));
    const onActiveSession = vi.fn();
    render(
      <TotpChallenge
        client={createTestClient()}
        password="correct horse battery staple!"
        onActiveSession={onActiveSession}
        onRecoveryLimited={vi.fn()}
      />,
    );
    const codeInput = screen.getByLabelText("Authentication code");
    await user.type(codeInput, "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Verification failed. Check the code and try again.");
    expect(alert).toHaveFocus();
    expect(onActiveSession).not.toHaveBeenCalled();
  });

  it("shows bounded retry guidance when the challenge throttle is active", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/totp/verify", () => rateLimitedResponse(125)));
    const onActiveSession = vi.fn();
    render(
      <TotpChallenge
        client={createTestClient()}
        password="correct horse battery staple!"
        onActiveSession={onActiveSession}
        onRecoveryLimited={vi.fn()}
      />,
    );
    await user.type(screen.getByLabelText("Authentication code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Too many attempts. Try again in 3 minutes.");
    expect(alert).toHaveFocus();
    expect(alert.textContent).not.toContain("125");
    expect(onActiveSession).not.toHaveBeenCalled();
  });

  it("switches to recovery-code entry and reports the recovery-limited session", async () => {
    const user = userEvent.setup();
    const seenRequests: Request[] = [];
    server.use(
      mockApi("post", "/api/auth/totp/recovery", async ({ request }) => {
        seenRequests.push(request);
        return recoveryLimitedResponse();
      }),
    );
    const onRecoveryLimited = vi.fn();
    render(
      <TotpChallenge
        client={createTestClient()}
        password="correct horse battery staple!"
        onActiveSession={vi.fn()}
        onRecoveryLimited={onRecoveryLimited}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    await user.type(screen.getByLabelText("Recovery code"), "ABCD-EFGH-IJKL");
    await user.click(screen.getByRole("button", { name: "Continue with recovery code" }));
    await waitFor(() => expect(onRecoveryLimited).toHaveBeenCalledTimes(1));
    expect(seenRequests[0]?.headers.get("x-csrf-token")).toBe(CSRF_COOKIE_VALUE);
    expect(await new Response(seenRequests[0]?.body ?? "").text()).toBe(
      '{"password":"correct horse battery staple!","recovery_code":"ABCD-EFGH-IJKL"}',
    );
  });

  it("shows bounded retry guidance when the recovery throttle is active", async () => {
    const user = userEvent.setup();
    server.use(mockApi("post", "/api/auth/totp/recovery", () => rateLimitedResponse(60)));
    const onRecoveryLimited = vi.fn();
    render(
      <TotpChallenge
        client={createTestClient()}
        password="correct horse battery staple!"
        onActiveSession={vi.fn()}
        onRecoveryLimited={onRecoveryLimited}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Use a recovery code instead" }));
    await user.type(screen.getByLabelText("Recovery code"), "ABCD-EFGH-IJKL");
    await user.click(screen.getByRole("button", { name: "Continue with recovery code" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Too many attempts. Try again in 1 minute.");
    expect(alert.textContent).not.toContain("60");
    expect(onRecoveryLimited).not.toHaveBeenCalled();
  });
});

describe("TotpEnrollmentOffer", () => {
  it("renders the qr code locally from the provisioning uri", () => {
    const { container } = render(
      <TotpEnrollmentOffer
        client={createTestClient()}
        enrollment={testEnrollment()}
        onCompleted={vi.fn()}
        onSkipped={vi.fn()}
      />,
    );
    expect(container.querySelector("svg")).not.toBeNull();
    expect(screen.getByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
  });

  it("copies the secret on demand without persisting it", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    render(
      <TotpEnrollmentOffer
        client={createTestClient()}
        enrollment={testEnrollment()}
        onCompleted={vi.fn()}
        onSkipped={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Copy secret" }));
    expect(writeText).toHaveBeenCalledWith("JBSWY3DPEHPK3PXP");
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });

  it("activates the credential and hands back the one-time recovery codes", async () => {
    const user = userEvent.setup();
    const seenRequests: Request[] = [];
    server.use(
      mockApi("post", "/api/auth/totp/enrollments/e26e0f1c-9884-4d84-a2c3-9d64a0b1f001/verify", async ({ request }) => {
        seenRequests.push(request);
        return recoveryCodesResponse();
      }),
    );
    const onCompleted = vi.fn();
    render(
      <TotpEnrollmentOffer
        client={createTestClient()}
        enrollment={testEnrollment()}
        onCompleted={onCompleted}
        onSkipped={vi.fn()}
      />,
    );
    await user.type(screen.getByLabelText("Verification code"), "123456");
    await user.click(screen.getByRole("button", { name: "Activate" }));
    await waitFor(() =>
      expect(onCompleted).toHaveBeenCalledWith(
        expect.objectContaining({
          codes: ["ABCD-EFGH-IJKL", "MNOP-QRST-UVWX", "YZ23-4567-89AB"],
          revision: 3,
        }),
      ),
    );
    expect(seenRequests[0]?.headers.get("x-csrf-token")).toBe(CSRF_COOKIE_VALUE);
  });

  it("skips the first-login offer through the dismissal action", async () => {
    const user = userEvent.setup();
    const seenBodies: string[] = [];
    server.use(
      mockApi("post", "/api/auth/totp/enrollments", async ({ request }) => {
        seenBodies.push(await request.text());
        return recoveryCodesResponse();
      }),
    );
    const onSkipped = vi.fn();
    render(
      <TotpEnrollmentOffer
        client={createTestClient()}
        enrollment={testEnrollment()}
        onCompleted={vi.fn()}
        onSkipped={onSkipped}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Skip for now" }));
    await waitFor(() => expect(onSkipped).toHaveBeenCalledTimes(1));
    expect(seenBodies).toEqual(['{"action":"dismiss_initial_offer"}']);
  });

  it("hides the skip control when replacement is required", () => {
    render(
      <TotpEnrollmentOffer
        client={createTestClient()}
        enrollment={testEnrollment()}
        requireCompletion
        onCompleted={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Skip for now" })).not.toBeInTheDocument();
  });

  it("reports an activation failure and refocuses the code input", async () => {
    const user = userEvent.setup();
    server.use(
      mockApi("post", "/api/auth/totp/enrollments/e26e0f1c-9884-4d84-a2c3-9d64a0b1f001/verify", () =>
        authenticationFailedResponse(),
      ),
    );
    render(
      <TotpEnrollmentOffer
        client={createTestClient()}
        enrollment={testEnrollment()}
        onCompleted={vi.fn()}
        onSkipped={vi.fn()}
      />,
    );
    const codeInput = screen.getByLabelText("Verification code");
    await user.type(codeInput, "000000");
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Activation failed. Check the code and try again.",
    );
    expect(codeInput).toHaveFocus();
  });

  it("shows bounded retry guidance when the activation throttle is active", async () => {
    const user = userEvent.setup();
    server.use(
      mockApi("post", "/api/auth/totp/enrollments/e26e0f1c-9884-4d84-a2c3-9d64a0b1f001/verify", () =>
        rateLimitedResponse(300),
      ),
    );
    render(
      <TotpEnrollmentOffer
        client={createTestClient()}
        enrollment={testEnrollment()}
        onCompleted={vi.fn()}
        onSkipped={vi.fn()}
      />,
    );
    await user.type(screen.getByLabelText("Verification code"), "123456");
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many attempts. Try again in 5 minutes.",
    );
  });
});
