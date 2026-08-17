/**
 * The pure deny-only policy evaluator, mirroring the Python reference
 * semantics of spec 6 and 7 byte for byte (the shared golden corpus is the
 * contract): locator/glob/operand normalization with the same closed reason
 * tokens, the same semantic fingerprints, and deny-only evaluation where a
 * definite match wins, missing required evidence yields indeterminate and
 * everything else is allowed. Enforcement maps indeterminate to excluded.
 */

import type { RuleKindName } from "./contracts";
import { SOURCE_TYPES } from "./contracts";
import { canonicalJsonBytes, sha256Hex } from "./canonical-json";
import type { ClosedJsonValue } from "./strict-json";

// --- closed reason tokens (mirror personal_os.exclusion_policy.errors) ------------------

export type PolicyRuleReason =
  | "locator_not_valid_unicode"
  | "locator_empty"
  | "locator_absolute"
  | "locator_trailing_separator"
  | "locator_backslash_separator"
  | "locator_scheme_or_drive"
  | "locator_invalid_segment"
  | "locator_control_character"
  | "locator_too_long"
  | "locator_too_many_segments"
  | "locator_segment_too_long"
  | "glob_unsupported_token"
  | "glob_too_long"
  | "glob_too_many_segments"
  | "glob_too_many_wildcards"
  | "rule_id_invalid"
  | "rule_count_invalid"
  | "operand_missing"
  | "operand_conflict"
  | "operand_invalid"
  | "subject_id_invalid"
  | "subject_locator_not_normalized"
  | "subject_field_type_invalid"
  | "subject_size_invalid"
  | "subject_workspace_mismatch";

/** One closed normalization/evaluation rejection; no rejected value inside. */
export class PolicyRuleError extends Error {
  readonly reason: PolicyRuleReason;

  constructor(reason: PolicyRuleReason) {
    super(`exclusion policy rule contract failed: ${reason}`);
    this.name = "PolicyRuleError";
    this.reason = reason;
  }
}

// --- bounds (spec 6.2-6.4) ---------------------------------------------------------------

export const LOCATOR_MAXIMUM_BYTES = 4096;
export const LOCATOR_MAXIMUM_SEGMENTS = 256;
export const LOCATOR_SEGMENT_MAXIMUM_BYTES = 255;
export const GLOB_MAXIMUM_BYTES = 1024;
export const GLOB_MAXIMUM_SEGMENTS = 64;
export const GLOB_MAXIMUM_WILDCARD_TOKENS = 16;
export const EXTENSION_MINIMUM_CHARACTERS = 2;
export const EXTENSION_MAXIMUM_CHARACTERS = 64;
export const MAXIMUM_SIZE_BYTES_CEILING = 104857600;
export const MAXIMUM_RULES_PER_EVALUATION = 256;

const RULE_FINGERPRINT_CONTRACT = "exclusion_policy_rule/v1";
const NIL_UUID = "00000000-0000-0000-0000-000000000000";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const EXTENSION_CHARACTERS = new Set("abcdefghijklmnopqrstuvwxyz0123456789._-");
const MIME_TSPECIALS = new Set(["(", ")", "<", ">", "@", ",", ";", ":", "\\", '"', "[", "]", "?", "=", "/", "*"]);
const GLOB_FORBIDDEN_CHARACTERS = new Set(["?", "[", "]", "{", "}"]);

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}

function isControlCharacter(character: string): boolean {
  const codePoint = character.codePointAt(0) ?? 0;
  // Unicode category Cc: the C0 controls, DEL and the C1 controls.
  return codePoint < 0x20 || (codePoint >= 0x7f && codePoint <= 0x9f);
}

/** Normalize to NFC and reject strings that are not valid Unicode. */
function nfcOrReject(value: string): string {
  const normalized = value.normalize("NFC");
  if (hasUnpairedSurrogate(value) || hasUnpairedSurrogate(normalized)) {
    throw new PolicyRuleError("locator_not_valid_unicode");
  }
  return normalized;
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const next = index + 1 < value.length ? value.charCodeAt(index + 1) : 0;
      if (next >= 0xdc00 && next <= 0xdfff) {
        index += 1;
        continue;
      }
      return true;
    }
    if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      return true;
    }
  }
  return false;
}

