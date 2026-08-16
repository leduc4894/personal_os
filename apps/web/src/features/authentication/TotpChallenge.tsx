"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
  type Ref,
} from "react";
import qrcode from "qrcode-generator";

import type {
  AuthenticationClient,
  RecoveryCodesData,
  SessionData,
  TotpEnrollmentOfferData,
} from "../../api/authentication-client";
import { rateLimitedRetryMessage } from "./rate-limit-copy";

/** A shared error region that steals focus once so screen readers announce it. */
function ErrorAnnouncement({
  message,
  announceRef,
}: {
  message: string;
  announceRef?: Ref<HTMLParagraphElement>;
}): ReactNode {
  return (
    <p ref={announceRef} role="alert" tabIndex={-1} className="error-message">
      {message}
    </p>
  );
}

export interface TotpChallengeProps {
  client: AuthenticationClient;
  /** The password that produced the pending session; kept in memory for recovery. */
  password: string;
  onActiveSession: (session: SessionData) => void;
  onRecoveryLimited: () => void;
}

/**
 * The second factor of an interactive login: a six-digit TOTP code, or a
 * recovery code that yields the recovery-limited replacement flow.
 */
export function TotpChallenge({
  client,
  password,
  onActiveSession,
  onRecoveryLimited,
}: TotpChallengeProps): ReactNode {
  const [mode, setMode] = useState<"code" | "recovery">("code");
  const [code, setCode] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (errorMessage !== null) {
      errorRef.current?.focus();
    }
  }, [errorMessage]);

  async function submitCode(submittedCode: string): Promise<void> {
    setIsSubmitting(true);
    setErrorMessage(null);
    const result = await client.verifyTotpChallenge({ code: submittedCode });
    setIsSubmitting(false);
    if (result.ok && result.data.state === "active") {
      setCode("");
      onActiveSession(result.data);
      return;
    }
    const genericMessage = "Verification failed. Check the code and try again.";
    setErrorMessage(
      result.ok ? genericMessage : (rateLimitedRetryMessage(result.error) ?? genericMessage),
    );
  }

  async function submitRecovery(submittedRecoveryCode: string): Promise<void> {
    setIsSubmitting(true);
    setErrorMessage(null);
    const result = await client.startTotpRecovery({
      password,
      recoveryCode: submittedRecoveryCode,
    });
    setIsSubmitting(false);
    if (result.ok && result.data.state === "recovery_limited") {
      setRecoveryCode("");
      onRecoveryLimited();
      return;
    }
    const genericMessage = "Recovery failed. Check the recovery code and try again.";
    setErrorMessage(
      result.ok ? genericMessage : (rateLimitedRetryMessage(result.error) ?? genericMessage),
    );
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }
    if (mode === "code") {
      void submitCode(code.trim());
      return;
    }
    void submitRecovery(recoveryCode.trim());
  }

  return (
    <section aria-labelledby="totp-challenge-heading">
      <h2 id="totp-challenge-heading">Two-factor verification</h2>
      {mode === "code" ? (
        <form onSubmit={handleSubmit} noValidate>
          <label htmlFor="totp-challenge-code">Authentication code</label>
          <input
            id="totp-challenge-code"
            name="code"
            autoComplete="one-time-code"
            inputMode="numeric"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
          <button type="submit" disabled={isSubmitting}>
            Verify
          </button>
          <button
            type="button"
            onClick={() => {
              setMode("recovery");
              setErrorMessage(null);
            }}
          >
            Use a recovery code instead
          </button>
        </form>
      ) : (
        <form onSubmit={handleSubmit} noValidate>
          <label htmlFor="totp-challenge-recovery-code">Recovery code</label>
          <input
            id="totp-challenge-recovery-code"
            name="recovery_code"
            value={recoveryCode}
            onChange={(event) => setRecoveryCode(event.target.value)}
          />
          <button type="submit" disabled={isSubmitting}>
            Continue with recovery code
          </button>
          <button
            type="button"
            onClick={() => {
              setMode("code");
              setErrorMessage(null);
            }}
          >
            Use an authentication code instead
          </button>
        </form>
      )}
      {errorMessage !== null && <ErrorAnnouncement message={errorMessage} announceRef={errorRef} />}
    </section>
  );
}

