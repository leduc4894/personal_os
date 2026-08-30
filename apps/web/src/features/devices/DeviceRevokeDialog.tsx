"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import { rateLimitedRetryMessage } from "../authentication/rate-limit-copy";
import type { AdminDeviceData, DeviceAdministrationClient } from "./device-administration-client";

type DialogMode = "confirm" | "reauth-required";

const GENERIC_REVOKE_FAILURE = "Revoking the device failed. Try again.";
const CONFIRMATION_MISMATCH_MESSAGE =
  "The device name did not match. Check the exact name and try again.";

const FOCUSABLE_CHILD_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function focusableChildren(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_CHILD_SELECTOR));
}

export interface DeviceRevokeDialogProps {
  client: DeviceAdministrationClient;
  device: AdminDeviceData;
  onClosed: () => void;
  onRevoked: () => void;
}

/**
 * The Admin revocation guard chain (spec 14.1, 18.3): the exact device-name
 * confirmation gates the request client-side, and the server's recent
 * re-authentication requirement and confirmation conflicts surface inline.
 */
export function DeviceRevokeDialog({
  client,
  device,
  onClosed,
  onRevoked,
}: DeviceRevokeDialogProps): ReactNode {
  const [mode, setMode] = useState<DialogMode>("confirm");
  const [confirmation, setConfirmation] = useState("");
  const [reauthFields, setReauthFields] = useState({ password: "", totpCode: "" });
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  // Captured before the heading effect moves focus into the dialog so the
  // opener element can be restored when the dialog unmounts.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      opener?.focus();
    };
  }, []);

  // Minimal modal focus trap: Tab from the last focusable child cycles to the
  // first, Shift+Tab inverts, and focus that escaped is pulled back in.
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key !== "Tab" || event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }
      const dialog = dialogRef.current;
      if (dialog === null) {
        return;
      }
      const focusableElements = focusableChildren(dialog);
      const firstChild = focusableElements[0];
      const lastChild = focusableElements[focusableElements.length - 1];
      if (firstChild === undefined || lastChild === undefined) {
        return;
      }
      const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const isFocusInsideDialog = activeElement !== null && dialog.contains(activeElement);
      if (event.shiftKey) {
        if (!isFocusInsideDialog || activeElement === firstChild) {
          event.preventDefault();
          lastChild.focus();
        }
        return;
      }
      if (!isFocusInsideDialog || activeElement === lastChild) {
        event.preventDefault();
        firstChild.focus();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  useEffect(() => {
    headingRef.current?.focus();
  }, [mode]);

  useEffect(() => {
    if (errorMessage !== null) {
      errorRef.current?.focus();
    }
  }, [errorMessage]);

  function requestRevocation(deviceNameConfirmation: string): void {
    void (async () => {
      setIsSubmitting(true);
      setErrorMessage(null);
      const result = await client.revokeAdminDevice({
        deviceId: device.device_id,
        deviceNameConfirmation,
      });
      setIsSubmitting(false);
      if (result.ok) {
        onRevoked();
        return;
      }
      const errorCode = result.error.code;
      if (errorCode === "recent_authentication_required") {
        setErrorMessage(null);
        setMode("reauth-required");
        return;
      }
      if (errorCode === "device_revocation_confirmation_invalid") {
        setErrorMessage(CONFIRMATION_MISMATCH_MESSAGE);
        return;
      }
      setErrorMessage(rateLimitedRetryMessage(result.error) ?? GENERIC_REVOKE_FAILURE);
    })();
  }

  function handleConfirmation(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (isSubmitting || confirmation !== device.device_name) {
      return;
    }
    requestRevocation(confirmation);
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
      setReauthFields({ password: "", totpCode: "" });
      await requestRevocation(confirmation);
    })();
  }

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="device-revoke-heading"
      className="device-revoke-dialog"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          onClosed();
        }
      }}
    >
      <h2 id="device-revoke-heading" ref={headingRef} tabIndex={-1}>
        Revoke device
      </h2>
      <p>
        Revoking <strong>{device.device_name}</strong> permanently stops its sync access. The device
        must be authorized again through its plugin to return.
      </p>
      {mode === "confirm" ? (
        <form onSubmit={handleConfirmation} noValidate>
          <label htmlFor="device-revoke-confirmation">Type the device name to confirm</label>
          <input
            id="device-revoke-confirmation"
            name="device_name_confirmation"
            autoComplete="off"
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />
          <button type="submit" disabled={isSubmitting || confirmation !== device.device_name}>
            Revoke device
          </button>
          <button type="button" onClick={onClosed}>
            Cancel
          </button>
        </form>
      ) : (
        <form onSubmit={handleReauthentication} noValidate>
          <p>Confirm your password again to revoke this device.</p>
          <label htmlFor="device-revoke-reauth-password">Current password</label>
          <input
            id="device-revoke-reauth-password"
            type="password"
            autoComplete="current-password"
            value={reauthFields.password}
            onChange={(event) =>
              setReauthFields((fields) => ({ ...fields, password: event.target.value }))
            }
          />
          <label htmlFor="device-revoke-reauth-totp">Authentication code</label>
          <input
            id="device-revoke-reauth-totp"
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
          <button type="button" onClick={onClosed}>
            Cancel
          </button>
        </form>
      )}
      {errorMessage !== null && (
        <p ref={errorRef} role="alert" tabIndex={-1} className="error-message">
          {errorMessage}
        </p>
      )}
    </div>
  );
}