/** Fold only ASCII A-Z; every other code point stays literal (spec 6.3). */
export function foldAsciiLowercase(value: string): string {
  let folded = "";
  for (const character of value) {
    const codeUnit = character.charCodeAt(0);
    folded += codeUnit >= 0x41 && codeUnit <= 0x5a
      ? String.fromCharCode(codeUnit + 32)
      : character;
  }
  return folded;
}

/** Normalize one Vault locator to the canonical NFC relative form (spec 6.3). */
export function normalizePolicyLocator(value: string): string {
  if (typeof value !== "string") {
    throw new PolicyRuleError("locator_not_valid_unicode");
  }
  const normalized = nfcOrReject(value);
  if (normalized.length === 0) {
    throw new PolicyRuleError("locator_empty");
  }
  if (normalized.includes("\\")) {
    throw new PolicyRuleError("locator_backslash_separator");
  }
  if (normalized.startsWith("/")) {
    throw new PolicyRuleError("locator_absolute");
  }
  if (normalized.endsWith("/")) {
    throw new PolicyRuleError("locator_trailing_separator");
  }
  for (const character of normalized) {
    if (isControlCharacter(character)) {
      throw new PolicyRuleError("locator_control_character");
    }
  }
  const segments = normalized.split("/");
  if (segments[0]?.includes(":")) {
    throw new PolicyRuleError("locator_scheme_or_drive");
  }
  for (const segment of segments) {
    if (segment === "" || segment === "." || segment === "..") {
      throw new PolicyRuleError("locator_invalid_segment");
    }
  }
  if (segments.length > LOCATOR_MAXIMUM_SEGMENTS) {
    throw new PolicyRuleError("locator_too_many_segments");
  }
  if (utf8ByteLength(normalized) > LOCATOR_MAXIMUM_BYTES) {
    throw new PolicyRuleError("locator_too_long");
  }
  for (const segment of segments) {
    if (utf8ByteLength(segment) > LOCATOR_SEGMENT_MAXIMUM_BYTES) {
      throw new PolicyRuleError("locator_segment_too_long");
    }
  }
  return normalized;
}

// --- the closed glob grammar (spec 6.4) ---------------------------------------------------

interface GlobSegmentPart {
  readonly kind: "literal" | "star";
  readonly text: string;
}

interface CompiledGlobSegment {
  readonly isDoubleStar: boolean;
  readonly parts: readonly GlobSegmentPart[];
}

interface CompiledGlob {
  readonly segments: readonly CompiledGlobSegment[];
}

function normalizeGlobText(pattern: string): string {
  if (typeof pattern !== "string") {
    throw new PolicyRuleError("locator_not_valid_unicode");
  }
  const normalized = nfcOrReject(pattern);
  if (normalized.length === 0) {
    throw new PolicyRuleError("locator_empty");
  }
  for (const character of normalized) {
    if (GLOB_FORBIDDEN_CHARACTERS.has(character)) {
      throw new PolicyRuleError("glob_unsupported_token");
    }
  }
  if (normalized.includes("\\")) {
    throw new PolicyRuleError("locator_backslash_separator");
  }
  if (normalized.startsWith("/")) {
    throw new PolicyRuleError("locator_absolute");
  }
  if (normalized.endsWith("/")) {
    throw new PolicyRuleError("locator_trailing_separator");
  }
  const segments = normalized.split("/");
  for (const segment of segments) {
    if (segment.startsWith("!")) {
      throw new PolicyRuleError("glob_unsupported_token");
    }
  }
  for (const character of normalized) {
    if (isControlCharacter(character)) {
      throw new PolicyRuleError("locator_control_character");
    }
  }
  if (segments[0]?.includes(":")) {
    throw new PolicyRuleError("locator_scheme_or_drive");
  }
  for (const segment of segments) {
    if (segment === "" || segment === "." || segment === "..") {
      throw new PolicyRuleError("locator_invalid_segment");
    }
  }
  if (segments.length > GLOB_MAXIMUM_SEGMENTS) {
    throw new PolicyRuleError("glob_too_many_segments");
  }
  if (utf8ByteLength(normalized) > GLOB_MAXIMUM_BYTES) {
    throw new PolicyRuleError("glob_too_long");
  }
  let wildcardCount = 0;
  for (const character of normalized) {
    if (character === "*") {
      wildcardCount += 1;
    }
  }
  if (wildcardCount > GLOB_MAXIMUM_WILDCARD_TOKENS) {
    throw new PolicyRuleError("glob_too_many_wildcards");
  }
  return normalized;
}

