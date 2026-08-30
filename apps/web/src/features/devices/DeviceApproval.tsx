"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import type { SessionData } from "../../api/authentication-client";
import { rateLimitedRetryMessage } from "../authentication/rate-limit-copy";
import {
  browserSessionStore,
  type AuthenticationSessionStore,
} from "../authentication/session-store";
import { TotpChallenge } from "../authentication/TotpChallenge";
import {
  createBrowserDeviceAdministrationClient,
  type DeviceAdministrationClient,
  type DeviceGrantContextData,
} from "./device-administration-client";
import { formatDeviceTimestamp, platformClassLabel, remainingExpiryLabel } from "./device-display";

type ApprovalStep =
  | "resolving"
  | "inline-login"
  | "inline-challenge"
  | "context"
  | "reauth-required"
  | "decided"
  | "invalid-code"
  | "terminal-error";

const GENERIC_LOOKUP_FAILURE =
  "The sign-in request could not be loaded. Try the link from the plugin again.";
const REOPEN_PLUGIN_GUIDANCE =
  "This sign-in request expired. Press Open browser again in the Obsidian plugin.";
const LOOKUP_FAILURE_MESSAGES: Readonly<Record<string, string>> = {
  device_credential_invalid:
    "That code is not recognized. Press Open browser again in the Obsidian plugin and use the fresh link.",
  device_authorization_denied: "This sign-in request was already denied.",
  device_authorization_expired: REOPEN_PLUGIN_GUIDANCE,
};
const ALREADY_DECIDED_MESSAGE = "This sign-in request was already decided.";
const RECOVERY_LIMITED_GUIDANCE =
  "Recovery-mode sign-in must be completed on the sign-in page first. Press Open browser again in the plugin afterwards.";
const NO_CODE_GUIDANCE =
  "This page was opened without a device code. Press Open browser again in the Obsidian plugin and use the fresh link.";

export interface DeviceApprovalProps {
  client?: DeviceAdministrationClient;
  sessionStore?: AuthenticationSessionStore;
}

/**
 * The device-approval page surface (spec 11.2, 11.3, 18.2): it consumes the
 * user code from the URL fragment exactly once, strips the fragment, and holds
 * the grant context in component memory only. Missing sessions sign in inline
 * — the already-captured code is re-resolved without reconstructing the
 * fragment — and every plugin metadata value renders as plain React text.
 */
