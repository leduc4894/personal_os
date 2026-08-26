/**
 * Tests of the hand-mirrored device sync wire client (device cursor and
 * manifest reconciliation, task 9).
 *
 * These tests pin the strict hand-mirrored wire grammar of the eight Task 6
 * device-sync operations against a raw transport double: every server shape,
 * UUID, integer bound, closed action/event union, canonical envelope failure
 * and the binary download's exact header names, byte length and SHA-256 —
 * a partial or truncated binary body NEVER lands as success. They also pin
 * the authentication and diagnostics contract: a missing access token avoids
 * the transport entirely and records `credential_failure/access_missing`,
 * every reached failure records the operation's exact cursor/reconcile/apply
 * stage with its closed reason, and a parsed envelope failure carries only
 * the UUID-gated request id plus the envelope's error code. Privacy: no
 * thrown error message ever carries the URL, a status number or body text.
 */

import { describe, expect, it } from "vitest";

import type { SyncHttpRequest } from "../journal/sync-api";
import type { DeviceSyncFailureCorrelation, DeviceSyncReason, DeviceSyncDiagnostics } from "./contracts";
import type {
  ApplyFailureStage,
  CredentialFailureStage,
  CursorFailureStage,
  ReconcileFailureStage,
} from "./contracts";
import {
  classifyDeviceSyncFailure,
  createDeviceSyncApi,
  DeviceSyncApiError,
} from "./api";
import type { DeviceSyncApi, DeviceSyncHttpTransport } from "./api";

const ORIGIN = "https://device.example.org";
const SECOND_ORIGIN = "https://device-2.example.org";
const ACCESS_TOKEN = "at1.device-sync-access";
const SECOND_ACCESS_TOKEN = "at2.device-sync-access";
const REQUEST_ID = "77777777-7777-4777-8777-777777777777";
const SOURCE_ID = "11111111-1111-4111-8111-111111111111";
const SOURCE_VERSION_ID = "22222222-2222-4222-8222-222222222222";
const MANIFEST_RUN_ID = "33333333-3333-4333-8333-333333333333";
const EVENT_ID = "44444444-4444-4444-8444-444444444444";
const ORIGIN_DEVICE_ID = "55555555-5555-4555-8555-555555555555";
const CURRENT_VERSION_ID = "66666666-6666-4666-8666-666666666666";
const TOMBSTONE_ID = "88888888-8888-4888-8888-888888888888";
const EXPIRES_AT = "2026-08-18T12:00:00Z";
const COMMITTED_AT = "2026-08-18T10:00:00Z";
const DOWNLOAD_BYTES = new TextEncoder().encode("# device download corpus bytes\n");

async function digestOf(bytes: Uint8Array): Promise<string> {
  const raw = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes as unknown as ArrayBuffer));
  let hexadecimal = "";
  for (const byte of raw) {
    hexadecimal += byte.toString(16).padStart(2, "0");
  }
  return hexadecimal;
}

// --- doubles -----------------------------------------------------------------------------------------

interface RecordedObservation {
  readonly kind: "cursor_failure" | "apply_failure" | "reconcile_failure" | "credential_failure";
  readonly stage: CursorFailureStage | ApplyFailureStage | ReconcileFailureStage | CredentialFailureStage;
  readonly reason: DeviceSyncReason;
  readonly correlation: DeviceSyncFailureCorrelation | undefined;
}

/** The synchronous recording diagnostics facade the tests read back. */
class RecordingDiagnostics implements DeviceSyncDiagnostics {
  readonly observations: RecordedObservation[] = [];

  cursorFailure(stage: CursorFailureStage, reason: DeviceSyncReason, correlation?: DeviceSyncFailureCorrelation): void {
    this.observations.push({ kind: "cursor_failure", stage, reason, correlation });
  }

  applyFailure(stage: ApplyFailureStage, reason: DeviceSyncReason, correlation?: DeviceSyncFailureCorrelation): void {
    this.observations.push({ kind: "apply_failure", stage, reason, correlation });
  }

  reconcileFailure(stage: ReconcileFailureStage, reason: DeviceSyncReason, correlation?: DeviceSyncFailureCorrelation): void {
    this.observations.push({ kind: "reconcile_failure", stage, reason, correlation });
  }

  credentialFailure(stage: "access_missing" | "refresh_failed", reason: DeviceSyncReason): void {
    this.observations.push({ kind: "credential_failure", stage, reason, correlation: undefined });
  }
}

interface TestOptions {
  readonly transport: DeviceSyncHttpTransport;
  readonly accessToken?: string | null;
  readonly diagnostics?: DeviceSyncDiagnostics;
  readonly origin?: string;
}

