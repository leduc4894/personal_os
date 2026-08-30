"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";

import type {
  AuthenticationClient,
  RecoveryCodesData,
  SessionData,
  TotpEnrollmentOfferData,
} from "../../api/authentication-client";
import { createBrowserAuthenticationClient } from "../../api/authentication-client";
import {
  browserSessionStore,
  type AuthenticationSessionStore,
} from "./session-store";
import { rateLimitedRetryMessage } from "./rate-limit-copy";
import { TotpChallenge, TotpEnrollmentOffer } from "./TotpChallenge";

type LoginStep = "password" | "challenge" | "replacement" | "recovery-codes";

export interface LoginFormProps {
  client?: AuthenticationClient;
  sessionStore?: AuthenticationSessionStore;
}

/**
 * The interactive login state machine: password, optional TOTP challenge,
 * recovery-limited replacement, and the one-time recovery-code reveal. New
 * password-only sessions proceed directly; TOTP remains opt-in from Security.
 * Every secret stays in component memory.
 */
export function LoginForm({
  client = createBrowserAuthenticationClient(),
  sessionStore = browserSessionStore,
}: LoginFormProps): ReactNode {
  const router = useRouter();
  const [step, setStep] = useState<LoginStep>("password");
  const [password, setPassword] = useState("");
  const [username, setUsername] = useState("");
  const [enrollment, setEnrollment] = useState<TotpEnrollmentOfferData | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<RecoveryCodesData | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const usernameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (errorMessage === null) {
      return;
    }
    if (step === "password") {
      const isMissingFieldError = errorMessage === "Enter your username and password.";
      if (isMissingFieldError) {
        usernameRef.current?.focus();
        return;
      }
    }
    errorRef.current?.focus();
  }, [errorMessage, step]);

  useEffect(() => {
    let cancelled = false;
    void client.getSession().then((result) => {
      if (cancelled || !result.ok) {
        return;
      }
      if (result.data.state === "active") {
        sessionStore.setSession(result.data);
        router.replace("/admin/devices");
        return;
      }
      if (result.data.state === "pending_totp") {
        setStep("challenge");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [client, router, sessionStore]);

  /** Clears every one-time value before leaving the flow. */
  function clearOneTimeValues(): void {
    setEnrollment(null);
    setRecoveryCodes(null);
    setPassword("");
  }

  function completeLogin(session: SessionData): void {
    sessionStore.setSession(session);
    clearOneTimeValues();
    router.replace("/admin/devices");
  }

  async function beginReplacementEnrollment(): Promise<void> {
    // Both recovery-limited transitions land here; the held password has done
    // its job the moment recovery mode starts, so drop it before anything else.
    setPassword("");
    const result = await client.startTotpEnrollment();
    if (result.ok && result.data.enrollment !== null && result.data.enrollment !== undefined) {
      setEnrollment(result.data.enrollment);
      setStep("replacement");
      return;
    }
    setErrorMessage("Recovery mode requires a new authenticator. Start it from the Security page.");
  }

  async function submitPassword(submittedUsername: string, submittedPassword: string): Promise<void> {
    if (submittedUsername.trim() === "" || submittedPassword === "") {
      setErrorMessage("Enter your username and password.");
      return;
    }
    setIsSubmitting(true);
    setErrorMessage(null);
    const result = await client.login({
      username: submittedUsername.trim(),
      password: submittedPassword,
    });
    setIsSubmitting(false);
    if (!result.ok) {
      setErrorMessage(
        rateLimitedRetryMessage(result.error) ?? "Sign-in failed. Check your username and password.",
      );
      return;
    }
    const session = result.data;
    if (session.state === "pending_totp") {
      setPassword(submittedPassword);
      setStep("challenge");
      return;
    }
    if (session.state === "recovery_limited") {
      sessionStore.setSession(session);
      await beginReplacementEnrollment();
      return;
    }
    sessionStore.setSession(session);
    completeLogin(session);
  }

  function handlePasswordSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!isSubmitting) {
      void submitPassword(username, password);
    }
  }

  function handleEnrollmentCompleted(codes: RecoveryCodesData): void {
    setEnrollment(null);
    setRecoveryCodes(codes);
    setStep("recovery-codes");
  }

  if (step === "challenge") {
    return (
      <TotpChallenge
        client={client}
        password={password}
        onActiveSession={(session) => completeLogin(session)}
        onRecoveryLimited={() => {
          void beginReplacementEnrollment();
        }}
      />
    );
  }

  if (step === "replacement" && enrollment !== null) {
    return (
      <>
        {step === "replacement" && (
          <p role="note">Recovery mode: set up a new authenticator before continuing.</p>
        )}
        <TotpEnrollmentOffer
          client={client}
          enrollment={enrollment}
          requireCompletion={step === "replacement"}
          onCompleted={handleEnrollmentCompleted}
          onSkipped={undefined}
        />
      </>
    );
  }

  if (step === "recovery-codes" && recoveryCodes !== null) {
    return (
      <section aria-labelledby="recovery-codes-heading">
        <h2 id="recovery-codes-heading">Save your recovery codes</h2>
        <p>These codes are shown only once. Store them somewhere safe.</p>
        <ul>
          {recoveryCodes.codes.map((code) => (
            <li key={code}>
              <code>{code}</code>
            </li>
          ))}
        </ul>
        <button
          type="button"
          onClick={() => {
            setRecoveryCodes(null);
            router.replace("/admin/devices");
          }}
        >
          Continue to devices
        </button>
      </section>
    );
  }

  return (
    <form onSubmit={handlePasswordSubmit} noValidate aria-labelledby="login-heading">
      <h1 id="login-heading">Sign in</h1>
      <label htmlFor="login-username">Username</label>
      <input
        id="login-username"
        name="username"
        ref={usernameRef}
        autoComplete="username"
        value={username}
        onChange={(event) => setUsername(event.target.value)}
      />
      <label htmlFor="login-password">Password</label>
      <input
        id="login-password"
        name="password"
        type="password"
        autoComplete="current-password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
      />
      <button type="submit" disabled={isSubmitting}>
        Sign in
      </button>
      {errorMessage !== null && (
        <p ref={errorRef} role="alert" tabIndex={-1} className="error-message">
          {errorMessage}
        </p>
      )}
    </form>
  );
}