export function DeviceApproval({
  client = createBrowserDeviceAdministrationClient(),
  sessionStore = browserSessionStore,
}: DeviceApprovalProps): ReactNode {
  const [step, setStep] = useState<ApprovalStep>("resolving");
  const [grantContext, setGrantContext] = useState<DeviceGrantContextData | null>(null);
  const [outcome, setOutcome] = useState<"approved" | "denied" | null>(null);
  const [terminalMessage, setTerminalMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [loginFields, setLoginFields] = useState({ username: "", password: "" });
  const [challengePassword, setChallengePassword] = useState("");
  const [reauthFields, setReauthFields] = useState({ password: "", totpCode: "" });
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isLookupRetryable, setIsLookupRetryable] = useState(false);
  const userCodeRef = useRef<string | null>(null);
  const fragmentConsumedRef = useRef(false);
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (errorMessage !== null) {
      errorRef.current?.focus();
    }
  }, [errorMessage]);

  const lookupGrant = useCallback(async (): Promise<void> => {
    const capturedUserCode = userCodeRef.current;
    if (capturedUserCode === null) {
      setStep("invalid-code");
      return;
    }
    setStep("resolving");
    const result = await client.lookupDeviceAuthorization({ userCode: capturedUserCode });
    if (result.ok) {
      setGrantContext(result.data);
      setStep("context");
      return;
    }
    if (result.error.code === "authentication_required") {
      setStep("inline-login");
      return;
    }
    if (result.error.code === "authentication_rate_limited") {
      setTerminalMessage(rateLimitedRetryMessage(result.error) ?? GENERIC_LOOKUP_FAILURE);
      setIsLookupRetryable(true);
      setStep("terminal-error");
      return;
    }
    setTerminalMessage(LOOKUP_FAILURE_MESSAGES[result.error.code] ?? GENERIC_LOOKUP_FAILURE);
    setIsLookupRetryable(false);
    setStep("terminal-error");
  }, [client]);

  const resolveSession = useCallback(async (): Promise<void> => {
    const result = await client.getSession();
    if (result.ok && result.data.state === "active") {
      sessionStore.setSession(result.data);
      await lookupGrant();
      return;
    }
    setStep("inline-login");
  }, [client, lookupGrant, sessionStore]);

  useEffect(() => {
    if (fragmentConsumedRef.current) {
      return;
    }
    // Consume the fragment exactly once (spec 11.2): capture the user code
    // into memory, then strip it from the address bar so it never survives a
    // reload, a share or the browser history. The query string is preserved —
    // it never carried the code.
    fragmentConsumedRef.current = true;
    const fragment = window.location.hash.replace(/^#/, "").trim();
    userCodeRef.current = fragment === "" ? null : fragment;
    if (window.location.hash !== "") {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    void resolveSession();
  }, [resolveSession]);

  function continueWithSession(session: SessionData, password: string): void {
    sessionStore.setSession(session);
    setErrorMessage(null);
    if (session.state === "pending_totp") {
      setChallengePassword(password);
      setStep("inline-challenge");
      return;
    }
    if (session.state === "recovery_limited") {
      setTerminalMessage(RECOVERY_LIMITED_GUIDANCE);
      setStep("terminal-error");
      return;
    }
    void lookupGrant();
  }

  function handleInlineLogin(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }
    const username = loginFields.username.trim();
    const { password } = loginFields;
    if (username === "" || password === "") {
      setErrorMessage("Enter your username and password.");
      return;
    }
    void (async () => {
      setIsSubmitting(true);
      setErrorMessage(null);
      const result = await client.login({ username, password });
      setIsSubmitting(false);
      if (!result.ok) {
        setErrorMessage(
          rateLimitedRetryMessage(result.error) ?? "Sign-in failed. Check your username and password.",
        );
        return;
      }
      continueWithSession(result.data, password);
    })();
  }

  function closeAsTerminal(message: string): void {
    setGrantContext(null);
    // The inline-challenge password is never needed past this point; drop it
    // with the rest of the one-time flow state.
    setChallengePassword("");
    setTerminalMessage(message);
    setIsLookupRetryable(false);
    setStep("terminal-error");
  }

  function requestDecision(requested: "approve" | "deny"): void {
    if (grantContext === null || isSubmitting) {
      return;
    }
    void (async () => {
      setIsSubmitting(true);
      setErrorMessage(null);
      const result =
        requested === "approve"
          ? await client.approveDeviceAuthorization({ grantId: grantContext.grant_id })
          : await client.denyDeviceAuthorization({ grantId: grantContext.grant_id });
      setIsSubmitting(false);
      const expectedState = requested === "approve" ? "approved" : "denied";
      if (result.ok && result.data.state === expectedState) {
        setOutcome(expectedState);
        setGrantContext(null);
        setStep("decided");
        return;
      }
      const errorCode = result.ok ? null : result.error.code;
      if (result.ok || errorCode === "device_authorization_state_invalid") {
        closeAsTerminal(ALREADY_DECIDED_MESSAGE);
        return;
      }
      if (errorCode === "device_authorization_expired") {
        closeAsTerminal(REOPEN_PLUGIN_GUIDANCE);
        return;
      }
      if (errorCode === "recent_authentication_required" && requested === "approve") {
        setStep("reauth-required");
        return;
      }
      setErrorMessage(
        rateLimitedRetryMessage(result.error) ??
          (requested === "approve"
            ? "Approving the device failed. Try again."
            : "Denying the device failed. Try again."),
      );
    })();
  }

  function handleReauthentication(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }
    if (reauthFields.password === "") {
      setErrorMessage("Enter your password.");
      return;
    }
    const totpCode = reauthFields.totpCode.trim();
    void (async () => {
      setIsSubmitting(true);
      setErrorMessage(null);
      const result = await client.reauthenticate({
        password: reauthFields.password,
        totpCode: totpCode === "" ? undefined : totpCode,
      });
      if (!result.ok) {
        setIsSubmitting(false);
        setErrorMessage(
          rateLimitedRetryMessage(result.error) ?? "Your current credentials were not accepted. Try again.",
        );
        return;
      }
      sessionStore.setSession(result.data);
      setReauthFields({ password: "", totpCode: "" });
      setIsSubmitting(false);
      setStep("context");
      requestDecision("approve");
    })();
  }

  /**
   * Abandons the re-authentication gate without deciding anything: the held
   * grant context is shown again and the partially typed credentials are
   * dropped, exactly as they are after a successful confirmation.
   */
  function abandonReauthentication(): void {
    if (isSubmitting) {
      return;
    }
    setReauthFields({ password: "", totpCode: "" });
    setErrorMessage(null);
    setStep("context");
  }

  if (step === "resolving") {
    return <p role="status">Checking the sign-in request…</p>;
  }

  if (step === "inline-login") {
    return (
      <section aria-labelledby="device-approval-login-heading">
        <h1 id="device-approval-login-heading">Sign in to approve the device</h1>
        <p>Sign in to review the device waiting for approval.</p>
        <form onSubmit={handleInlineLogin} noValidate>
          <label htmlFor="device-approval-username">Username</label>
          <input
            id="device-approval-username"
            name="username"
            autoComplete="username"
            value={loginFields.username}
            onChange={(event) =>
              setLoginFields((fields) => ({ ...fields, username: event.target.value }))
            }
          />
          <label htmlFor="device-approval-password">Password</label>
          <input
            id="device-approval-password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={loginFields.password}
            onChange={(event) =>
              setLoginFields((fields) => ({ ...fields, password: event.target.value }))
            }
          />
          <button type="submit" disabled={isSubmitting}>
            Sign in
          </button>
        </form>
        {errorMessage !== null && (
          <p ref={errorRef} role="alert" tabIndex={-1} className="error-message">
            {errorMessage}
          </p>
        )}
      </section>
    );
  }

  if (step === "inline-challenge") {
    return (
      <TotpChallenge
        client={client}
        password={challengePassword}
        onActiveSession={(session) => continueWithSession(session, "")}
        onRecoveryLimited={() => closeAsTerminal(RECOVERY_LIMITED_GUIDANCE)}
      />
    );
  }

  if (step === "invalid-code" || step === "terminal-error") {
    return (
      <section aria-labelledby="device-approval-unavailable-heading">
        <h1 id="device-approval-unavailable-heading">Device approval unavailable</h1>
        <p role="note">{step === "invalid-code" ? NO_CODE_GUIDANCE : (terminalMessage ?? GENERIC_LOOKUP_FAILURE)}</p>
        {step === "terminal-error" && isLookupRetryable && (
          <button type="button" onClick={() => void lookupGrant()}>
            Retry
          </button>
        )}
      </section>
    );
  }

  if (step === "reauth-required") {
    return (
      <section aria-labelledby="device-approval-reauth-heading">
        <h1 id="device-approval-reauth-heading">Confirm your password again</h1>
        <p>Confirm your password again to approve this device.</p>
        <form onSubmit={handleReauthentication} noValidate>
          <label htmlFor="device-approval-reauth-password">Current password</label>
          <input
            id="device-approval-reauth-password"
            type="password"
            autoComplete="current-password"
            value={reauthFields.password}
            onChange={(event) =>
              setReauthFields((fields) => ({ ...fields, password: event.target.value }))
            }
          />
          <label htmlFor="device-approval-reauth-totp">Authentication code</label>
          <input
            id="device-approval-reauth-totp"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={reauthFields.totpCode}
            onChange={(event) =>
              setReauthFields((fields) => ({ ...fields, totpCode: event.target.value }))
            }
          />
          <button type="submit" disabled={isSubmitting}>
            Confirm password
          </button>
          <button type="button" disabled={isSubmitting} onClick={abandonReauthentication}>
            Cancel
          </button>
        </form>
        {errorMessage !== null && (
          <p ref={errorRef} role="alert" tabIndex={-1} className="error-message">
            {errorMessage}
          </p>
        )}
      </section>
    );
  }

  if (step === "decided") {
    return (
      <section aria-labelledby="device-decision-heading">
        <h1 id="device-decision-heading">
          {outcome === "approved" ? "Device approved" : "Device denied"}
        </h1>
        <p>
          {outcome === "approved"
            ? "You can return to the Obsidian plugin; it will finish setting up sync."
            : "The Obsidian plugin was told to stop. No access was granted."}
        </p>
        <Link href="/admin/devices">Manage devices</Link>
      </section>
    );
  }

  if (grantContext === null) {
    return <p role="status">Checking the sign-in request…</p>;
  }

  return (
    <section aria-labelledby="device-approval-heading">
      <h1 id="device-approval-heading">Approve device access</h1>
      <p>
        Confirm this code matches the one shown by the plugin: <code>{grantContext.user_code}</code>
      </p>
      <dl className="device-grant-context">
        <dt>Device name</dt>
        <dd>{grantContext.device_name}</dd>
        <dt>Device type</dt>
        <dd>{platformClassLabel(grantContext.platform_class)}</dd>
        <dt>Platform</dt>
        <dd>{grantContext.platform_name}</dd>
        <dt>Plugin version</dt>
        <dd>{grantContext.plugin_version}</dd>
        <dt>Scope</dt>
        <dd>{grantContext.requested_scope}</dd>
        <dt>Expires</dt>
        <dd>
          <time dateTime={grantContext.expires_at}>
            {formatDeviceTimestamp(grantContext.expires_at)}
          </time>{" "}
          (in {remainingExpiryLabel(grantContext.expires_at)})
        </dd>
      </dl>
      <div className="device-approval-actions">
        <button
          type="button"
          disabled={isSubmitting}
          onClick={() => requestDecision("approve")}
        >
          Approve
        </button>
        <button type="button" disabled={isSubmitting} onClick={() => requestDecision("deny")}>
          Deny
        </button>
      </div>
      {errorMessage !== null && (
        <p ref={errorRef} role="alert" tabIndex={-1} className="error-message">
          {errorMessage}
        </p>
      )}
    </section>
  );
}