function createApi(options: TestOptions): { api: DeviceSyncApi; diagnostics: RecordingDiagnostics } {
  const diagnostics = options.diagnostics ?? new RecordingDiagnostics();
  const api = createDeviceSyncApi({
    transport: options.transport,
    resolveOrigin: () => options.origin ?? ORIGIN,
    getAccessToken: () => options.accessToken === undefined ? ACCESS_TOKEN : options.accessToken,
    diagnostics,
  });
  return { api, diagnostics: diagnostics instanceof RecordingDiagnostics ? diagnostics : new RecordingDiagnostics() };
}

/** A recording JSON transport: every request lands in the journal of calls. */
function jsonTransport(
  respond: (request: SyncHttpRequest) => Promise<{ readonly status: number; readonly bodyText: string }>,
): DeviceSyncHttpTransport & { readonly requests: SyncHttpRequest[] } {
  const requests: SyncHttpRequest[] = [];
  return Object.assign(
    async (request: SyncHttpRequest) => {
      requests.push(request);
      return { bodyBytes: null, headers: {}, ...(await respond(request)) };
    },
    { requests },
  );
}

/** A fixed JSON transport answering every request with the same envelope. */
function staticJsonTransport(status: number, bodyText: string): DeviceSyncHttpTransport & { readonly requests: SyncHttpRequest[] } {
  return jsonTransport(async () => ({ status, bodyText }));
}

interface BinaryResponseSpec {
  readonly status?: number;
  readonly contentLength: string | null;
  readonly sha256Header: string | null;
  readonly contentType?: string | null;
  readonly requestIdHeader?: string | null;
  readonly bytes: Uint8Array;
}

/** One binary transport response shaped like the verified download. */
async function binaryResponse(spec: BinaryResponseSpec): Promise<DeviceSyncHttpTransport & { readonly requests: SyncHttpRequest[] }> {
  const bodyText = spec.status === undefined || spec.status === 200 ? "" : errorBody("device_download_integrity_failed");
  return jsonTransport(async () => ({
    status: spec.status ?? 200,
    bodyText,
    bodyBytes: spec.bytes.slice().buffer as ArrayBuffer,
    headers: {
      ...(spec.contentLength === null ? {} : { "content-length": spec.contentLength }),
      ...(spec.sha256Header === null ? {} : { "x-content-sha256": spec.sha256Header }),
      ...(spec.contentType === null || spec.contentType === undefined ? {} : { "content-type": spec.contentType }),
      ...(spec.requestIdHeader === null || spec.requestIdHeader === undefined ? {} : { "x-request-id": spec.requestIdHeader }),
    },
  }));
}

// --- wire fixtures -----------------------------------------------------------------------------------

function successBody(data: unknown): string {
  return JSON.stringify({ request_id: REQUEST_ID, data, warnings: [], error: null });
}

function errorBody(code: string, requestId: string = REQUEST_ID): string {
  return JSON.stringify({
    request_id: requestId,
    data: null,
    warnings: [],
    error: { code, message: "safe registered message", retryable: false, details: {} },
  });
}

function fullEventWire(): Record<string, unknown> {
  return {
    event_id: EVENT_ID,
    event_sequence: 39,
    event_type: "updated",
    source_id: SOURCE_ID,
    origin_device_id: ORIGIN_DEVICE_ID,
    base_version_id: SOURCE_VERSION_ID,
    current_version_id: CURRENT_VERSION_ID,
    base_fingerprint: { sha256: "a".repeat(64), size_bytes: 12, media_type: "text/markdown" },
    current_fingerprint: { sha256: "b".repeat(64), size_bytes: 13, media_type: "text/markdown" },
    prior_locator: "notes/old.md",
    resulting_locator: "notes/new.md",
    tombstone_id: null,
    committed_at: COMMITTED_AT,
  };
}

function eventPageBody(events: readonly unknown[]): string {
  return successBody({
    acknowledged_sequence: 12,
    delivered_through_sequence: 40,
    page_checkpoint_sequence: 40,
    has_more: true,
    events,
  });
}

function manifestRunReceiptBody(): string {
  return successBody({
    manifest_run_id: MANIFEST_RUN_ID,
    state: "collecting",
    base_acknowledged_sequence: 12,
    checkpoint_sequence: 12,
    policy_revision_number: 2,
    client_observation_generation: 7,
    next_page_number: 0,
    entry_count: 0,
    expires_at: EXPIRES_AT,
  });
}

function manifestPageReceiptBody(): string {
  return successBody({
    manifest_run_id: MANIFEST_RUN_ID,
    page_number: 0,
    accepted_entry_count: 2,
    next_page_number: 1,
  });
}

function manifestActionPageBody(): string {
  return successBody({
    manifest_run_id: MANIFEST_RUN_ID,
    has_more: false,
    actions: [
      {
        action_index: 0,
        action_kind: "download",
        local_entry_id: "entry-alpha",
        source_id: SOURCE_ID,
        source_version_id: SOURCE_VERSION_ID,
        source_locator_id: null,
        source_tombstone_id: null,
        reason: null,
      },
      {
        action_index: 1,
        action_kind: "conflict",
        local_entry_id: null,
        source_id: null,
        source_version_id: null,
        source_locator_id: null,
        source_tombstone_id: TOMBSTONE_ID,
        reason: "device_manifest_target_occupied",
      },
    ],
  });
}

