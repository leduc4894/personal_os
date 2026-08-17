"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useRouter } from "next/navigation";

import {
  createBrowserExclusionPolicyClient,
  type PolicyEditorClient,
} from "../../api/exclusion-policy-client";
import { PolicyPreview, type PolicyPreviewPanelState } from "./PolicyPreview";
import { PolicyPublishDialog } from "./PolicyPublishDialog";
import { PolicyStatus } from "./PolicyStatus";
import {
  buildDraftReplaceRequest,
  countDraftChanges,
  draftFromStatus,
  isPreviewPublishable,
  MAXIMUM_RULES_PER_REVISION,
  operandTextFromRuleData,
  policyLoadFailureMessage,
  draftSaveFailureMessage,
  PREVIEW_MAXIMUM_POLLS,
  PREVIEW_POLL_INTERVAL_MS,
  previewFailureMessage,
  previewStatusMessage,
  RULE_KINDS,
  RULE_KIND_LABELS,
  SOURCE_TYPE_OPTIONS,
  validateDraft,
  type PolicyAdminState,
  type PolicyDraft,
  type PolicyDraftRuleInput,
  type PolicyPreviewCursorData,
  type PolicyPublicationData,
  type PolicySafeErrorCode,
  type PolicyStatusData,
  type RuleKind,
} from "./policy-models";

export interface PolicyEditorProps {
  client?: PolicyEditorClient;
  /** Bounded polling interval; tests inject short intervals. */
  pollIntervalMs?: number;
}

type EditorNotice =
  | { kind: "success"; text: string }
  | { kind: "error"; text: string }
  | { kind: "conflict"; text: string };

const OPERAND_LABELS: Readonly<Record<RuleKind, string>> = {
  exact_source_id: "Source ID",
  folder_prefix: "Folder prefix",
  path_glob: "Path glob",
  extension: "Extension",
  media_type: "Media type",
  maximum_size: "Maximum size (bytes)",
  source_type: "Source type",
};

const STALE_PREVIEW_MESSAGE =
  "The preview no longer matches the saved draft or the active revision. Start a new preview.";

function createRuleId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function toSafeErrorCode(code: string): PolicySafeErrorCode {
  const closed: readonly string[] = [
    "exclusion_policy_input_invalid",
    "exclusion_policy_not_initialized",
    "exclusion_policy_draft_conflict",
    "exclusion_policy_preview_pending",
    "exclusion_policy_preview_failed",
    "exclusion_policy_preview_expired",
    "exclusion_policy_preview_stale",
    "exclusion_policy_confirmation_invalid",
    "exclusion_policy_denied",
    "exclusion_policy_indeterminate",
    "exclusion_policy_snapshot_outdated",
    "exclusion_policy_signing_unavailable",
    "exclusion_policy_commit_outcome_unknown",
    "recent_authentication_required",
    "authentication_required",
    "csrf_validation_failed",
    "internal_error",
  ];
  return closed.includes(code) ? (code as PolicySafeErrorCode) : "internal_error";
}

/**
 * The Admin policy surface (spec 17): it owns the closed state machine —
 * local draft edits stay local until an explicit save, the server draft
 * version is the concurrency token, previews poll only while pending and
 * publication is gated on the ready preview exactly matching the saved draft
 * and the current active revision. Preview rows are rendered as escaped text
 * only and never persisted to web storage.
 */
