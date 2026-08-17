import type { components } from "@workspace/api-client";

/**
 * Pure browser-side models of the exclusion-policy Admin surface (spec 6, 9,
 * 10, 11, 17): the editable draft, the closed rule grammar mirrored from the
 * backend normalization contract, the preview/publication gating rules and
 * the safe failure copy registry. Nothing here touches the network, storage
 * or React; every function is deterministic so the components stay thin.
 */

export type PolicyStatusData = components["schemas"]["ExclusionPolicyStatusData"];
export type PolicyDraftData = components["schemas"]["PolicyDraftData"];
export type PolicyRuleData = components["schemas"]["PolicyRuleData"];
export type RuleKind = components["schemas"]["RuleKind"];
export type PolicyPreviewData = components["schemas"]["PolicyPreviewData"];
export type PolicyPreviewResultRowData = components["schemas"]["PolicyPreviewResultRowData"];
export type PolicyPreviewCursorData = components["schemas"]["PolicyPreviewCursorData"];
export type PolicyPublicationData = components["schemas"]["PolicyPublicationData"];
export type PolicyDraftRuleRequest = components["schemas"]["PolicyDraftRuleRequest"];
export type PolicyDraftReplaceRequest = components["schemas"]["PolicyDraftReplaceRequest"];

/** The seven closed rule kinds of spec 6.2, in canonical selector order. */
export const RULE_KINDS: readonly RuleKind[] = [
  "exact_source_id",
  "folder_prefix",
  "path_glob",
  "extension",
  "media_type",
  "maximum_size",
  "source_type",
] as const;

export const RULE_KIND_LABELS: Readonly<Record<RuleKind, string>> = {
  exact_source_id: "Exact source ID",
  folder_prefix: "Folder prefix",
  path_glob: "Path glob",
  extension: "Extension",
  media_type: "Media type",
  maximum_size: "Maximum size",
  source_type: "Source type",
};

/** The closed ``SourceType`` vocabulary a source-type rule may name. */
export const SOURCE_TYPE_OPTIONS: readonly string[] = [
  "markdown",
  "text",
  "pdf",
  "image",
  "audio",
  "web",
  "youtube",
] as const;

export const MAXIMUM_RULES_PER_REVISION = 256;
export const MAXIMUM_SIZE_BYTES_CEILING = 104_857_600;

/** Spec 11: the exact typed publication confirmation phrase. */
export const PUBLISH_CONFIRMATION_PHRASE = "PUBLISH EXCLUSION POLICY";

/** Bounded preview polling: 2 seconds between reads, capped by the deadline. */
export const PREVIEW_POLL_INTERVAL_MS = 2_000;
export const PREVIEW_MAXIMUM_POLLS = 450;

/** One editable draft rule: a stable UUID plus its kind and operand text. */
export interface PolicyDraftRuleInput {
  readonly rule_id: string;
  readonly rule_kind: RuleKind;
  readonly operandText: string;
}

/**
 * The local draft model. ``draft_version`` is the server concurrency token:
 * it changes only through an explicit successful save.
 */
export interface PolicyDraft {
  readonly draft_id: string;
  readonly draft_version: number;
  readonly base_policy_revision_id: string | null;
  readonly rules: readonly PolicyDraftRuleInput[];
}

/** A ready preview plus the result pages accumulated so far. */
export interface ReadyPolicyPreview {
  readonly preview: PolicyPreviewData;
  readonly rows: readonly PolicyPreviewResultRowData[];
  readonly hasMore: boolean;
}

/** The closed safe error vocabulary this surface can terminate on. */
export type PolicySafeErrorCode =
  | "exclusion_policy_input_invalid"
  | "exclusion_policy_not_initialized"
  | "exclusion_policy_draft_conflict"
  | "exclusion_policy_preview_pending"
  | "exclusion_policy_preview_failed"
  | "exclusion_policy_preview_expired"
  | "exclusion_policy_preview_stale"
  | "exclusion_policy_confirmation_invalid"
  | "exclusion_policy_denied"
  | "exclusion_policy_indeterminate"
  | "exclusion_policy_snapshot_outdated"
  | "exclusion_policy_signing_unavailable"
  | "exclusion_policy_commit_outcome_unknown"
  | "recent_authentication_required"
  | "authentication_required"
  | "csrf_validation_failed"
  | "internal_error";

/** The Admin state machine of spec 17, exactly as planned. */
export type PolicyAdminState =
  | { kind: "loading" }
  | { kind: "editing"; draft: PolicyDraft; status: PolicyStatusData }
  | { kind: "previewing"; draft: PolicyDraft; previewId: string }
  | { kind: "publishable"; draft: PolicyDraft; preview: ReadyPolicyPreview }
  | { kind: "failed"; errorCode: PolicySafeErrorCode };