function cursorReceiptBody(): string {
  return successBody({ acknowledged_sequence: 41, delivered_through_sequence: 41 });
}

const DOWNLOAD = { sourceId: SOURCE_ID, sourceVersionId: SOURCE_VERSION_ID };
const ACKNOWLEDGEMENT = { expectedPreviousSequence: 12, appliedThroughSequence: 40 };
const START_MANIFEST = { clientObservationGeneration: 7 };
const APPEND_PAGE = {
  manifestRunId: MANIFEST_RUN_ID,
  pageNumber: 0,
  pageDigest: "c".repeat(64),
  entries: [
    {
      localEntryId: "entry-alpha",
      normalizedLocator: "notes/one.md",
      fingerprint: { sha256: "a".repeat(64), sizeBytes: 12, mediaType: "text/markdown" },
      observationGeneration: 7,
      knownSourceId: SOURCE_ID,
      knownVersionId: SOURCE_VERSION_ID,
    },
  ],
};
const FINALIZE = { manifestRunId: MANIFEST_RUN_ID, totalEntryCount: 2, finalDigest: "d".repeat(64) };
const ACTIONS_QUERY = { manifestRunId: MANIFEST_RUN_ID, afterActionIndex: 0, limit: 200 };
const COMPLETE = { manifestRunId: MANIFEST_RUN_ID, finalDigest: "d".repeat(64) };

// --- strict JSON response parsing (step 1) -----------------------------------------------------------