export interface TotpEnrollmentOfferProps {
  client: AuthenticationClient;
  enrollment: TotpEnrollmentOfferData;
  /** Recovery-limited replacement cannot be skipped (spec 10.3). */
  requireCompletion?: boolean | undefined;
  onCompleted: (codes: RecoveryCodesData) => void;
  onSkipped?: (() => void) | undefined;
}

/**
 * The one-time TOTP enrollment screen: a locally rendered QR, the Base32
 * secret for manual entry, and the activation code step. The provisioning
 * material lives in component memory only and is cleared on unmount.
 */
export function TotpEnrollmentOffer({
  client,
  enrollment,
  requireCompletion = false,
  onCompleted,
  onSkipped,
}: TotpEnrollmentOfferProps): ReactNode {
  const [code, setCode] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const codeInputRef = useRef<HTMLInputElement>(null);

  const qrSvgMarkup = useMemo(() => {
    const qr = qrcode(0, "M");
    qr.addData(enrollment.provisioning_uri);
    qr.make();
    return qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
  }, [enrollment.provisioning_uri]);

  useEffect(() => {
    if (errorMessage !== null) {
      codeInputRef.current?.focus();
    }
  }, [errorMessage]);

  useEffect(
    () => () => {
      setCode("");
      setErrorMessage(null);
      setCopyStatus(null);
    },
    [],
  );

  async function copyValue(value: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      setCopyStatus("Copied.");
    } catch {
      setCopyStatus("Copying is unavailable in this browser.");
    }
  }

  async function activate(submittedCode: string): Promise<void> {
    setIsSubmitting(true);
    setErrorMessage(null);
    const result = await client.verifyTotpEnrollment({
      enrollmentId: enrollment.enrollment_id,
      code: submittedCode,
    });
    setIsSubmitting(false);
    if (result.ok) {
      setCode("");
      onCompleted(result.data);
      return;
    }
    const genericMessage = "Activation failed. Check the code and try again.";
    setErrorMessage(rateLimitedRetryMessage(result.error) ?? genericMessage);
  }

  async function skip(): Promise<void> {
    setIsSubmitting(true);
    await client.dismissInitialTotpOffer();
    setIsSubmitting(false);
    onSkipped?.();
  }

  return (
    <section aria-labelledby="totp-enrollment-heading">
      <h2 id="totp-enrollment-heading">Set up two-factor authentication</h2>
      <p>Scan the QR code with your authenticator app, or enter the secret manually.</p>
      {/* The QR is generated locally from the provisioning URI; nothing leaves the page. */}
      <div
        className="qr-code"
        // qrcode-generator returns trusted SVG markup it rendered itself.
        dangerouslySetInnerHTML={{ __html: qrSvgMarkup }}
      />
      <p>
        Secret: <code>{enrollment.secret}</code>
      </p>
      <button type="button" onClick={() => void copyValue(enrollment.secret)}>
        Copy secret
      </button>
      <button type="button" onClick={() => void copyValue(enrollment.provisioning_uri)}>
        Copy provisioning URI
      </button>
      {copyStatus !== null && (
        <p role="status">{copyStatus}</p>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!isSubmitting) {
            void activate(code.trim());
          }
        }}
        noValidate
      >
        <label htmlFor="totp-enrollment-code">Verification code</label>
        <input
          id="totp-enrollment-code"
          name="enrollment_code"
          ref={codeInputRef}
          autoComplete="one-time-code"
          inputMode="numeric"
          value={code}
          onChange={(event) => setCode(event.target.value)}
        />
        <button type="submit" disabled={isSubmitting}>
          Activate
        </button>
      </form>
      {!requireCompletion && onSkipped !== undefined && (
        <button type="button" disabled={isSubmitting} onClick={() => void skip()}>
          Skip for now
        </button>
      )}
      {errorMessage !== null && <ErrorAnnouncement message={errorMessage} />}
    </section>
  );
}