/** Builds the local editable draft from the Admin status read. */
export function draftFromStatus(status: PolicyStatusData): PolicyDraft {
  return {
    base_policy_revision_id: status.draft.base_policy_revision_id,
    draft_id: status.draft.draft_id,
    draft_version: status.draft.draft_version,
    rules: status.draft.rules.map((rule) => ({
      rule_id: rule.rule_id,
      rule_kind: rule.rule_kind,
      operandText: operandTextFromRuleData(rule),
    })),
  };
}

/** Extracts the single populated operand of one rendered draft rule. */
export function operandTextFromRuleData(rule: PolicyRuleData): string {
  switch (rule.rule_kind) {
    case "exact_source_id":
      return rule.source_id ?? "";
    case "folder_prefix":
      return rule.folder_prefix ?? "";
    case "path_glob":
      return rule.path_glob ?? "";
    case "extension":
      return rule.extension ?? "";
    case "media_type":
      return rule.media_type ?? "";
    case "maximum_size":
      return rule.maximum_size_bytes === null || rule.maximum_size_bytes === undefined
        ? ""
        : String(rule.maximum_size_bytes);
    case "source_type":
      return rule.source_type ?? "";
  }
}

export type RuleOperandNormalization =
  | { ok: true; normalized: string }
  | { ok: false; message: string };

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const NIL_UUID = "00000000-0000-0000-0000-000000000000";
const EXTENSION_CHARACTERS = /^[a-z0-9._-]+$/;
const MIME_TSPECIALS = new Set(["(", ")", "<", ">", "@", ",", ";", ":", "\\", '"', "[", "]", "?", "=", "/", "*"]);
const GLOB_FORBIDDEN_CHARACTERS = new Set(["?", "[", "]", "{", "}"]);

function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).length;
}

function isControlCharacter(char: string): boolean {
  const codePoint = char.codePointAt(0) ?? 0;
  return (codePoint <= 0x1f && codePoint >= 0) || (codePoint >= 0x7f && codePoint <= 0x9f);
}

function foldAsciiLowercase(value: string): string {
  let folded = "";
  for (const char of value) {
    folded += char >= "A" && char <= "Z" ? String.fromCharCode(char.charCodeAt(0) + 32) : char;
  }
  return folded;
}

interface PathChecks {
  readonly maximumSegments: number;
  readonly maximumBytes: number;
  readonly kindLabel: string;
  readonly allowWildcards: boolean;
}

function normalizeRelativePath(value: string, checks: PathChecks): RuleOperandNormalization {
  const normalized = value.normalize("NFC");
  if (normalized.length === 0) {
    return { ok: false, message: `Enter at least one segment for the ${checks.kindLabel.toLowerCase()}.` };
  }
  if (normalized.includes("\\")) {
    return { ok: false, message: "Use / as the separator; backslash is not allowed." };
  }
  if (normalized.startsWith("/")) {
    return { ok: false, message: `The ${checks.kindLabel.toLowerCase()} must be relative; remove the leading slash.` };
  }
  if (normalized.endsWith("/")) {
    return { ok: false, message: `Remove the trailing slash from the ${checks.kindLabel.toLowerCase()}.` };
  }
  if (Array.from(normalized).some(isControlCharacter)) {
    return { ok: false, message: "Control characters are not allowed." };
  }
  const segments = normalized.split("/");
  if (segments[0]?.includes(":")) {
    return { ok: false, message: "Scheme or drive prefixes are not allowed." };
  }
  if (checks.allowWildcards) {
    if (Array.from(normalized).some((char) => GLOB_FORBIDDEN_CHARACTERS.has(char))) {
      return {
        ok: false,
        message: "The glob supports only * wildcards; ?, character classes, braces and negation are not allowed.",
      };
    }
    if (segments.some((segment) => segment.startsWith("!"))) {
      return {
        ok: false,
        message: "The glob supports only * wildcards; ?, character classes, braces and negation are not allowed.",
      };
    }
  }
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    return { ok: false, message: "Segments . and .. are not allowed." };
  }
  if (segments.length > checks.maximumSegments) {
    return {
      ok: false,
      message: `The ${checks.kindLabel.toLowerCase()} has too many segments (at most ${checks.maximumSegments}).`,
    };
  }
  if (utf8Bytes(normalized) > checks.maximumBytes) {
    return {
      ok: false,
      message: `The ${checks.kindLabel.toLowerCase()} is too long (at most ${checks.maximumBytes} bytes).`,
    };
  }
  if (segments.some((segment) => utf8Bytes(segment) > 255)) {
    return { ok: false, message: "One segment is too long (at most 255 bytes)." };
  }
  if (checks.allowWildcards && Array.from(normalized).filter((char) => char === "*").length > 16) {
    return { ok: false, message: "The glob has too many wildcard tokens (at most 16)." };
  }
  return { ok: true, normalized };
}