describe("device sync wire client strict response parsing", () => {
  it("pulls one full strict event page with every optional member", async () => {
    const { api } = createApi({ transport: staticJsonTransport(200, eventPageBody([fullEventWire()])) });
    const page = await api.pullEvents();
    expect(page).toEqual({
      acknowledgedSequence: 12,
      deliveredThroughSequence: 40,
      pageCheckpointSequence: 40,
      hasMore: true,
      events: [
        {
          eventId: EVENT_ID,
          eventSequence: 39,
          operation: "updated",
          sourceId: SOURCE_ID,
          originDeviceId: ORIGIN_DEVICE_ID,
          baseVersionId: SOURCE_VERSION_ID,
          currentVersionId: CURRENT_VERSION_ID,
          baseFingerprint: { sha256: "a".repeat(64), sizeBytes: 12, mediaType: "text/markdown" },
          currentFingerprint: { sha256: "b".repeat(64), sizeBytes: 13, mediaType: "text/markdown" },
          priorLocator: "notes/old.md",
          resultingLocator: "notes/new.md",
          tombstoneId: null,
          committedAt: COMMITTED_AT,
        },
      ],
    });
  });

  it("pulls a minimal event whose optional members are absent", async () => {
    const { api } = createApi({
      transport: staticJsonTransport(
        200,
        eventPageBody([
          { event_id: EVENT_ID, event_sequence: 13, event_type: "deleted", source_id: SOURCE_ID, committed_at: COMMITTED_AT },
        ]),
      ),
    });
    const page = await api.pullEvents();
    expect(page.events[0]).toMatchObject({
      eventId: EVENT_ID,
      eventSequence: 13,
      operation: "deleted",
      originDeviceId: null,
      baseFingerprint: null,
      currentFingerprint: null,
      priorLocator: null,
      resultingLocator: null,
      tombstoneId: null,
    });
  });

  it.each([
    [
      "a non-UUID event id",
      (page: Record<string, unknown>) => ({
        ...page,
        events: [{ event_id: "not-a-uuid", event_sequence: 13, event_type: "created", source_id: SOURCE_ID, committed_at: COMMITTED_AT }],
      }),
    ],
    [
      "a non-integer event sequence",
      (page: Record<string, unknown>) => ({
        ...page,
        events: [{ event_id: EVENT_ID, event_sequence: 13.5, event_type: "created", source_id: SOURCE_ID, committed_at: COMMITTED_AT }],
      }),
    ],
    ["a negative acknowledged sequence", (page: Record<string, unknown>) => ({ ...page, acknowledged_sequence: -1 })],
    [
      "an unregistered event type",
      (page: Record<string, unknown>) => ({
        ...page,
        events: [{ event_id: EVENT_ID, event_sequence: 13, event_type: "exploded", source_id: SOURCE_ID, committed_at: COMMITTED_AT }],
      }),
    ],
    [
      "a malformed fingerprint digest",
      (page: Record<string, unknown>) => ({
        ...page,
        events: [
          {
            event_id: EVENT_ID,
            event_sequence: 13,
            event_type: "created",
            source_id: SOURCE_ID,
            committed_at: COMMITTED_AT,
            current_fingerprint: { sha256: "NOT_HEX", size_bytes: 4, media_type: "text/plain" },
          },
        ],
      }),
    ],
    ["a non-boolean has_more flag", (page: Record<string, unknown>) => ({ ...page, has_more: "yes" })],
    [
      "a missing events array",
      (page: Record<string, unknown>) => {
        const withoutEvents: Record<string, unknown> = { ...page };
        delete withoutEvents["events"];
        return withoutEvents;
      },
    ],
    [
      "a non-UUID source id in an event",
      (page: Record<string, unknown>) => ({
        ...page,
        events: [{ event_id: EVENT_ID, event_sequence: 13, event_type: "created", source_id: "source", committed_at: COMMITTED_AT }],
      }),
    ],
  ])("rejects a pull page carrying %s as a closed server_error", async (_label, mutate) => {
    const page = mutate({
      acknowledged_sequence: 12,
      delivered_through_sequence: 40,
      page_checkpoint_sequence: 40,
      has_more: false,
      events: [],
    });
    const { api } = createApi({ transport: staticJsonTransport(200, successBody(page)) });
    await expect(api.pullEvents()).rejects.toMatchObject({
      reason: "server_error",
      retryable: true,
      requestId: null,
    });
  });

  it("rejects a malformed envelope body as a retryable server_error", async () => {
    const { api } = createApi({ transport: staticJsonTransport(200, "<html>not json</html>") });
    await expect(api.pullEvents()).rejects.toMatchObject({ reason: "server_error" });
  });

  it("acknowledges the cursor with the exact strict body and parses the receipt", async () => {
    const transport = staticJsonTransport(200, cursorReceiptBody());
    const { api } = createApi({ transport });
    const receipt = await api.acknowledgeCursor(ACKNOWLEDGEMENT);
    expect(receipt).toEqual({ acknowledgedSequence: 41, deliveredThroughSequence: 41 });
    expect(transport.requests[0]).toMatchObject({
      url: `${ORIGIN}/api/sync/cursor-acknowledgements`,
      method: "POST",
    });
    expect(JSON.parse(transport.requests[0]?.body as string)).toEqual({
      expected_previous_sequence: 12,
      applied_through_sequence: 40,
    });
  });

  it("starts a manifest run with the exact body and parses the run receipt", async () => {
    const transport = staticJsonTransport(200, manifestRunReceiptBody());
    const { api } = createApi({ transport });
    const receipt = await api.startManifest(START_MANIFEST);
    expect(receipt).toEqual({
      manifestRunId: MANIFEST_RUN_ID,
      state: "collecting",
      baseAcknowledgedSequence: 12,
      checkpointSequence: 12,
      policyRevisionNumber: 2,
      clientObservationGeneration: 7,
      nextPageNumber: 0,
      entryCount: 0,
      expiresAt: EXPIRES_AT,
    });
    expect(transport.requests[0]).toMatchObject({ url: `${ORIGIN}/api/sync/manifests`, method: "POST" });
    expect(JSON.parse(transport.requests[0]?.body as string)).toEqual({ client_observation_generation: 7 });
  });

  it.each([
    ["an unregistered run state", { state: "wrecked" }],
    ["a malformed expiry timestamp", { expires_at: "18/08/2026" }],
    ["a non-integer entry count", { entry_count: 1.5 }],
    ["a non-UUID manifest run id", { manifest_run_id: "run" }],
  ])("rejects a run receipt carrying %s", async (_label, override) => {
    const data = {
      manifest_run_id: MANIFEST_RUN_ID,
      state: "collecting",
      base_acknowledged_sequence: 12,
      checkpoint_sequence: 12,
      policy_revision_number: 2,
      client_observation_generation: 7,
      next_page_number: 0,
      entry_count: 0,
      expires_at: EXPIRES_AT,
      ...override,
    };
    const { api } = createApi({ transport: staticJsonTransport(200, successBody(data)) });
    await expect(api.startManifest(START_MANIFEST)).rejects.toMatchObject({ reason: "server_error" });
  });

  it("appends one manifest page with the exact wire entry body", async () => {
    const transport = staticJsonTransport(200, manifestPageReceiptBody());
    const { api } = createApi({ transport });
    const receipt = await api.appendManifestPage(APPEND_PAGE);
    expect(receipt).toEqual({
      manifestRunId: MANIFEST_RUN_ID,
      pageNumber: 0,
      acceptedEntryCount: 2,
      nextPageNumber: 1,
    });
    expect(transport.requests[0]).toMatchObject({
      url: `${ORIGIN}/api/sync/manifests/${MANIFEST_RUN_ID}/pages/0`,
      method: "PUT",
    });
    expect(JSON.parse(transport.requests[0]?.body as string)).toEqual({
      entries: [
        {
          local_entry_id: "entry-alpha",
          normalized_locator: "notes/one.md",
          fingerprint: { sha256: "a".repeat(64), size_bytes: 12, media_type: "text/markdown" },
          observation_generation: 7,
          known_source_id: SOURCE_ID,
          known_version_id: SOURCE_VERSION_ID,
        },
      ],
      page_digest: "c".repeat(64),
    });
  });

  it("finalizes a run with the exact body and parses the run receipt", async () => {
    const transport = staticJsonTransport(200, manifestRunReceiptBody());
    const { api } = createApi({ transport });
    await api.finalizeManifest(FINALIZE);
    expect(transport.requests[0]).toMatchObject({
      url: `${ORIGIN}/api/sync/manifests/${MANIFEST_RUN_ID}/finalize`,
      method: "POST",
    });
    expect(JSON.parse(transport.requests[0]?.body as string)).toEqual({
      total_entry_count: 2,
      final_digest: "d".repeat(64),
    });
  });

  it("lists manifest actions with the bounded query and parses the action unions", async () => {
    const transport = staticJsonTransport(200, manifestActionPageBody());
    const { api } = createApi({ transport });
    const page = await api.listManifestActions(ACTIONS_QUERY);
    expect(transport.requests[0]).toMatchObject({
      url: `${ORIGIN}/api/sync/manifests/${MANIFEST_RUN_ID}/actions?after_action_index=0&limit=200`,
      method: "GET",
    });
    expect(transport.requests[0]?.body).toBeUndefined();
    expect(page).toEqual({
      manifestRunId: MANIFEST_RUN_ID,
      hasMore: false,
      actions: [
        {
          actionIndex: 0,
          actionKind: "download",
          localEntryId: "entry-alpha",
          sourceId: SOURCE_ID,
          sourceVersionId: SOURCE_VERSION_ID,
          sourceLocatorId: null,
          sourceTombstoneId: null,
          reason: null,
        },
        {
          actionIndex: 1,
          actionKind: "conflict",
          localEntryId: null,
          sourceId: null,
          sourceVersionId: null,
          sourceLocatorId: null,
          sourceTombstoneId: TOMBSTONE_ID,
          reason: "device_manifest_target_occupied",
        },
      ],
    });
  });

  it.each([
    ["an unregistered action kind", { action_index: 0, action_kind: "explode" }],
    ["an unregistered action reason", { action_index: 0, action_kind: "conflict", reason: "device_manifest_reason_unknown" }],
    ["a non-UUID source id", { action_index: 0, action_kind: "download", source_id: "source" }],
    ["a non-integer action index", { action_index: -1, action_kind: "no_change" }],
  ])("rejects an action page carrying %s", async (_label, override) => {
    const defaultAction: Record<string, unknown> = { action_index: 0, action_kind: "no_change" };
    const data = {
      manifest_run_id: MANIFEST_RUN_ID,
      has_more: false,
      actions: [{ ...defaultAction, ...override }],
    };
    const { api } = createApi({ transport: staticJsonTransport(200, successBody(data)) });
    await expect(api.listManifestActions(ACTIONS_QUERY)).rejects.toMatchObject({ reason: "server_error" });
  });

  it("completes a manifest run with the exact body and parses the cursor receipt", async () => {
    const transport = staticJsonTransport(200, cursorReceiptBody());
    const { api } = createApi({ transport });
    const receipt = await api.completeManifest(COMPLETE);
    expect(receipt).toEqual({ acknowledgedSequence: 41, deliveredThroughSequence: 41 });
    expect(transport.requests[0]).toMatchObject({
      url: `${ORIGIN}/api/sync/manifests/${MANIFEST_RUN_ID}/complete`,
      method: "POST",
    });
    expect(JSON.parse(transport.requests[0]?.body as string)).toEqual({ final_digest: "d".repeat(64) });
  });

  it("sends the bearer credential and JSON accept headers on every operation", async () => {
    const transport = staticJsonTransport(200, eventPageBody([]));
    const { api } = createApi({ transport });
    await api.pullEvents();
    const request = transport.requests[0];
    expect(request?.headers["authorization"]).toBe(`Bearer ${ACCESS_TOKEN}`);
    expect(request?.headers["accept"]).toBe("application/json");
    expect(request?.body).toBeUndefined();
  });
});

