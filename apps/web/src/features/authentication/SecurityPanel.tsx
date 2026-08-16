"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";

import type {
  AuthenticationClient,
  RecoveryCodesData,
  SessionData,
  TotpEnrollmentOfferData,
} from "../../api/authentication-client";
import { createBrowserAuthenticationClient } from "../../api/authentication-client";
import { browserSessionStore, type AuthenticationSessionStore } from "./session-store";
import { TotpEnrollmentOffer } from "./TotpChallenge";

type TotpSectionMode = "unknown" | "active-credential" | "enroll-offer" | "reauth-required";

export interface SecurityPanelProps {
  client?: AuthenticationClient;
  sessionStore?: AuthenticationSessionStore;
}

/**
 * The Security page surface (spec 18.4): password change, TOTP
 * enroll/replace/disable, recovery-code regeneration and logout. One-time
 * recovery codes live in component memory only and disappear on unmount.
 */
export function SecurityPanel({
  client = createBrowserAuthenticationClient(),
  sessionStore = browserSessionStore,
}: SecurityPanelProps): ReactNode {
  const router = useRouter();
  const [session, setSession] = useState<SessionData | null>(null);
  const [totpMode, setTotpMode] = useState<TotpSectionMode>("unknown");
  const [enrollment, setEnrollment] = useState<TotpEnrollmentOfferData | null>(null);
  const [regeneratedCodes, setRegeneratedCodes] = useState<RecoveryCodesData | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [noticeMessage, setNoticeMessage] = useState<string | null>(null);
  const [changeFields, setChangeFields] = useState({
    currentPassword: "",
    currentTotpCode: "",
    newPassword: "",
    confirmedPassword: "",
  });
  const [disableFields, setDisableFields] = useState({ password: "", totpCode: "" });
  const [regenerateFields, setRegenerateFields] = useState({ password: "", totpCode: "" });
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (errorMessage !== null) {
      errorRef.current?.focus();
    }
  }, [errorMessage]);

  const probeTotpSection = useCallback(
    (authenticatedSession: SessionData) => {
      void client.startTotpEnrollment().then((result) => {
        if (result.ok && result.data.enrollment !== null && result.data.enrollment !== undefined) {
          setEnrollment(result.data.enrollment);
          setTotpMode("enroll-offer");
          return;
        }
        if (!result.ok && result.error.code === "totp_enrollment_state_invalid") {
          setEnrollment(null);
          setTotpMode("active-credential");
          return;
        }
        if (!result.ok && result.error.code === "recent_authentication_required") {
          void authenticatedSession;
          setTotpMode("reauth-required");
          return;
        }
        setTotpMode("unknown");
      });
    },
    [client],
  );

  useEffect(() => {
    let cancelled = false;
    void client.getSession().then((result) => {
      if (cancelled) {
        return;
      }
      if (!result.ok || result.data.state !== "active") {
        router.replace("/login");
        return;
      }
      setSession(result.data);
      sessionStore.setSession(result.data);
      probeTotpSection(result.data);
    });
    return () => {
      cancelled = true;
    };
  }, [client, router, sessionStore, probeTotpSection]);

  async function changePassword(): Promise<void> {
    const { currentPassword, currentTotpCode, newPassword, confirmedPassword } = changeFields;
    if (newPassword !== confirmedPassword) {
      setErrorMessage("The new passwords do not match.");
      return;
    }
    if (newPassword.length < 15) {
      setErrorMessage("The new password must be at least 15 characters.");
      return;
    }
    const reauthenticated = await client.reauthenticate({
      password: currentPassword,
      totpCode: currentTotpCode.trim() === "" ? undefined : currentTotpCode.trim(),
    });
    if (!reauthenticated.ok) {
      setErrorMessage("Your current credentials were not accepted. Try again.");
      return;
    }
    const changed = await client.changePassword({ newPassword });
    if (!changed.ok) {
      setErrorMessage("The password change failed. Try again.");
      return;
    }
    setChangeFields({
      currentPassword: "",
      currentTotpCode: "",
      newPassword: "",
      confirmedPassword: "",
    });
    setErrorMessage(null);
    setNoticeMessage("Password changed. Other sessions were signed out.");
  }

  async function disableTotpCredential(): Promise<void> {
    const result = await client.disableTotp({
      password: disableFields.password,
      totpCode: disableFields.totpCode,
    });
    if (!result.ok || result.data.state !== "active") {
      setErrorMessage("Disabling two-factor authentication failed. Try again.");
      return;
    }
    setDisableFields({ password: "", totpCode: "" });
    setErrorMessage(null);
    setNoticeMessage("Two-factor authentication is disabled.");
    probeTotpSection(result.data);
  }

  async function regenerateCodes(): Promise<void> {
    const result = await client.regenerateTotpRecoveryCodes({
      password: regenerateFields.password,
      totpCode: regenerateFields.totpCode,
    });
    if (!result.ok) {
      setErrorMessage("Regenerating recovery codes failed. Try again.");
      return;
    }
    setRegenerateFields({ password: "", totpCode: "" });
    setErrorMessage(null);
    setRegeneratedCodes(result.data);
  }

  async function signOut(): Promise<void> {
    const result = await client.logout();
    sessionStore.clear();
    if (result.ok) {
      router.replace("/login");
      return;
    }
    router.replace("/login");
  }

  function submitHandler(action: () => Promise<void>): (event: FormEvent<HTMLFormElement>) => void {
    return (event) => {
      event.preventDefault();
      setErrorMessage(null);
      setNoticeMessage(null);
      void action();
    };
  }

  if (session === null) {
    return <p role="status">Loading security settings…</p>;
  }

  return (
    <div className="security-panel">
      {totpMode === "enroll-offer" && enrollment !== null && (
        <TotpEnrollmentOffer
          client={client}
          enrollment={enrollment}
          onCompleted={(codes) => {
            setEnrollment(null);
            setTotpMode("active-credential");
            setRegeneratedCodes(codes);
          }}
        />
      )}

      {totpMode === "reauth-required" && (
        <section aria-labelledby="totp-reauth-heading">
          <h2 id="totp-reauth-heading">Two-factor authentication</h2>
          <p>Confirm your password again to manage two-factor authentication.</p>
          <form
            onSubmit={submitHandler(async () => {
              const result = await client.reauthenticate({ password: changeFields.currentPassword });
              if (result.ok) {
                probeTotpSection(result.data);
              } else {
                setErrorMessage("Your current credentials were not accepted. Try again.");
              }
            })}
            noValidate
          >
            <label htmlFor="security-reauth-password">Current password</label>
            <input
              id="security-reauth-password"
              type="password"
              autoComplete="current-password"
              value={changeFields.currentPassword}
              onChange={(event) =>
                setChangeFields((fields) => ({ ...fields, currentPassword: event.target.value }))
              }
            />
            <button type="submit">Confirm password</button>
          </form>
        </section>
      )}

      {totpMode === "active-credential" && (
        <section aria-labelledby="totp-active-heading">
          <h2 id="totp-active-heading">Two-factor authentication</h2>
          <p>Two-factor authentication is active.</p>

          <form onSubmit={submitHandler(disableTotpCredential)} noValidate>
            <fieldset>
              <legend>Disable two-factor authentication</legend>
              <label htmlFor="disable-password">Disable password</label>
              <input
                id="disable-password"
                type="password"
                autoComplete="current-password"
                value={disableFields.password}
                onChange={(event) =>
                  setDisableFields((fields) => ({ ...fields, password: event.target.value }))
                }
              />
              <label htmlFor="disable-totp-code">Disable TOTP code</label>
              <input
                id="disable-totp-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={disableFields.totpCode}
                onChange={(event) =>
                  setDisableFields((fields) => ({ ...fields, totpCode: event.target.value }))
                }
              />
              <button type="submit">Disable two-factor authentication</button>
            </fieldset>
          </form>

          <form onSubmit={submitHandler(regenerateCodes)} noValidate>
            <fieldset>
              <legend>Recovery codes</legend>
              <label htmlFor="regenerate-password">Regenerate password</label>
              <input
                id="regenerate-password"
                type="password"
                autoComplete="current-password"
                value={regenerateFields.password}
                onChange={(event) =>
                  setRegenerateFields((fields) => ({ ...fields, password: event.target.value }))
                }
              />
              <label htmlFor="regenerate-totp-code">Regenerate TOTP code</label>
              <input
                id="regenerate-totp-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={regenerateFields.totpCode}
                onChange={(event) =>
                  setRegenerateFields((fields) => ({ ...fields, totpCode: event.target.value }))
                }
              />
              <button type="submit">Regenerate recovery codes</button>
            </fieldset>
          </form>
        </section>
      )}

      {regeneratedCodes !== null && (
        <section aria-labelledby="regenerated-codes-heading">
          <h2 id="regenerated-codes-heading">Recovery codes</h2>
          <p>These codes are shown only once. Store them somewhere safe.</p>
          <ul>
            {regeneratedCodes.codes.map((code) => (
              <li key={code}>
                <code>{code}</code>
              </li>
            ))}
          </ul>
          <button type="button" onClick={() => setRegeneratedCodes(null)}>
            I saved the codes
          </button>
        </section>
      )}

      <section aria-labelledby="password-heading">
        <h2 id="password-heading">Password</h2>
        <form onSubmit={submitHandler(changePassword)} noValidate>
          <label htmlFor="change-current-password">Current password</label>
          <input
            id="change-current-password"
            type="password"
            autoComplete="current-password"
            value={changeFields.currentPassword}
            onChange={(event) =>
              setChangeFields((fields) => ({ ...fields, currentPassword: event.target.value }))
            }
          />
          <label htmlFor="change-current-totp">Current TOTP code</label>
          <input
            id="change-current-totp"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={changeFields.currentTotpCode}
            onChange={(event) =>
              setChangeFields((fields) => ({ ...fields, currentTotpCode: event.target.value }))
            }
          />
          <label htmlFor="change-new-password">New password</label>
          <input
            id="change-new-password"
            type="password"
            autoComplete="new-password"
            value={changeFields.newPassword}
            onChange={(event) =>
              setChangeFields((fields) => ({ ...fields, newPassword: event.target.value }))
            }
          />
          <label htmlFor="change-confirm-password">Confirm new password</label>
          <input
            id="change-confirm-password"
            type="password"
            autoComplete="new-password"
            value={changeFields.confirmedPassword}
            onChange={(event) =>
              setChangeFields((fields) => ({ ...fields, confirmedPassword: event.target.value }))
            }
          />
          <button type="submit">Change password</button>
        </form>
      </section>

      <section aria-labelledby="session-heading">
        <h2 id="session-heading">Session</h2>
        <button type="button" onClick={() => void signOut()}>
          Sign out
        </button>
      </section>

      {errorMessage !== null && (
        <p ref={errorRef} role="alert" tabIndex={-1} className="error-message">
          {errorMessage}
        </p>
      )}
      {noticeMessage !== null && <p role="status">{noticeMessage}</p>}
    </div>
  );
}