function normalizeExtension(value: string): RuleOperandNormalization {
  const folded = foldAsciiLowercase(value.normalize("NFC"));
  if (folded.length < 2 || folded.length > 64 || !folded.startsWith(".")) {
    return { ok: false, message: "The extension must be 2–64 characters starting with a dot." };
  }
  if (!EXTENSION_CHARACTERS.test(folded.slice(1))) {
    return {
      ok: false,
      message: "The extension may contain only lowercase letters, digits, dots, hyphens and underscores.",
    };
  }
  return { ok: true, normalized: folded };
}

function isLowercaseTokenPart(char: string): boolean {
  const codePoint = char.charCodeAt(0);
  return (
    codePoint >= 33 &&
    codePoint <= 126 &&
    !MIME_TSPECIALS.has(char) &&
    !(char >= "A" && char <= "Z") &&
    char !== "*"
  );
}

function normalizeMediaType(value: string): RuleOperandNormalization {
  const separatorIndex = value.indexOf("/");
  if (separatorIndex === -1 || value.indexOf("/", separatorIndex + 1) !== -1) {
    return { ok: false, message: "Enter a media type as type/subtype or a family as type/*." };
  }
  const typePart = value.slice(0, separatorIndex);
  const subtypePart = value.slice(separatorIndex + 1);
  if (typePart.length === 0 || subtypePart.length === 0) {
    return { ok: false, message: "Enter a media type as type/subtype or a family as type/*." };
  }
  if (subtypePart === "*") {
    const isFamilyType = Array.from(typePart).every(
      (char) =>
        char.charCodeAt(0) >= 33 &&
        char.charCodeAt(0) <= 126 &&
        !MIME_TSPECIALS.has(char) &&
        !(char >= "A" && char <= "Z"),
    );
    if (!isFamilyType || typePart.includes("*")) {
      return { ok: false, message: "Family media types must be a lowercase type token followed by /*." };
    }
    return { ok: true, normalized: `${typePart}/*` };
  }
  const isExactMediaType = Array.from(value).every((char) => char === "/" || isLowercaseTokenPart(char));
  if (!isExactMediaType) {
    return { ok: false, message: "Media types are lowercase, without parameters or wildcards." };
  }
  return { ok: true, normalized: value };
}

function normalizeMaximumSize(value: string): RuleOperandNormalization {
  if (!/^\d+$/.test(value)) {
    return { ok: false, message: "Enter a whole number of bytes between 0 and 104857600." };
  }
  const sizeBytes = Number(value);
  if (!Number.isSafeInteger(sizeBytes) || sizeBytes < 0 || sizeBytes > MAXIMUM_SIZE_BYTES_CEILING) {
    return { ok: false, message: "Enter a whole number of bytes between 0 and 104857600." };
  }
  return { ok: true, normalized: String(sizeBytes) };
}

function normalizeExactSourceId(value: string): RuleOperandNormalization {
  const trimmed = value.trim();
  if (!UUID_PATTERN.test(trimmed) || trimmed.toLowerCase() === NIL_UUID) {
    return { ok: false, message: "Enter the source ID in UUID form." };
  }
  return { ok: true, normalized: trimmed.toLowerCase() };
}

/**
 * Validates and normalizes one rule operand with the closed grammar of
 * spec 6.2–6.4. Feedback strings are closed copy: they never echo the
 * rejected value.
 */
export function normalizeRuleOperand(ruleKind: RuleKind, operandText: string): RuleOperandNormalization {
  switch (ruleKind) {
    case "exact_source_id":
      return normalizeExactSourceId(operandText);
    case "folder_prefix":
      return normalizeRelativePath(operandText, {
        allowWildcards: false,
        kindLabel: "folder prefix",
        maximumBytes: 4096,
        maximumSegments: 256,
      });
    case "path_glob":
      return normalizeRelativePath(operandText, {
        allowWildcards: true,
        kindLabel: "path glob",
        maximumBytes: 1024,
        maximumSegments: 64,
      });
    case "extension":
      return normalizeExtension(operandText);
    case "media_type":
      return normalizeMediaType(operandText);
    case "maximum_size":
      return normalizeMaximumSize(operandText);
    case "source_type": {
      const trimmed = operandText.trim();
      if (!SOURCE_TYPE_OPTIONS.includes(trimmed)) {
        return {
          ok: false,
          message: `Choose one of the closed source types: ${SOURCE_TYPE_OPTIONS.join(", ")}.`,
        };
      }
      return { ok: true, normalized: trimmed };
    }
  }
}