// --- verified binary download (step 1) ---------------------------------------------------------------

describe("device sync verified binary download", () => {
  it("returns the exact verified bytes with their declared digest, size and media type", async () => {
    const transport = await binaryResponse({
      contentLength: String(DOWNLOAD_BYTES.byteLength),
      sha256Header: await digestOf(DOWNLOAD_BYTES),
      contentType: "text/markdown",
      requestIdHeader: REQUEST_ID,
      bytes: DOWNLOAD_BYTES,
    });
    const { api } = createApi({ transport });
    const download = await api.downloadSourceVersion(DOWNLOAD);
    expect([...download.bytes]).toEqual([...DOWNLOAD_BYTES]);
    expect(download.declaredSha256).toBe(await digestOf(DOWNLOAD_BYTES));
    expect(download.sizeBytes).toBe(DOWNLOAD_BYTES.byteLength);
    expect(download.mediaType).toBe("text/markdown");
    expect(transport.requests[0]).toMatchObject({
      url: `${ORIGIN}/api/sources/${SOURCE_ID}/versions/${SOURCE_VERSION_ID}/content`,
      method: "GET",
    });
    expect(transport.requests[0]?.headers["accept"]).toBe("application/octet-stream");
  });

  it("rejects a truncated verified download", async () => {
    const truncated = DOWNLOAD_BYTES.slice(0, 7);
    const { api } = createApi({
      transport: await binaryResponse({ contentLength: "8", sha256Header: await digestOf(DOWNLOAD_BYTES), bytes: truncated }),
    });
    await expect(api.downloadSourceVersion(DOWNLOAD)).rejects.toMatchObject({
      reason: "device_download_integrity_failed",
      retryable: false,
    });
  });

  it("rejects a download whose digest does not match the declared SHA-256", async () => {
    const { api } = createApi({
      transport: await binaryResponse({
        contentLength: String(DOWNLOAD_BYTES.byteLength),
        sha256Header: (await digestOf(DOWNLOAD_BYTES)).slice(0, 63) + "0",
        bytes: DOWNLOAD_BYTES,
      }),
    });
    await expect(api.downloadSourceVersion(DOWNLOAD)).rejects.toMatchObject({
      reason: "device_download_integrity_failed",
    });
  });

  it.each([
    ["a missing content-length", { contentLength: null }],
    ["a non-numeric content-length", { contentLength: "about 38 bytes" }],
    ["a missing x-content-sha256 header", { sha256Header: null }],
    ["a non-hex x-content-sha256 header", { sha256Header: "z".repeat(64) }],
    ["a missing content-type", { contentType: null }],
    ["a non-canonical content-type", { contentType: "Text/Markdown" }],
  ])("rejects a download carrying %s", async (_label, override) => {
    const spec = {
      contentLength: String(DOWNLOAD_BYTES.byteLength),
      sha256Header: await digestOf(DOWNLOAD_BYTES),
      contentType: "text/markdown",
      bytes: DOWNLOAD_BYTES,
      ...override,
    };
    const { api } = createApi({ transport: await binaryResponse(spec) });
    await expect(api.downloadSourceVersion(DOWNLOAD)).rejects.toMatchObject({
      reason: "device_download_integrity_failed",
    });
  });

  it("maps a pre-stream download error envelope onto its registered reason", async () => {
    const { api } = createApi({ transport: staticJsonTransport(422, errorBody("device_download_integrity_failed")) });
    await expect(api.downloadSourceVersion(DOWNLOAD)).rejects.toMatchObject({
      reason: "device_download_integrity_failed",
      requestId: REQUEST_ID,
      wireErrorCode: "device_download_integrity_failed",
    });
  });
});