function compileSegment(segment: string): CompiledGlobSegment {
  if (segment === "**") {
    return { isDoubleStar: true, parts: [] };
  }
  const parts: GlobSegmentPart[] = [];
  let literal = "";
  for (const character of segment) {
    if (character === "*") {
      if (literal.length > 0) {
        parts.push({ kind: "literal", text: literal });
        literal = "";
      }
      parts.push({ kind: "star", text: "" });
      continue;
    }
    literal += character;
  }
  if (literal.length > 0) {
    parts.push({ kind: "literal", text: literal });
  }
  return { isDoubleStar: false, parts };
}

export function compilePolicyGlob(pattern: string): CompiledGlob {
  const normalized = normalizeGlobText(pattern);
  return { segments: normalized.split("/").map((segment) => compileSegment(segment)) };
}

function segmentMatches(parts: readonly GlobSegmentPart[], value: string): boolean {
  const partCount = parts.length;
  const valueLength = value.length;
  let partIndex = 0;
  let valueIndex = 0;
  let backtrackPart = -1;
  let backtrackValue = 0;
  while (valueIndex < valueLength) {
    if (partIndex < partCount) {
      const part = parts[partIndex];
      if (part !== undefined && part.kind === "literal") {
        if (value.startsWith(part.text, valueIndex)) {
          valueIndex += part.text.length;
          partIndex += 1;
          continue;
        }
      } else {
        backtrackPart = partIndex;
        backtrackValue = valueIndex;
        partIndex += 1;
        continue;
      }
    }
    if (backtrackPart >= 0) {
      backtrackValue += 1;
      valueIndex = backtrackValue;
      partIndex = backtrackPart + 1;
      continue;
    }
    return false;
  }
  while (partIndex < partCount) {
    if (parts[partIndex]?.kind !== "star") {
      return false;
    }
    partIndex += 1;
  }
  return true;
}

export function globMatches(
  compiled: CompiledGlob,
  locatorSegments: readonly string[],
): boolean {
  const pathCount = locatorSegments.length;
  let reachable = new Array<boolean>(pathCount + 1).fill(false);
  reachable[0] = true;
  for (const segment of compiled.segments) {
    const nextReachable = new Array<boolean>(pathCount + 1).fill(false);
    if (segment.isDoubleStar) {
      let prefixReachable = false;
      for (let index = 0; index <= pathCount; index += 1) {
        prefixReachable = prefixReachable || (reachable[index] ?? false);
        nextReachable[index] = prefixReachable;
      }
    } else {
      for (let index = 0; index < pathCount; index += 1) {
        if (
          reachable[index] &&
          segmentMatches(segment.parts, locatorSegments[index] ?? "")
        ) {
          nextReachable[index + 1] = true;
        }
      }
    }
    reachable = nextReachable;
  }
  return reachable[pathCount] ?? false;
}

// --- operands and rules (spec 6.1-6.2) -----------------------------------------------------

function isFamilyTypeToken(value: string): boolean {
  if (value.length === 0) {
    return false;
  }
  for (const character of value) {
    const codeUnit = character.charCodeAt(0);
    if (codeUnit < 33 || codeUnit > 126) {
      return false;
    }
    if (MIME_TSPECIALS.has(character)) {
      return false;
    }
    if (codeUnit >= 0x41 && codeUnit <= 0x5a) {
      return false;
    }
  }
  return true;
}