export function PolicyEditor({
  client = createBrowserExclusionPolicyClient(),
  pollIntervalMs = PREVIEW_POLL_INTERVAL_MS,
}: PolicyEditorProps): ReactNode {
  const router = useRouter();
  const [adminState, setAdminState] = useState<PolicyAdminState>({ kind: "loading" });
  const [status, setStatus] = useState<PolicyStatusData | null>(null);
  const [savedDraft, setSavedDraft] = useState<PolicyDraft | null>(null);
  const [localRules, setLocalRules] = useState<readonly PolicyDraftRuleInput[]>([]);
  const [editedRuleIds, setEditedRuleIds] = useState<ReadonlySet<string>>(new Set());
  const [selectedKind, setSelectedKind] = useState<RuleKind>("exact_source_id");
  const [notice, setNotice] = useState<EditorNotice | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [isCreatingPreview, setIsCreatingPreview] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [isPublishDialogOpen, setIsPublishDialogOpen] = useState(false);
  const [lastPublication, setLastPublication] = useState<PolicyPublicationData | null>(null);
  const alertRef = useRef<HTMLParagraphElement>(null);

  const savedDraftRef = useRef<PolicyDraft | null>(null);
  const statusRef = useRef<PolicyStatusData | null>(null);

  useEffect(() => {
    savedDraftRef.current = savedDraft;
  }, [savedDraft]);

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  const localDraft = useMemo<PolicyDraft | null>(
    () => (savedDraft === null ? null : { ...savedDraft, rules: localRules }),
    [savedDraft, localRules],
  );
  const validation = useMemo(
    () => (localDraft === null ? null : validateDraft(localDraft, editedRuleIds)),
    [localDraft, editedRuleIds],
  );
  const changes = useMemo(
    () => (localDraft === null || savedDraft === null ? null : countDraftChanges(localDraft, savedDraft)),
    [localDraft, savedDraft],
  );

  useEffect(() => {
    if (notice !== null && notice.kind !== "success") {
      alertRef.current?.focus();
    }
  }, [notice]);

  const applyStatus = useCallback((data: PolicyStatusData): void => {
    const draft = draftFromStatus(data);
    setStatus(data);
    setSavedDraft(draft);
    setLocalRules(draft.rules);
    setEditedRuleIds(new Set());
    setAdminState({ kind: "editing", draft, status: data });
  }, []);

  useEffect(() => {
    let cancelled = false;
    void client.getExclusionPolicyStatus().then((result) => {
      if (cancelled) {
        return;
      }
      if (!result.ok) {
        if (result.error.code === "authentication_required") {
          router.replace("/login");
          return;
        }
        setNotice({ kind: "error", text: policyLoadFailureMessage(result.error.code) });
        setAdminState({ kind: "failed", errorCode: toSafeErrorCode(result.error.code) });
        return;
      }
      applyStatus(result.data);
    });
    return () => {
      cancelled = true;
    };
  }, [applyStatus, client, router]);

  async function loadStatus(): Promise<void> {
    const result = await client.getExclusionPolicyStatus();
    if (!result.ok) {
      if (result.error.code === "authentication_required") {
        router.replace("/login");
        return;
      }
      setNotice({ kind: "error", text: policyLoadFailureMessage(result.error.code) });
      setAdminState({ kind: "failed", errorCode: toSafeErrorCode(result.error.code) });
      return;
    }
    applyStatus(result.data);
  }

  const previewId = adminState.kind === "previewing" ? adminState.previewId : null;

  useEffect(() => {
    if (previewId === null) {
      return;
    }
    let cancelled = false;
    let polls = 0;
    const activePreviewId: string = previewId;
    async function pollPreview(): Promise<void> {
      if (cancelled) {
        return;
      }
      polls += 1;
      if (polls > PREVIEW_MAXIMUM_POLLS) {
        stopPolling("The preview did not finish in time. Start a new preview.");
        return;
      }
      const result = await client.getExclusionPolicyPreview({ policyPreviewId: activePreviewId });
      if (cancelled) {
        return;
      }
      if (result.ok) {
        const data = result.data;
        if (data.status === "pending" || data.status === "leased" || data.status === "running") {
          return;
        }
        if (data.status === "ready") {
          const saved = savedDraftRef.current;
          const currentStatus = statusRef.current;
          if (saved !== null && currentStatus !== null && isPreviewPublishable(data, saved, currentStatus)) {
            setAdminState({
              kind: "publishable",
              draft: saved,
              preview: {
                preview: data,
                rows: data.results ?? [],
                hasMore: data.next_cursor !== null && data.next_cursor !== undefined,
              },
            });
          } else {
            stopPolling(STALE_PREVIEW_MESSAGE);
          }
          return;
        }
        stopPolling(previewStatusMessage(data.status));
        return;
      }
      stopPolling(previewFailureMessage(result.error.code));
    }
    function stopPolling(message: string): void {
      if (cancelled) {
        return;
      }
      cancelled = true;
      window.clearInterval(timer);
      const saved = savedDraftRef.current;
      const currentStatus = statusRef.current;
      if (saved !== null && currentStatus !== null) {
        setAdminState({ kind: "editing", draft: saved, status: currentStatus });
      } else {
        setAdminState({ kind: "failed", errorCode: "internal_error" });
      }
      setNotice({ kind: "error", text: message });
    }
    const timer = window.setInterval(() => {
      void pollPreview();
    }, pollIntervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [previewId, client, pollIntervalMs]);

  function updateRuleOperand(ruleId: string, operandText: string): void {
    setLocalRules((rules) =>
      rules.map((rule) => (rule.rule_id === ruleId ? { ...rule, operandText } : rule)),
    );
    setEditedRuleIds((previous) => {
      const next = new Set(previous);
      next.add(ruleId);
      return next;
    });
  }

  function addRule(): void {
    if (localRules.length >= MAXIMUM_RULES_PER_REVISION) {
      return;
    }
    setLocalRules((rules) => [
      ...rules,
      {
        rule_id: createRuleId(),
        rule_kind: selectedKind,
        operandText: selectedKind === "source_type" ? SOURCE_TYPE_OPTIONS[0]! : "",
      },
    ]);
  }

  function moveRule(index: number, offset: -1 | 1): void {
    setLocalRules((rules) => {
      const targetIndex = index + offset;
      if (targetIndex < 0 || targetIndex >= rules.length) {
        return rules;
      }
      const next = [...rules];
      const [moved] = next.splice(index, 1);
      next.splice(targetIndex, 0, moved!);
      return next;
    });
  }

  function removeRule(ruleId: string): void {
    setLocalRules((rules) => rules.filter((rule) => rule.rule_id !== ruleId));
  }

  function handleSave(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (
      localDraft === null ||
      savedDraft === null ||
      status === null ||
      validation === null ||
      !validation.isSaveable ||
      isSaving ||
      adminState.kind !== "editing"
    ) {
      return;
    }
    void (async () => {
      setIsSaving(true);
      setNotice(null);
      const result = await client.replaceExclusionPolicyDraft({
        expectedDraftVersion: savedDraft.draft_version,
        rules: buildDraftReplaceRequest(localDraft).rules,
      });
      setIsSaving(false);
      if (result.ok) {
        const nextSaved: PolicyDraft = {
          base_policy_revision_id: result.data.base_policy_revision_id,
          draft_id: result.data.draft_id,
          draft_version: result.data.draft_version,
          rules: result.data.rules.map((rule) => ({
            rule_id: rule.rule_id,
            rule_kind: rule.rule_kind,
            operandText: operandTextFromRuleData(rule),
          })),
        };
        setSavedDraft(nextSaved);
        setLocalRules(nextSaved.rules);
        setEditedRuleIds(new Set());
        setAdminState({ kind: "editing", draft: nextSaved, status });
        setNotice({ kind: "success", text: "Draft saved." });
        return;
      }
      if (result.error.code === "exclusion_policy_draft_conflict") {
        setNotice({ kind: "conflict", text: draftSaveFailureMessage(result.error.code) });
        return;
      }
      setNotice({ kind: "error", text: draftSaveFailureMessage(result.error.code) });
    })();
  }

  function handleCreatePreview(): void {
    if (savedDraft === null || status === null || isCreatingPreview || isSaving) {
      return;
    }
    void (async () => {
      setIsCreatingPreview(true);
      setNotice(null);
      const result = await client.createExclusionPolicyPreview();
      setIsCreatingPreview(false);
      if (!result.ok) {
        setNotice({ kind: "error", text: previewFailureMessage(result.error.code) });
        return;
      }
      const data = result.data;
      if (data.status === "pending" || data.status === "leased" || data.status === "running") {
        setAdminState({ kind: "previewing", draft: savedDraft, previewId: data.policy_preview_id });
        return;
      }
      if (data.status === "ready" && isPreviewPublishable(data, savedDraft, status)) {
        setAdminState({
          kind: "publishable",
          draft: savedDraft,
          preview: {
            preview: data,
            rows: data.results ?? [],
            hasMore: data.next_cursor !== null && data.next_cursor !== undefined,
          },
        });
        return;
      }
      setNotice({
        kind: "error",
        text: data.status === "ready" ? STALE_PREVIEW_MESSAGE : previewStatusMessage(data.status),
      });
    })();
  }

  function handleLoadMore(cursor: PolicyPreviewCursorData): void {
    if (adminState.kind !== "publishable" || isLoadingMore) {
      return;
    }
    void (async () => {
      setIsLoadingMore(true);
      const result = await client.getExclusionPolicyPreview({
        policyPreviewId: adminState.preview.preview.policy_preview_id,
        cursor,
      });
      setIsLoadingMore(false);
      if (!result.ok) {
        setNotice({ kind: "error", text: previewFailureMessage(result.error.code) });
        return;
      }
      if (result.data.status !== "ready") {
        setNotice({ kind: "error", text: previewStatusMessage(result.data.status) });
        return;
      }
      setAdminState((previous) => {
        if (previous.kind !== "publishable") {
          return previous;
        }
        return {
          kind: "publishable",
          draft: previous.draft,
          preview: {
            preview: result.data,
            rows: [...previous.preview.rows, ...(result.data.results ?? [])],
            hasMore: result.data.next_cursor !== null && result.data.next_cursor !== undefined,
          },
        };
      });
    })();
  }

  function handlePublished(result: PolicyPublicationData): void {
    setLastPublication(result);
    setIsPublishDialogOpen(false);
    setNotice({
      kind: "success",
      text: `Published revision ${result.revision_number}${
        result.is_replay ? " (exact replay of an already committed publication)" : ""
      }. Reconciliation continues in the background.`,
    });
    void (async () => {
      const refreshed = await client.getExclusionPolicyStatus();
      if (!refreshed.ok) {
        setNotice({
          kind: "error",
          text: "The policy could not be reloaded. Reload the page to see the committed state.",
        });
        return;
      }
      applyStatus(refreshed.data);
    })();
  }

  if (adminState.kind === "failed") {
    return (
      <div className="policy-admin policy-admin-failed">
        <p ref={alertRef} role="alert" tabIndex={-1} className="error-message">
          {notice?.text ?? policyLoadFailureMessage(adminState.errorCode)}
        </p>
        <button type="button" onClick={() => void loadStatus()}>
          Try again
        </button>
      </div>
    );
  }

  if (adminState.kind === "loading" || status === null || savedDraft === null || localDraft === null) {
    return (
      <p role="status" aria-label="Loading policy">
        Loading policy…
      </p>
    );
  }

  const isEditingLocked = adminState.kind !== "editing";
  const hasUnsavedChanges = changes?.hasUnsavedChanges ?? false;
  const isPreviewDisabled =
    isEditingLocked ||
    isCreatingPreview ||
    isSaving ||
    hasUnsavedChanges ||
    validation === null ||
    !validation.isSaveable ||
    notice?.kind === "conflict";
  const isSaveDisabled =
    isEditingLocked || isSaving || validation === null || !validation.isSaveable;

  let previewPanelState: PolicyPreviewPanelState | null = null;
  if (adminState.kind === "previewing") {
    previewPanelState = { kind: "in-flight" };
  } else if (adminState.kind === "publishable") {
    previewPanelState = {
      kind: "ready",
      preview: adminState.preview.preview,
      rows: adminState.preview.rows,
      hasMore: adminState.preview.hasMore,
    };
  }

  return (
    <div className="policy-admin">
      <PolicyStatus status={status} lastPublication={lastPublication} />
      {notice !== null && notice.kind === "success" && (
        <p role="status" className="success-message">
          {notice.text}
        </p>
      )}
      {notice !== null && notice.kind === "error" && (
        <p ref={alertRef} role="alert" tabIndex={-1} className="error-message">
          {notice.text}
        </p>
      )}
      {notice !== null && notice.kind === "conflict" && (
        <div role="alert" className="conflict-message">
          <p>{notice.text}</p>
          <button
            type="button"
            onClick={() => {
              setNotice(null);
              void loadStatus();
            }}
          >
            Reload draft
          </button>
        </div>
      )}
      <form onSubmit={handleSave} noValidate aria-labelledby="policy-draft-heading">
        <h2 id="policy-draft-heading">Draft rules</h2>
        <p>
          Draft version {savedDraft.draft_version}
          {hasUnsavedChanges && changes !== null
            ? ` · You have unsaved changes: ${changes.added} added, ${changes.removed} removed, ${changes.changed} changed.`
            : ""}
        </p>
        <ul className="policy-rules">
          {localDraft.rules.map((rule, index) => {
            const issue = validation?.rowIssues.get(rule.rule_id) ?? null;
            const operandInputId = `rule-${rule.rule_id}-operand`;
            return (
              <li key={rule.rule_id}>
                <fieldset>
                  <legend>
                    Rule {index + 1}: {RULE_KIND_LABELS[rule.rule_kind]}
                  </legend>
                  {rule.rule_kind === "source_type" ? (
                    <select
                      id={operandInputId}
                      value={rule.operandText}
                      disabled={isEditingLocked}
                      onChange={(event) => updateRuleOperand(rule.rule_id, event.target.value)}
                      aria-label={OPERAND_LABELS.source_type}
                    >
                      {SOURCE_TYPE_OPTIONS.map((sourceType) => (
                        <option key={sourceType} value={sourceType}>
                          {sourceType}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <>
                      <label htmlFor={operandInputId}>{OPERAND_LABELS[rule.rule_kind]}</label>
                      <input
                        id={operandInputId}
                        type={rule.rule_kind === "maximum_size" ? "number" : "text"}
                        inputMode={rule.rule_kind === "maximum_size" ? "numeric" : undefined}
                        autoComplete="off"
                        value={rule.operandText}
                        disabled={isEditingLocked}
                        onChange={(event) => updateRuleOperand(rule.rule_id, event.target.value)}
                      />
                    </>
                  )}
                  <button
                    type="button"
                    aria-label={`Move rule ${index + 1} up`}
                    disabled={isEditingLocked || index === 0}
                    onClick={() => moveRule(index, -1)}
                  >
                    Move up
                  </button>
                  <button
                    type="button"
                    aria-label={`Move rule ${index + 1} down`}
                    disabled={isEditingLocked || index === localDraft.rules.length - 1}
                    onClick={() => moveRule(index, 1)}
                  >
                    Move down
                  </button>
                  <button
                    type="button"
                    aria-label={`Remove rule ${index + 1}`}
                    disabled={isEditingLocked}
                    onClick={() => removeRule(rule.rule_id)}
                  >
                    Remove
                  </button>
                  {issue !== null && (
                    <p role="alert" className="error-message">
                      {issue}
                    </p>
                  )}
                </fieldset>
              </li>
            );
          })}
        </ul>
        <label htmlFor="add-rule-kind">Rule kind</label>
        <select
          id="add-rule-kind"
          value={selectedKind}
          disabled={isEditingLocked || localDraft.rules.length >= MAXIMUM_RULES_PER_REVISION}
          onChange={(event) => setSelectedKind(event.target.value as RuleKind)}
        >
          {RULE_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {RULE_KIND_LABELS[kind]}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={addRule}
          disabled={isEditingLocked || localDraft.rules.length >= MAXIMUM_RULES_PER_REVISION}
        >
          Add rule
        </button>
        <button type="submit" disabled={isSaveDisabled}>
          Save draft
        </button>
        <button
          type="button"
          onClick={handleCreatePreview}
          disabled={isPreviewDisabled}
        >
          Preview impact
        </button>
      </form>
      {previewPanelState !== null && (
        <PolicyPreview
          state={previewPanelState}
          isLoadingMore={isLoadingMore}
          onLoadMore={handleLoadMore}
          onPublish={adminState.kind === "publishable" ? () => setIsPublishDialogOpen(true) : undefined}
          isPublishEnabled={adminState.kind === "publishable"}
        />
      )}
      {isPublishDialogOpen && adminState.kind === "publishable" && (
        <PolicyPublishDialog
          client={client}
          draft={savedDraft}
          preview={adminState.preview.preview}
          status={status}
          onClosed={() => setIsPublishDialogOpen(false)}
          onPublished={handlePublished}
        />
      )}
    </div>
  );
}