/** The stable duplicate-detection key: kind plus normalized operand. */
function ruleSemanticKey(rule: PolicyDraftRuleInput): string | null {
  const normalization = normalizeRuleOperand(rule.rule_kind, rule.operandText);
  return normalization.ok ? `${rule.rule_kind}\n${normalization.normalized}` : null;
}

export interface DraftValidation {
  /** Closed feedback per rule ID; null when the row is valid. */
  readonly rowIssues: ReadonlyMap<string, string>;
  readonly isSaveable: boolean;
}

/**
 * Whole-draft validation: every operand must satisfy its grammar and no two
 * rules may share a semantic key. Empty rows block saving without feedback
 * until the user has touched them.
 */
export function validateDraft(draft: PolicyDraft, editedRuleIds: ReadonlySet<string> = new Set()): DraftValidation {
  const rowIssues = new Map<string, string>();
  const seenKeys = new Map<string, string>();
  let hasEmptyRows = false;
  for (const rule of draft.rules) {
    if (rule.operandText.trim().length === 0) {
      hasEmptyRows = true;
      if (editedRuleIds.has(rule.rule_id)) {
        rowIssues.set(rule.rule_id, "Enter a value for this rule.");
      }
      continue;
    }
    const normalization = normalizeRuleOperand(rule.rule_kind, rule.operandText);
    if (!normalization.ok) {
      rowIssues.set(rule.rule_id, normalization.message);
      continue;
    }
    const key = `${rule.rule_kind}\n${normalization.normalized}`;
    const firstRuleId = seenKeys.get(key);
    if (firstRuleId !== undefined) {
      rowIssues.set(rule.rule_id, "An identical rule already exists in this draft.");
      continue;
    }
    seenKeys.set(key, rule.rule_id);
  }
  const isSaveable =
    rowIssues.size === 0 && !hasEmptyRows && draft.rules.length <= MAXIMUM_RULES_PER_REVISION;
  return { rowIssues, isSaveable };
}

/** Maps the editable draft onto the strict full-list replacement body. */
export function buildDraftReplaceRequest(draft: PolicyDraft): PolicyDraftReplaceRequest {
  return {
    expected_draft_version: draft.draft_version,
    rules: draft.rules.map((rule) => {
      const normalization = normalizeRuleOperand(rule.rule_kind, rule.operandText);
      if (!normalization.ok) {
        throw new Error(`draft rule ${rule.rule_id} failed validation`);
      }
      const normalized = normalization.normalized;
      switch (rule.rule_kind) {
        case "exact_source_id":
          return { rule_id: rule.rule_id, rule_kind: rule.rule_kind, source_id: normalized };
        case "folder_prefix":
          return { rule_id: rule.rule_id, rule_kind: rule.rule_kind, folder_prefix: normalized };
        case "path_glob":
          return { rule_id: rule.rule_id, rule_kind: rule.rule_kind, path_glob: normalized };
        case "extension":
          return { rule_id: rule.rule_id, rule_kind: rule.rule_kind, extension: normalized };
        case "media_type":
          return { rule_id: rule.rule_id, rule_kind: rule.rule_kind, media_type: normalized };
        case "maximum_size":
          return {
            rule_id: rule.rule_id,
            rule_kind: rule.rule_kind,
            maximum_size_bytes: Number(normalized),
          };
        case "source_type":
          return { rule_id: rule.rule_id, rule_kind: rule.rule_kind, source_type: normalized };
      }
    }),
  };
}

export interface DraftChangeCounts {
  readonly added: number;
  readonly removed: number;
  readonly changed: number;
  readonly hasUnsavedChanges: boolean;
}

/**
 * The current-versus-draft structural diff available without a new preview:
 * rule additions, removals and edits between the saved server draft and the
 * local editing state.
 */