// --- authentication and diagnostics (step 2) ---------------------------------------------------------

describe("device sync client authentication and diagnostics", () => {
  it("avoids the transport entirely and records access_missing without a token", async () => {
    const transport = jsonTransport(async () => ({ status: 200, bodyText: cursorReceiptBody() }));
    const { api, diagnostics } = createApi({ transport, accessToken: null });
    await expect(api.pullEvents()).rejects.toMatchObject({ reason: "login_required", retryable: false });
    await expect(api.startManifest(START_MANIFEST)).rejects.toMatchObject({ reason: "login_required" });
    await expect(api.downloadSourceVersion(DOWNLOAD)).rejects.toMatchObject({ reason: "login_required" });
    expect(transport.requests).toHaveLength(0);
    expect(diagnostics.observations).toEqual([
      { kind: "credential_failure", stage: "access_missing", reason: "login_required", correlation: undefined },
      { kind: "credential_failure", stage: "access_missing", reason: "login_required", correlation: undefined },
      { kind: "credential_failure", stage: "access_missing", reason: "login_required", correlation: undefined },
    ]);
  });

  it("treats an empty access token as missing", async () => {
    const transport = jsonTransport(async () => ({ status: 200, bodyText: cursorReceiptBody() }));
    const { api } = createApi({ transport, accessToken: "" });
    await expect(api.pullEvents()).rejects.toMatchObject({ reason: "login_required" });
    expect(transport.requests).toHaveLength(0);
  });

  it("records the cursor lane with network_offline when the transport rejects", async () => {
    const transport = jsonTransport(async () => {
      throw new Error("connection refused");
    });
    const { api, diagnostics } = createApi({ transport });
    await expect(api.pullEvents()).rejects.toMatchObject({ reason: "network_offline", retryable: true });
    expect(diagnostics.observations).toEqual([
      {
        kind: "cursor_failure",
        stage: "pull",
        reason: "network_offline",
        correlation: { requestId: null, wireErrorCode: null },
      },
    ]);
  });

  it("classifies an aborted transport as a network timeout", async () => {
    const transport = jsonTransport(async () => {
      throw new DOMException("The request was aborted", "AbortError");
    });
    const { api } = createApi({ transport });
    await expect(api.pullEvents()).rejects.toMatchObject({ reason: "network_timeout", retryable: true });
  });

  it("records each manifest operation's own reconcile stage on a transport rejection", async () => {
    const failing = jsonTransport(async () => {
      throw new TypeError("fetch failed");
    });
    const { api, diagnostics } = createApi({ transport: failing });
    await expect(api.startManifest(START_MANIFEST)).rejects.toMatchObject({ reason: "network_offline" });
    await expect(api.appendManifestPage(APPEND_PAGE)).rejects.toMatchObject({ reason: "network_offline" });
    await expect(api.finalizeManifest(FINALIZE)).rejects.toMatchObject({ reason: "network_offline" });
    await expect(api.listManifestActions(ACTIONS_QUERY)).rejects.toMatchObject({ reason: "network_offline" });
    await expect(api.completeManifest(COMPLETE)).rejects.toMatchObject({ reason: "network_offline" });
    expect(diagnostics.observations.map((observation) => [observation.kind, observation.stage])).toEqual([
      ["reconcile_failure", "start"],
      ["reconcile_failure", "page"],
      ["reconcile_failure", "finalize"],
      ["reconcile_failure", "actions"],
      ["reconcile_failure", "complete"],
    ]);
  });

  it("records the apply lane when the download transport rejects", async () => {
    const failing = jsonTransport(async () => {
      throw new Error("network gone");
    });
    const { api, diagnostics } = createApi({ transport: failing });
    await expect(api.downloadSourceVersion(DOWNLOAD)).rejects.toMatchObject({ reason: "network_offline" });
    expect(diagnostics.observations).toEqual([
      {
        kind: "apply_failure",
        stage: "download",
        reason: "network_offline",
        correlation: { requestId: null, wireErrorCode: null },
      },
    ]);
  });

  it("maps a reached 429 onto network_rate_limited with its gated correlation", async () => {
    const { api, diagnostics } = createApi({ transport: staticJsonTransport(429, errorBody("authentication_rate_limited")) });
    const failure = classifyDeviceSyncFailure(await api.pullEvents().catch((error: unknown) => error));
    expect(failure).toEqual({
      reason: "network_rate_limited",
      retryable: true,
      correlation: { requestId: REQUEST_ID, wireErrorCode: "authentication_rate_limited" },
    });
    expect(diagnostics.observations).toEqual([
      {
        kind: "cursor_failure",
        stage: "pull",
        reason: "network_rate_limited",
        correlation: { requestId: REQUEST_ID, wireErrorCode: "authentication_rate_limited" },
      },
    ]);
  });

  it("maps a reached 5xx without an envelope onto a retryable server_error", async () => {
    const { api, diagnostics } = createApi({ transport: staticJsonTransport(502, "Bad Gateway") });
    await expect(api.acknowledgeCursor(ACKNOWLEDGEMENT)).rejects.toMatchObject({
      reason: "server_error",
      retryable: true,
      requestId: null,
    });
    expect(diagnostics.observations).toEqual([
      {
        kind: "cursor_failure",
        stage: "acknowledge",
        reason: "server_error",
        correlation: { requestId: null, wireErrorCode: null },
      },
    ]);
  });

  it("keeps the retryable dependency outage retryable with its registered code", async () => {
    const { api } = createApi({ transport: staticJsonTransport(503, errorBody("device_sync_dependency_unavailable")) });
    await expect(api.startManifest(START_MANIFEST)).rejects.toMatchObject({
      reason: "device_sync_dependency_unavailable",
      retryable: true,
      wireErrorCode: "device_sync_dependency_unavailable",
    });
  });

  it("maps a reached 401 onto access_expired without retry", async () => {
    const { api } = createApi({ transport: staticJsonTransport(401, errorBody("device_credential_invalid")) });
    await expect(api.pullEvents()).rejects.toMatchObject({
      reason: "access_expired",
      retryable: false,
      wireErrorCode: "device_credential_invalid",
    });
  });

  it("maps a genuine 403 envelope onto login_required and an edge 403 onto server_error", async () => {
    const genuine = createApi({ transport: staticJsonTransport(403, errorBody("authorization_scope_denied")) });
    await expect(genuine.api.pullEvents()).rejects.toMatchObject({ reason: "login_required", retryable: false });
    const edge = createApi({ transport: staticJsonTransport(403, "<html>challenge</html>") });
    await expect(edge.api.pullEvents()).rejects.toMatchObject({ reason: "server_error", retryable: true });
  });

  it("lands every registered device-sync server code verbatim", async () => {
    for (const code of [
      "device_cursor_gap",
      "device_cursor_regression",
      "device_cursor_ack_ahead",
      "device_event_unavailable",
      "device_event_integrity_failed",
      "device_manifest_not_found",
      "device_manifest_expired",
      "device_manifest_state_invalid",
      "device_manifest_page_invalid",
      "device_manifest_page_replay_mismatch",
      "device_manifest_digest_mismatch",
      "device_manifest_policy_advanced",
    ] as const) {
      const { api } = createApi({ transport: staticJsonTransport(409, errorBody(code)) });
      await expect(api.pullEvents(), code).rejects.toMatchObject({ reason: code, retryable: false, requestId: REQUEST_ID });
    }
  });

  it("drops a non-UUID envelope request id from the failure correlation", async () => {
    const { api } = createApi({ transport: staticJsonTransport(409, errorBody("device_cursor_gap", "request-17")) });
    await expect(api.pullEvents()).rejects.toMatchObject({
      reason: "device_cursor_gap",
      requestId: null,
      wireErrorCode: "device_cursor_gap",
    });
  });

  it("records the gated correlation of an envelope failure on the trail lane", async () => {
    const { api, diagnostics } = createApi({
      transport: staticJsonTransport(409, errorBody("device_manifest_policy_advanced")),
    });
    await expect(api.finalizeManifest(FINALIZE)).rejects.toMatchObject({ reason: "device_manifest_policy_advanced" });
    expect(diagnostics.observations).toEqual([
      {
        kind: "reconcile_failure",
        stage: "finalize",
        reason: "device_manifest_policy_advanced",
        correlation: { requestId: REQUEST_ID, wireErrorCode: "device_manifest_policy_advanced" },
      },
    ]);
  });

  it("records the header-gated correlation of a binary integrity failure", async () => {
    const truncated = DOWNLOAD_BYTES.slice(0, 7);
    const { api, diagnostics } = createApi({
      transport: await binaryResponse({ contentLength: "8", sha256Header: await digestOf(DOWNLOAD_BYTES), requestIdHeader: REQUEST_ID, bytes: truncated }),
    });
    await expect(api.downloadSourceVersion(DOWNLOAD)).rejects.toMatchObject({ reason: "device_download_integrity_failed" });
    expect(diagnostics.observations).toEqual([
      {
        kind: "apply_failure",
        stage: "download",
        reason: "device_download_integrity_failed",
        correlation: { requestId: REQUEST_ID, wireErrorCode: null },
      },
    ]);
  });

  it("never carries the URL, a status number or body text in a thrown error", async () => {
    const { api } = createApi({ transport: staticJsonTransport(503, "OPERATIONAL INCIDENT DETAIL") });
    const error = await api.pullEvents().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(DeviceSyncApiError);
    expect((error as Error).message).toBe("device sync api failed: server_error");
    expect((error as Error).message).not.toContain(ORIGIN);
    expect((error as Error).message).not.toContain("503");
    expect((error as Error).message).not.toContain("OPERATIONAL");
  });

  it("resolves the origin and credential afresh on every request", async () => {
    let origin = ORIGIN;
    let accessToken: string | null = ACCESS_TOKEN;
    const transport = staticJsonTransport(200, cursorReceiptBody());
    const api = createDeviceSyncApi({
      transport,
      resolveOrigin: () => origin,
      getAccessToken: () => accessToken,
      diagnostics: new RecordingDiagnostics(),
    });
    await api.acknowledgeCursor(ACKNOWLEDGEMENT);
    origin = SECOND_ORIGIN;
    accessToken = SECOND_ACCESS_TOKEN;
    await api.acknowledgeCursor(ACKNOWLEDGEMENT);
    expect(transport.requests[1]?.url).toBe(`${SECOND_ORIGIN}/api/sync/cursor-acknowledgements`);
    expect(transport.requests[1]?.headers["authorization"]).toBe(`Bearer ${SECOND_ACCESS_TOKEN}`);
  });
});

