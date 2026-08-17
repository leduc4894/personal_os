import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { PolicyRuleError } from "./evaluator";
import {
  evaluatePolicy,
  normalizePolicyLocator,
  normalizePolicyRule,
} from "./evaluator";
import type { NormalizedPolicyRule, PolicyEvaluationSubject } from "./evaluator";

interface NormalizationCase {
  readonly case_id: string;
  readonly value: string;
  readonly expected?: string;
  readonly error_reason?: string;
}

interface RuleCase {
  readonly case_id: string;
  readonly rule_id: string;
  readonly rule_kind: string;
  readonly rule_index?: number;
  readonly source_id_operand?: string | null;
  readonly text_operand?: string | null;
  readonly size_bytes_operand?: number | null;
  readonly expected_fingerprint?: string;
  readonly error_reason?: string;
}

interface EvaluationCase {
  readonly case_id: string;
  readonly rules: readonly RuleCase[];
  readonly subject: Readonly<Record<string, unknown>>;
  readonly expected: {
    readonly raw: string;
    readonly enforced: string;
    readonly matched_rule_ids: readonly string[];
    readonly missing_fields: readonly string[];
  };
}

interface EvaluatorFixture {
  readonly contract: string;
  readonly workspace_id: string;
  readonly policy_revision_id: string;
  readonly revision_number: number;
  readonly normalization_cases: readonly NormalizationCase[];
  readonly rule_cases: readonly RuleCase[];
  readonly evaluation_cases: readonly EvaluationCase[];
}

const FIXTURE = JSON.parse(
  readFileSync(
    new URL(
      "../../../../tests/fixtures/exclusion_policy/evaluator-golden.json",
      import.meta.url,
    ),
    "utf8",
  ),
) as EvaluatorFixture;

function locatorRejection(value: string): string {
  try {
    normalizePolicyLocator(value);
  } catch (error) {
    if (error instanceof PolicyRuleError) {
      return error.reason;
    }
    throw error;
  }
  throw new Error("expected locator normalization to reject the value");
}

function buildRule(ruleCase: RuleCase): Promise<NormalizedPolicyRule> {
  return normalizePolicyRule({
    ruleId: ruleCase.rule_id,
    ruleKind: ruleCase.rule_kind as never,
    sourceIdOperand: ruleCase.source_id_operand ?? null,
    textOperand: ruleCase.text_operand ?? null,
    sizeBytesOperand: ruleCase.size_bytes_operand ?? null,
  });
}

function buildSubject(caseSubject: Readonly<Record<string, unknown>>): PolicyEvaluationSubject {
  return {
    workspaceId: FIXTURE.workspace_id,
    sourceId: (caseSubject["source_id"] as string | undefined) ?? null,
    normalizedLocator: (caseSubject["normalized_locator"] as string | undefined) ?? null,
    sourceType: (caseSubject["source_type"] as string | undefined) ?? null,
    mediaType: (caseSubject["media_type"] as string | undefined) ?? null,
    sizeBytes: (caseSubject["size_bytes"] as number | undefined) ?? null,
  };
}

describe("golden normalization cases (Python parity)", () => {
  it("replays every locator normalization outcome with the same reason tokens", () => {
    expect(FIXTURE.normalization_cases.length).toBeGreaterThanOrEqual(8);
    for (const caseItem of FIXTURE.normalization_cases) {
      if (caseItem.expected !== undefined) {
        expect(normalizePolicyLocator(caseItem.value), caseItem.case_id).toBe(
          caseItem.expected,
        );
      } else {
        expect(locatorRejection(caseItem.value), caseItem.case_id).toBe(
          caseItem.error_reason,
        );
      }
    }
  });
});

describe("golden rule cases (Python parity)", () => {
  it("replays operand normalization and the semantic fingerprints", async () => {
    expect(FIXTURE.rule_cases.length).toBeGreaterThanOrEqual(7);
    for (const ruleCase of FIXTURE.rule_cases) {
      if (ruleCase.error_reason !== undefined) {
        await expect(buildRule(ruleCase), ruleCase.case_id).rejects.toMatchObject({
          reason: ruleCase.error_reason,
        });
        continue;
      }
      const normalized = await buildRule(ruleCase);
      expect(normalized.semanticFingerprint, ruleCase.case_id).toBe(
        ruleCase.expected_fingerprint,
      );
    }
  });

  it("folds extension operands ASCII-lowercase to one semantic identity", async () => {
    const upper = await normalizePolicyRule({
      ruleId: "018f47a0-7b00-7000-8000-000000000102",
      ruleKind: "extension",
      textOperand: ".PDF",
      sourceIdOperand: null,
      sizeBytesOperand: null,
    });
    expect(upper.operand).toEqual({ kind: "extension", extension: ".pdf" });
  });
});

describe("golden evaluation cases (Python parity)", () => {
  it("replays every decision, match and missing-field outcome", async () => {
    expect(FIXTURE.evaluation_cases.length).toBeGreaterThanOrEqual(15);
    for (const caseItem of FIXTURE.evaluation_cases) {
      const rules = [];
      for (const ruleCase of caseItem.rules) {
        rules.push(await buildRule(ruleCase));
      }
      const outcome = evaluatePolicy(rules, buildSubject(caseItem.subject), {
        workspaceId: FIXTURE.workspace_id,
      });
      expect(
        { raw: outcome.raw, enforced: outcome.enforced },
        caseItem.case_id,
      ).toEqual({
        raw: caseItem.expected.raw,
        enforced: caseItem.expected.enforced,
      });
      expect(outcome.matchedRuleIds, caseItem.case_id).toEqual([
        ...caseItem.expected.matched_rule_ids,
      ]);
      expect(outcome.missingFields, caseItem.case_id).toEqual([
        ...caseItem.expected.missing_fields,
      ]);
    }
  });

  it("rejects subjects from a foreign workspace and unnormalized locators", () => {
    const rules: NormalizedPolicyRule[] = [];
    const subject: PolicyEvaluationSubject = {
      workspaceId: "018f47a0-7b00-7000-8000-000000000102",
      sourceId: null,
      normalizedLocator: null,
      sourceType: null,
      mediaType: null,
      sizeBytes: null,
    };
    const evaluationReason = (action: () => unknown): string => {
      try {
        action();
      } catch (error) {
        if (error instanceof PolicyRuleError) {
          return error.reason;
        }
        throw error;
      }
      throw new Error("expected evaluation to reject the subject");
    };
    expect(
      evaluationReason(() => evaluatePolicy(rules, subject, { workspaceId: FIXTURE.workspace_id })),
    ).toBe("subject_workspace_mismatch");
    expect(
      evaluationReason(() =>
        evaluatePolicy(
          rules,
          { ...subject, workspaceId: FIXTURE.workspace_id, normalizedLocator: "cafe\u0301/x.md" },
          { workspaceId: FIXTURE.workspace_id },
        ),
      ),
    ).toBe("subject_locator_not_normalized");
  });
});