/** Validate one lowercase canonical `type/subtype` MIME value (no parameters). */
export function isCanonicalMediaType(value: string): boolean {
  const separatorIndex = value.indexOf("/");
  if (separatorIndex <= 0 || separatorIndex !== value.lastIndexOf("/")) {
    return false;
  }
  const typePart = value.slice(0, separatorIndex);
  const subtypePart = value.slice(separatorIndex + 1);
  if (typePart.length === 0 || subtypePart.length === 0) {
    return false;
  }
  for (const character of value) {
    const codeUnit = character.charCodeAt(0);
    if (codeUnit === 0x2f) {
      continue;
    }
    if (codeUnit < 33 || codeUnit > 126) {
      return false;
    }
    if (MIME_TSPECIALS.has(character)) {
      return false;
    }
    if (codeUnit >= 0x41 && codeUnit <= 0x5a) {
      return false;
    }
    if (character === "*") {
      return false;
    }
  }
  return true;
}

function normalizeExtensionOperand(textOperand: string): string {
  const folded = foldAsciiLowercase(textOperand);
  if (
    folded.length < EXTENSION_MINIMUM_CHARACTERS ||
    folded.length > EXTENSION_MAXIMUM_CHARACTERS
  ) {
    throw new PolicyRuleError("operand_invalid");
  }
  if (!folded.startsWith(".")) {
    throw new PolicyRuleError("operand_invalid");
  }
  for (const character of folded) {
    if (!EXTENSION_CHARACTERS.has(character)) {
      throw new PolicyRuleError("operand_invalid");
    }
  }
  return folded;
}

export type RuleOperand =
  | { readonly kind: "exact_source_id"; readonly sourceId: string }
  | { readonly kind: "folder_prefix"; readonly folderPrefix: string }
  | { readonly kind: "path_glob"; readonly pattern: string; readonly compiled: CompiledGlob }
  | { readonly kind: "extension"; readonly extension: string }
  | {
      readonly kind: "media_type";
      readonly exact: string | null;
      readonly familyType: string | null;
    }
  | { readonly kind: "maximum_size"; readonly maximumSizeBytes: number }
  | { readonly kind: "source_type"; readonly sourceType: string };

export interface NormalizedPolicyRule {
  readonly ruleId: string;
  readonly ruleKind: RuleKindName;
  readonly operand: RuleOperand;
  readonly semanticFingerprint: string;
}

function requireUuid(value: string, reason: PolicyRuleReason): string {
  if (!UUID_PATTERN.test(value) || value === NIL_UUID) {
    throw new PolicyRuleError(reason);
  }
  return value;
}

function fingerprintEnvelopeValue(
  ruleKind: RuleKindName,
  operand: RuleOperand,
): Record<string, ClosedJsonValue> {
  const envelope: Record<string, ClosedJsonValue> = {
    contract: RULE_FINGERPRINT_CONTRACT,
    rule_kind: ruleKind,
  };
  switch (operand.kind) {
    case "exact_source_id":
      envelope["source_id"] = operand.sourceId;
      break;
    case "folder_prefix":
      envelope["folder_prefix"] = operand.folderPrefix;
      break;
    case "path_glob":
      envelope["path_glob"] = operand.pattern;
      break;
    case "extension":
      envelope["extension"] = operand.extension;
      break;
    case "media_type":
      envelope["media_type"] = operand.exact ?? `${operand.familyType ?? ""}/*`;
      break;
    case "maximum_size":
      envelope["maximum_size_bytes"] = operand.maximumSizeBytes;
      break;
    case "source_type":
      envelope["source_type"] = operand.sourceType;
      break;
  }
  return envelope;
}

export interface NormalizeRuleInput {
  readonly ruleId: string;
  readonly ruleKind: RuleKindName;
  readonly sourceIdOperand?: string | null;
  readonly textOperand?: string | null;
  readonly sizeBytesOperand?: number | null;
}

/**
 * Normalize one rule into its immutable operand form with the same semantic
 * fingerprint the Python backend computes (lowercase SHA-256 over the closed
 * fingerprint envelope).
 */