// --- the closed failure classifier -------------------------------------------------------------------

describe("classifyDeviceSyncFailure", () => {
  it("mirrors one device sync api error", () => {
    const error = new DeviceSyncApiError("device_cursor_gap", false, REQUEST_ID, "device_cursor_gap");
    expect(classifyDeviceSyncFailure(error)).toEqual({
      reason: "device_cursor_gap",
      retryable: false,
      correlation: { requestId: REQUEST_ID, wireErrorCode: "device_cursor_gap" },
    });
  });

  it("maps a TypeError-class failure onto retryable network_offline", () => {
    expect(classifyDeviceSyncFailure(new TypeError("fetch failed"))).toEqual({
      reason: "network_offline",
      retryable: true,
      correlation: undefined,
    });
  });

  it("maps every unclassified failure onto retryable server_error", () => {
    expect(classifyDeviceSyncFailure(new Error("anything"))).toEqual({
      reason: "server_error",
      retryable: true,
      correlation: undefined,
    });
    expect(classifyDeviceSyncFailure("a raw string")).toEqual({
      reason: "server_error",
      retryable: true,
      correlation: undefined,
    });
    expect(classifyDeviceSyncFailure(undefined)).toEqual({
      reason: "server_error",
      retryable: true,
      correlation: undefined,
    });
  });
});