export function countDraftChanges(local: PolicyDraft, saved: PolicyDraft): DraftChangeCounts {
  const savedRules = new Map(saved.rules.map((rule) => [rule.rule_id, rule]));
  const localRules = new Map(local.rules.map((rule) => [rule.rule_id, rule]));
  let added = 0;
  let changed = 0;
  for (const rule of local.rules) {
    const savedRule = savedRules.get(rule.rule_id);
    if (savedRule === undefined) {
      added += 1;
      continue;
    }
    const savedNormalization = normalizeRuleOperand(savedRule.rule_kind, savedRule.operandText);
    const localNormalization = normalizeRuleOperand(rule.rule_kind, rule.operandText);
    const savedKey = savedNormalization.ok ? savedNormalization.normalized : savedRule.operandText;
    const localKey = localNormalization.ok ? localNormalization.normalized : rule.operandText;
    if (savedRule.rule_kind !== rule.rule_kind || savedKey !== localKey) {
      changed += 1;
    }
  }
  const removed = saved.rules.filter((rule) => !localRules.has(rule.rule_id)).length;
  return { added, removed, changed, hasUnsavedChanges: added + removed + changed > 0 };
}

/**
 * Publication gating (spec 11/17): a ready, unconsumed preview publishes only
 * when it is bound to exactly the saved draft version/identity and to the
 * active revision the status read reports. The source checkpoint travels with
 * the preview itself; the server rechecks it authoritatively.
 */
export function isPreviewPublishable(
  preview: PolicyPreviewData,
  savedDraft: PolicyDraft,
  status: PolicyStatusData,
): boolean {
  return (
    preview.status === "ready" &&
    preview.consumed_at === null &&
    preview.policy_draft_id === savedDraft.draft_id &&
    preview.draft_version === savedDraft.draft_version &&
    preview.base_policy_revision_id === status.active_policy_revision_id
  );
}

/** Sorted unique missing field names across indeterminate preview rows. */
export function indeterminateMissingFields(rows: readonly PolicyPreviewResultRowData[]): readonly string[] {
  const fields = new Set<string>();
  for (const row of rows) {
    if (row.impact_class === "indeterminate") {
      for (const field of row.missing_fields) {
        fields.add(field);
      }
    }
  }
  return [...fields].sort();
}

/** Closed terminal copy for a preview that stopped in a non-publishable state. */
export function previewStatusMessage(status: string): string {
  switch (status) {
    case "failed":
      return "The preview could not be completed. Start a new preview.";
    case "expired":
      return "The preview expired. Start a new preview.";
    case "consumed":
      return "The preview was already used for a publication. Start a new preview.";
    default:
      return "The preview is no longer available. Start a new preview.";
  }
}

/** Closed copy for a preview read that returned a registered error. */
export function previewFailureMessage(errorCode: string): string {
  switch (errorCode) {
    case "exclusion_policy_preview_failed":
      return "The preview could not be completed. Start a new preview.";
    case "exclusion_policy_preview_expired":
      return "The preview expired. Start a new preview.";
    case "exclusion_policy_preview_stale":
      return "The preview is stale because the workspace changed. Start a new preview.";
    default:
      return "The preview is no longer available. Start a new preview.";
  }
}

/** Closed copy when the Admin status read fails. */
export function policyLoadFailureMessage(errorCode: string): string {
  if (errorCode === "exclusion_policy_not_initialized") {
    return "The exclusion policy is not initialized for this workspace yet. Initialize signing keys first.";
  }
  return "The policy could not be loaded. Try again.";
}

/** Closed copy when an explicit draft save fails. */
export function draftSaveFailureMessage(errorCode: string): string {
  switch (errorCode) {
    case "exclusion_policy_draft_conflict":
      return "The draft was changed in another tab or window. Reload the draft and reapply your changes manually; nothing was saved.";
    case "exclusion_policy_input_invalid":
      return "The draft was rejected by validation. Check every rule and try again.";
    default:
      return "Saving the draft failed. Nothing was changed. Try again.";
  }
}

/** Closed copy when a publication attempt fails; never provider detail. */
export function publishFailureMessage(errorCode: string): string {
  switch (errorCode) {
    case "exclusion_policy_confirmation_invalid":
      return "The confirmation did not match. Type PUBLISH EXCLUSION POLICY exactly and try again.";
    case "exclusion_policy_preview_expired":
      return "The preview expired before publication. Start a new preview.";
    case "exclusion_policy_preview_stale":
      return "The preview is stale. Start a new preview.";
    case "exclusion_policy_snapshot_outdated":
      return "The active policy changed since the preview. Reload and start a new preview.";
    case "exclusion_policy_signing_unavailable":
      return "Policy signing is currently unavailable. Try again later.";
    case "exclusion_policy_commit_outcome_unknown":
      return "The publication outcome is unknown. Retry with the same confirmation to resolve it.";
    default:
      return "Publishing the policy failed. Nothing was changed.";
  }
}