export async function normalizePolicyRule(input: NormalizeRuleInput): Promise<NormalizedPolicyRule> {
  requireUuid(input.ruleId, "rule_id_invalid");
  const populated = [input.sourceIdOperand ?? null, input.textOperand ?? null, input.sizeBytesOperand ?? null].filter(
    (operand) => operand !== null,
  );
  if (populated.length === 0) {
    throw new PolicyRuleError("operand_missing");
  }
  if (populated.length > 1) {
    throw new PolicyRuleError("operand_conflict");
  }
  let operand: RuleOperand;
  switch (input.ruleKind) {
    case "exact_source_id":
      if (typeof input.sourceIdOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = {
        kind: "exact_source_id",
        sourceId: requireUuid(input.sourceIdOperand, "operand_invalid"),
      };
      break;
    case "folder_prefix":
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "folder_prefix", folderPrefix: normalizePolicyLocator(input.textOperand) };
      break;
    case "path_glob": {
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      const pattern = normalizeGlobText(input.textOperand);
      operand = { kind: "path_glob", pattern, compiled: compilePolicyGlob(pattern) };
      break;
    }
    case "extension":
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "extension", extension: normalizeExtensionOperand(input.textOperand) };
      break;
    case "media_type": {
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      const separatorIndex = input.textOperand.indexOf("/");
      if (
        separatorIndex > 0 &&
        input.textOperand.length === separatorIndex + 2 &&
        input.textOperand.endsWith("/*")
      ) {
        const familyType = input.textOperand.slice(0, separatorIndex);
        if (!isFamilyTypeToken(familyType)) {
          throw new PolicyRuleError("operand_invalid");
        }
        operand = { kind: "media_type", exact: null, familyType };
        break;
      }
      if (!isCanonicalMediaType(input.textOperand)) {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "media_type", exact: input.textOperand, familyType: null };
      break;
    }
    case "maximum_size": {
      const sizeBytes = input.sizeBytesOperand;
      if (
        typeof sizeBytes !== "number" ||
        !Number.isInteger(sizeBytes) ||
        sizeBytes < 0 ||
        sizeBytes > MAXIMUM_SIZE_BYTES_CEILING
      ) {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "maximum_size", maximumSizeBytes: sizeBytes };
      break;
    }
    case "source_type": {
      if (typeof input.textOperand !== "string") {
        throw new PolicyRuleError("operand_invalid");
      }
      if (!SOURCE_TYPES.includes(input.textOperand)) {
        throw new PolicyRuleError("operand_invalid");
      }
      operand = { kind: "source_type", sourceType: input.textOperand };
      break;
    }
  }
  const fingerprint = await sha256Hex(
    canonicalJsonBytes(fingerprintEnvelopeValue(input.ruleKind, operand)),
  );
  return {
    ruleId: input.ruleId,
    ruleKind: input.ruleKind,
    operand,
    semanticFingerprint: fingerprint,
  };
}

// --- evaluation (spec 7) --------------------------------------------------------------------

export interface PolicyEvaluationSubject {
  readonly workspaceId: string;
  readonly sourceId?: string | null;
  readonly normalizedLocator?: string | null;
  readonly sourceType?: string | null;
  readonly mediaType?: string | null;
  readonly sizeBytes?: number | null;
}

export interface PolicyEvaluationOutcome {
  readonly raw: "allowed" | "excluded" | "indeterminate";
  readonly enforced: "allowed" | "excluded";
  readonly matchedRuleIds: readonly string[];
  readonly missingFields: readonly string[];
}

const REQUIRED_FIELD_BY_KIND: Readonly<Record<RuleKindName, string>> = {
  exact_source_id: "source_id",
  folder_prefix: "normalized_locator",
  path_glob: "normalized_locator",
  extension: "normalized_locator",
  media_type: "media_type",
  maximum_size: "size_bytes",
  source_type: "source_type",
};

