"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import type { PolicyPublicationTriggerClient } from "../../api/exclusion-policy-client";
import {
  PUBLISH_CONFIRMATION_PHRASE,
  publishFailureMessage,
  type PolicyDraft,
  type PolicyPreviewData,
  type PolicyPublicationData,
  type PolicyStatusData,
} from "./policy-models";

type DialogMode = "confirm" | "reauth-required";

export interface PolicyPublishDialogProps {
  client: PolicyPublicationTriggerClient;
  /** The saved draft; its version is the concurrency token being published. */
  draft: PolicyDraft;
  /** The ready preview the publication is bound to. */
  preview: PolicyPreviewData;
  status: PolicyStatusData;
  onClosed: () => void;
  onPublished: (result: PolicyPublicationData) => void;
}

const GENERIC_REAUTH_FAILURE = "Your current credentials were not accepted. Try again.";

/** One printable opaque idempotency key of 1–200 ASCII characters (spec 11). */
function createIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * The exact typed publication confirmation (spec 11/17): the dialog shows the
 * precise binding — expected active revision, draft identity/version/hash,
 * preview identity, impact digest and the source checkpoint — requires the
 * exact phrase, reuses one idempotency key across retries so a replay
 * resolves to the committed result, and renders only closed failure copy.
 */
export function PolicyPublishDialog({
  client,
  draft,
  preview,
  status,
  onClosed,
  onPublished,
}: PolicyPublishDialogProps): ReactNode {
  const [mode, setMode] = useState<DialogMode>("confirm");
  const [confirmation, setConfirmation] = useState("");
  const [reauthFields, setReauthFields] = useState({ password: "", totpCode: "" });
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPublished, setIsPublished] = useState(false);
  const idempotencyKeyRef = useRef<string>(createIdempotencyKey());
  const headingRef = useRef<HTMLHeadingElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, [mode]);

  useEffect(() => {
    if (errorMessage !== null) {
      errorRef.current?.focus();
    }
  }, [errorMessage]);

  function requestPublication(): void {
    void (async () => {
      setIsSubmitting(true);
      setErrorMessage(null);
      const result = await client.publishExclusionPolicy({
        request: {
          confirmation: PUBLISH_CONFIRMATION_PHRASE,
          expected_active_policy_revision_id: status.active_policy_revision_id,
          expected_active_revision_number: status.active_revision_number,
          expected_draft_sha256: preview.draft_sha256,
          expected_draft_version: draft.draft_version,
          policy_draft_id: draft.draft_id,
          policy_preview_id: preview.policy_preview_id,
          preview_impact_digest: preview.impact_digest ?? "",
        },
        idempotencyKey: idempotencyKeyRef.current,
      });
      setIsSubmitting(false);
      if (result.ok) {
        setIsPublished(true);
        onPublished(result.data);
        return;
      }
      if (result.error.code === "recent_authentication_required") {
        setErrorMessage(null);
        setMode("reauth-required");
        return;
      }
      setErrorMessage(publishFailureMessage(result.error.code));
    })();
  }

  function handleConfirmation(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (isSubmitting || confirmation !== PUBLISH_CONFIRMATION_PHRASE) {
      return;
    }
    requestPublication();
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
        setErrorMessage(GENERIC_REAUTH_FAILURE);
        return;
      }
      setReauthFields({ password: "", totpCode: "" });
      requestPublication();
    })();
  }

  if (isPublished) {
    return null;
  }

  const counters = preview.counters;
  const unchangedCount = counters.still_allowed_count + counters.still_excluded_count;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="policy-publish-heading"
      className="policy-publish-dialog"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          onClosed();
        }
      }}
    >
      <h2 id="policy-publish-heading" ref={headingRef} tabIndex={-1}>
        Publish exclusion policy
      </h2>
      <p>
        This publishes revision {status.active_revision_number} → {status.active_revision_number + 1}{" "}
        bound to:
      </p>
      <dl>
        <div>
          <dt>Binding</dt>
          <dd>
            Draft version {draft.draft_version} (SHA-256 <code>{preview.draft_sha256}</code>)
          </dd>
        </div>
        <div>
          <dt>Preview</dt>
          <dd>
            <code>{preview.policy_preview_id}</code>
          </dd>
        </div>
        <div>
          <dt>Source checkpoint</dt>
          <dd>checkpoint {preview.source_checkpoint_event_sequence}</dd>
        </div>
        <div>
          <dt>Impact</dt>
          <dd>
            Newly excluded {counters.newly_excluded_count} · Newly allowed {counters.newly_allowed_count}{" "}
            · Unchanged {unchangedCount} · Indeterminate {counters.indeterminate_count}
          </dd>
        </div>
      </dl>
      {mode === "confirm" ? (
        <form onSubmit={handleConfirmation} noValidate>
          <p id="policy-publish-phrase-hint">
            Type {PUBLISH_CONFIRMATION_PHRASE} exactly. Publication rechecks the saved draft and the
            active policy revision before committing.
          </p>
          <label htmlFor="policy-publish-confirmation">
            Type {PUBLISH_CONFIRMATION_PHRASE} to confirm
          </label>
          <input
            id="policy-publish-confirmation"
            name="publication_confirmation"
            autoComplete="off"
            aria-describedby="policy-publish-phrase-hint"
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />
          <button type="submit" disabled={isSubmitting || confirmation !== PUBLISH_CONFIRMATION_PHRASE}>
            Publish policy
          </button>
          <button type="button" onClick={onClosed}>
            Cancel
          </button>
        </form>
      ) : (
        <form onSubmit={handleReauthentication} noValidate>
          <p>Confirm your password again to publish. The same publication request is retried.</p>
          <label htmlFor="policy-publish-reauth-password">Current password</label>
          <input
            id="policy-publish-reauth-password"
            type="password"
            autoComplete="current-password"
            value={reauthFields.password}
            onChange={(event) =>
              setReauthFields((fields) => ({ ...fields, password: event.target.value }))
            }
          />
          <label htmlFor="policy-publish-reauth-totp">Authentication code</label>
          <input
            id="policy-publish-reauth-totp"
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