function ruleMatches(rule: NormalizedPolicyRule, subject: PolicyEvaluationSubject): boolean | null {
  const operand = rule.operand;
  switch (operand.kind) {
    case "exact_source_id":
      if (subject.sourceId == null) {
        return null;
      }
      return subject.sourceId === operand.sourceId;
    case "folder_prefix": {
      if (subject.normalizedLocator == null) {
        return null;
      }
      const segments = subject.normalizedLocator.split("/");
      const prefixSegments = operand.folderPrefix.split("/");
      return segments.slice(0, prefixSegments.length).join("/") === operand.folderPrefix;
    }
    case "path_glob":
      if (subject.normalizedLocator == null) {
        return null;
      }
      return globMatches(operand.compiled, subject.normalizedLocator.split("/"));
    case "extension": {
      if (subject.normalizedLocator == null) {
        return null;
      }
      const finalFilename = subject.normalizedLocator.split("/").pop() ?? "";
      return foldAsciiLowercase(finalFilename).endsWith(operand.extension);
    }
    case "media_type": {
      if (subject.mediaType == null) {
        return null;
      }
      if (operand.exact !== null) {
        return subject.mediaType === operand.exact;
      }
      return subject.mediaType.split("/")[0] === operand.familyType;
    }
    case "maximum_size":
      if (subject.sizeBytes == null) {
        return null;
      }
      return subject.sizeBytes > operand.maximumSizeBytes;
    case "source_type":
      if (subject.sourceType == null) {
        return null;
      }
      return subject.sourceType === operand.sourceType;
  }
}

function validateSubject(subject: PolicyEvaluationSubject, workspaceId: string): void {
  if (subject.workspaceId !== workspaceId) {
    throw new PolicyRuleError("subject_workspace_mismatch");
  }
  if (subject.sourceId != null) {
    requireUuid(subject.sourceId, "subject_id_invalid");
  }
  if (subject.normalizedLocator != null) {
    if (typeof subject.normalizedLocator !== "string") {
      throw new PolicyRuleError("subject_field_type_invalid");
    }
    if (normalizePolicyLocator(subject.normalizedLocator) !== subject.normalizedLocator) {
      throw new PolicyRuleError("subject_locator_not_normalized");
    }
  }
  if (subject.sourceType != null && !SOURCE_TYPES.includes(subject.sourceType)) {
    throw new PolicyRuleError("subject_field_type_invalid");
  }
  if (subject.mediaType != null && !isCanonicalMediaType(subject.mediaType)) {
    throw new PolicyRuleError("subject_field_type_invalid");
  }
  if (subject.sizeBytes != null) {
    if (
      typeof subject.sizeBytes !== "number" ||
      !Number.isInteger(subject.sizeBytes) ||
      subject.sizeBytes < 0
    ) {
      throw new PolicyRuleError("subject_size_invalid");
    }
  }
}

/**
 * Evaluate one subject against immutable normalized rules, deterministically:
 * one or more definite matches exclude, otherwise any required missing field
 * yields indeterminate, otherwise the subject is allowed.
 */
export function evaluatePolicy(
  rules: readonly NormalizedPolicyRule[],
  subject: PolicyEvaluationSubject,
  options: { readonly workspaceId: string },
): PolicyEvaluationOutcome {
  if (rules.length > MAXIMUM_RULES_PER_EVALUATION) {
    throw new PolicyRuleError("rule_count_invalid");
  }
  validateSubject(subject, options.workspaceId);
  const matchedRuleIds: string[] = [];
  const missingFields = new Set<string>();
  for (const rule of rules) {
    const outcome = ruleMatches(rule, subject);
    if (outcome === null) {
      missingFields.add(REQUIRED_FIELD_BY_KIND[rule.ruleKind]);
    } else if (outcome) {
      matchedRuleIds.push(rule.ruleId);
    }
  }
  matchedRuleIds.sort();
  const sortedMissingFields = [...missingFields].sort();
  if (matchedRuleIds.length > 0) {
    return {
      raw: "excluded",
      enforced: "excluded",
      matchedRuleIds,
      missingFields: sortedMissingFields,
    };
  }
  if (missingFields.size > 0) {
    return {
      raw: "indeterminate",
      enforced: "excluded",
      matchedRuleIds,
      missingFields: sortedMissingFields,
    };
  }
  return { raw: "allowed", enforced: "allowed", matchedRuleIds, missingFields: sortedMissingFields };
}
