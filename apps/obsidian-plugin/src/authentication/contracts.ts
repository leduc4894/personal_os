/**
 * Closed contracts of the plugin-side device authentication (spec 11, 12, 13,
 * 14, 19).
 *
 * The generated workspace client `@workspace/api-client` cannot be loaded
 * inside Obsidian's module graph, so this module hand-writes ONLY the wire
 * shapes the plugin consumes. Every shape mirrors
 * `packages/api-client/src/generated/schema.ts` (operations
 * `createDeviceAuthorization`, `pollDeviceAuthorization`,
 * `refreshDeviceToken`, `revokeCurrentDeviceToken` and the
 * `ApiEnvelope_*`/`ApiErrorBody` components); the bundle-boundary contract
 * test keeps this mirror honest from the Python side.
 */

/** The closed connection states of spec 19, exactly. */
export const CONNECTION_STATES = [
  "not_connected",
  "requesting_authorization",
  "waiting_for_approval",
  "connected",
  "offline",
  "refresh_required",
  "revoked",
  "configuration_invalid",
] as const;

export type ConnectionState = (typeof CONNECTION_STATES)[number];

/** One closed, non-empty status label per connection state (spec 19). */
export const CONNECTION_STATUS_TEXT: Readonly<Record<ConnectionState, string>> = {
  not_connected: "Not connected",
  requesting_authorization: "Requesting authorization…",
  waiting_for_approval: "Waiting for approval",
  connected: "Connected",
  offline: "Offline — credentials preserved",
  refresh_required: "Refresh required",
  revoked: "Revoked",
  configuration_invalid: "Configuration invalid",
};

export interface AuthenticationControls {
  readonly canLogin: boolean;
  /**
   * The offline dead-end escape (plugin hygiene, 2026-08-16 §12): Retry
   * connection is enabled exactly while offline WITH an active credential —
   * the one recoverable state that previously required a plugin reload.
   */
  readonly canRetryConnection: boolean;
  readonly canOpenBrowser: boolean;
  readonly canCancel: boolean;
  readonly canDisconnect: boolean;
}

/**
 * Derive the spec-19 control availability from credential facts: Login only
 * from the unconnected/closed-configuration states, Retry connection only
 * while offline with an active credential, browser/cancel while a pending
 * grant exists, Disconnect while an active credential exists.
 */
export function resolveAuthenticationControls(
  state: ConnectionState,
  facts: { hasPendingGrant: boolean; hasActiveCredential: boolean },
): AuthenticationControls {
  return {
    canLogin:
      !facts.hasActiveCredential &&
      !facts.hasPendingGrant &&
      state !== "requesting_authorization" &&
      state !== "waiting_for_approval",
    canRetryConnection: state === "offline" && facts.hasActiveCredential,
    canOpenBrowser: facts.hasPendingGrant,
    canCancel: facts.hasPendingGrant,
    canDisconnect: facts.hasActiveCredential,
  };
}

/** The approved plugin version window of a 426 rejection (spec 17 details). */
export interface ApprovedVersionBounds {
  readonly minimum: string;
  readonly maximum: string;
}

export interface DeviceAuthErrorOptions {
  readonly status: number;
  readonly message: string;
  readonly retryAfterSeconds?: number | null;
  readonly approvedVersionBounds?: ApprovedVersionBounds | null;
  readonly isLocal?: boolean;
}

/**
 * One mapped device-authentication failure: registry code, HTTP status and
 * only the registered safe details. The message never carries credentials,
 * user codes, rejected values or provider text (spec 20.4).
 */
export class DeviceAuthError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryAfterSeconds: number | null;
  readonly approvedVersionBounds: ApprovedVersionBounds | null;
  readonly isLocal: boolean;

  constructor(code: string, options: DeviceAuthErrorOptions) {
    super(options.message);
    this.name = "DeviceAuthError";
    this.code = code;
    this.status = options.status;
    this.retryAfterSeconds = options.retryAfterSeconds ?? null;
    this.approvedVersionBounds = options.approvedVersionBounds ?? null;
    this.isLocal = options.isLocal ?? false;
  }
}

/**
 * Whether a thrown value is one mapped device-authentication failure. This
 * guard replaces every `as DeviceAuthError` cast at the failure
 * classification sites (plugin hygiene, 2026-08-16 §12): a foreign error
 * whose `code` property happens to collide with a registry code can never
 * pass and never reaches a terminal branch.
 */
export function isDeviceAuthError(error: unknown): error is DeviceAuthError {
  return error instanceof DeviceAuthError;
}

/**
 * Resolve the closed transport code a thrown value carries, or null when the
 * value is not a mapped device-authentication failure (closed-reason
 * surfacing C2). Only the already-closed code vocabulary can pass: the
 * exception message and any foreign error's raw properties never do, so the
 * result is safe to render as a state-change detail.
 */
export function resolveDeviceAuthClosedCode(error: unknown): string | null {
  if (error instanceof DeviceAuthError && error.code !== "") {
    return error.code;
  }
  return null;
}

// --- hand-written wire shapes (mirror schema.ts) -------------------------------------

/** Mirrors `DevicePlatformClass` of schema.ts (spec 11.1). */
export type DevicePlatformClassWire = "obsidian_desktop" | "obsidian_mobile";

/** Mirrors `DeviceGrantRequest` of schema.ts (spec 11.1). */
export interface DeviceGrantWireRequest {
  readonly client_instance_id: string;
  readonly device_name: string;
  readonly platform_class: DevicePlatformClassWire;
  readonly platform_name: string;
  readonly plugin_version: string;
  readonly requested_scope: "obsidian_sync";
}

/** Mirrors `DeviceGrantData` of schema.ts (spec 11.1). */
export interface DeviceGrantWireData {
  readonly grant_id: string;
  readonly user_code: string;
  readonly polling_secret: string;
  readonly verification_uri: string;
  readonly verification_uri_complete: string;
  readonly expires_in_seconds: number;
  readonly poll_interval_seconds: number;
}

/** Mirrors `DeviceGrantExchangeData` of schema.ts (spec 12.1, 12.2). */
export interface DeviceGrantExchangeWireData {
  readonly grant_id: string;
  readonly device_id: string;
  readonly token_family_id: string;
  readonly refresh_generation: number;
  readonly access_credential: string;
  readonly refresh_credential: string;
  readonly access_expires_at: string;
  readonly refresh_expires_at: string;
}

/** Mirrors `RefreshedDeviceTokenData` of schema.ts (spec 13.3, 13.4). */
export interface RefreshedDeviceTokenWireData {
  readonly token_family_id: string;
  readonly refresh_generation: number;
  readonly access_credential: string;
  readonly refresh_credential: string;
  readonly access_expires_at: string;
  readonly refresh_expires_at: string;
  readonly family_absolute_expires_at: string;
}

/** Mirrors `DeviceSelfRevokeData` of schema.ts (spec 14.2). */
export interface DeviceSelfRevokeWireData {
  readonly device_id: string;
  readonly token_family_id: string;
  readonly revoked_at: string;
}

/** Mirrors `ApiErrorBody` of schema.ts (registry code + safe details). */
export interface ApiWireErrorBody {
  readonly code: string;
  readonly message: string;
  readonly details: Readonly<Record<string, unknown>>;
  readonly retryable: boolean;
}

/** Mirrors the `ApiEnvelope_*` components of schema.ts. */
export interface ApiWireEnvelope<TData> {
  readonly data: TData | null;
  readonly error: ApiWireErrorBody | null;
  readonly request_id: string;
  readonly warnings: readonly unknown[];
}

// --- transport ------------------------------------------------------------------------

export interface DeviceHttpRequest {
  readonly url: string;
  readonly method: "POST";
  readonly headers: Readonly<Record<string, string>>;
  readonly body: string;
}

export interface DeviceHttpResponse {
  readonly status: number;
  readonly bodyText: string;
}

export type DeviceHttpTransport = (request: DeviceHttpRequest) => Promise<DeviceHttpResponse>;

export interface DeviceApiTransport {
  createGrant(request: DeviceGrantWireRequest): Promise<DeviceGrantWireData>;
  pollGrant(grantId: string, pollingSecret: string): Promise<DeviceGrantExchangeWireData>;
  refresh(
    refreshCredential: string,
    rotationId: string,
  ): Promise<RefreshedDeviceTokenWireData>;
  revokeCurrent(refreshCredential: string): Promise<DeviceSelfRevokeWireData>;
}

function parseApprovedVersionBounds(
  details: Readonly<Record<string, unknown>>,
): ApprovedVersionBounds | null {
  const raw = details["approved_version_bounds"];
  if (!Array.isArray(raw) || raw.length < 2) {
    return null;
  }
  const [minimum, maximum] = raw;
  if (typeof minimum !== "string" || typeof maximum !== "string") {
    return null;
  }
  return { minimum, maximum };
}

function mapErrorEnvelope(status: number, body: ApiWireErrorBody): DeviceAuthError {
  const retryAfterRaw = body.details["retry_after_seconds"];
  return new DeviceAuthError(body.code, {
    status,
    message: body.message,
    retryAfterSeconds: typeof retryAfterRaw === "number" ? retryAfterRaw : null,
    approvedVersionBounds: parseApprovedVersionBounds(body.details),
  });
}

function parseEnvelope(status: number, bodyText: string): {
  data: unknown;
  error: ApiWireErrorBody | null;
} {
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText) as unknown;
  } catch {
    throw new DeviceAuthError("api_request_malformed", {
      status,
      message: "the server response was not valid JSON",
      isLocal: true,
    });
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new DeviceAuthError("api_request_malformed", {
      status,
      message: "the server response was not an envelope object",
      isLocal: true,
    });
  }
  const envelope = parsed as Partial<ApiWireEnvelope<unknown>>;
  if (envelope.error !== null && envelope.error !== undefined) {
    throw mapErrorEnvelope(status, envelope.error);
  }
  if (envelope.data === null || envelope.data === undefined) {
    throw new DeviceAuthError("api_request_malformed", {
      status,
      message: "the server response envelope carried no data",
      isLocal: true,
    });
  }
  return { data: envelope.data, error: null };
}

/**
 * Build the minimal typed device transport over one injected HTTP adapter.
 * Every call resolves the origin afresh so settings edits apply without a
 * rebuild, presents credentials only in the dedicated Bearer header, and maps
 * failures to `DeviceAuthError` with no logging of any content.
 */
export function createDeviceApiTransport(
  http: DeviceHttpTransport,
  resolveOrigin: () => string,
): DeviceApiTransport {
  async function post(path: string, headers: Record<string, string>, body: unknown): Promise<unknown> {
    const request: DeviceHttpRequest = {
      url: `${resolveOrigin()}${path}`,
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json", ...headers },
      body: JSON.stringify(body),
    };
    let response: DeviceHttpResponse;
    try {
      response = await http(request);
    } catch {
      throw new DeviceAuthError("network_unavailable", {
        status: 0,
        message: "the server could not be reached",
        isLocal: true,
      });
    }
    return parseEnvelope(response.status, response.bodyText).data;
  }

  return {
    async createGrant(request) {
      // This is a native Obsidian requestUrl call, not a browser fetch: no
      // Origin header is forged and the request URL itself targets the
      // configured public origin — the browser security boundary begins at
      // the server-minted verification URL.
      return (await post("/api/auth/device-authorizations", {}, request)) as DeviceGrantWireData;
    },
    async pollGrant(grantId, pollingSecret) {
      return (await post(
        `/api/auth/device-authorizations/${encodeURIComponent(grantId)}/poll`,
        { authorization: `Bearer ${pollingSecret}` },
        {},
      )) as DeviceGrantExchangeWireData;
    },
    async refresh(refreshCredential, rotationId) {
      return (await post(
        "/api/auth/device-tokens/refresh",
        { authorization: `Bearer ${refreshCredential}` },
        { rotation_id: rotationId },
      )) as RefreshedDeviceTokenWireData;
    },
    async revokeCurrent(refreshCredential) {
      return (await post(
        "/api/auth/device-tokens/revoke-current",
        { authorization: `Bearer ${refreshCredential}` },
        {},
      )) as DeviceSelfRevokeWireData;
    },
  };
}

// --- injected adapters ----------------------------------------------------------------

/**
 * The narrow SecretStorage surface the plugin relies on (Obsidian 1.11.4+).
 * The real binding is vault-local key/value storage with no delete API, so no
 * caller may treat a record as removed — only overwritten.
 */
export interface SecretStorageRecordAdapter {
  setSecret(recordName: string, value: string): void;
  getSecret(recordName: string): string | null;
}

export type EpochMsClock = () => number;
export type UuidFactory = () => string;
export type UrlOpener = (url: string) => void;
export type Delay = (milliseconds: number) => Promise<void>;

// --- settings and origin validation ---------------------------------------------------

export interface PendingGrantSettings {
  grant_id: string;
  user_code: string;
  verification_uri: string;
  expires_at_epoch_seconds: number;
  poll_interval_seconds: number;
}

/**
 * The non-secret plugin data (spec 11.1, 19): server origin, device/client
 * metadata and the SecretStorage record name. No credential material is ever
 * stored here.
 */
export interface DeviceAuthenticationSettings {
  server_origin: string;
  device_name: string;
  client_instance_id: string;
  /**
   * The server-minted device id (uuid7) the grant exchange assigned to
   * this plugin instance (fix round 1, blocker A): the ONLY identity the
   * device-event `origin_device_id` namespace uses, so the device-sync
   * coordinator's self-origin evidence binds it — never
   * `client_instance_id`, a disjoint client-minted v4 namespace. Null
   * before the first completed exchange.
   */
  device_id: string | null;
  secret_record_name: string | null;
  pending_grant: PendingGrantSettings | null;
}

const LOOPBACK_HOSTNAME_PATTERN = /^(?:localhost|.+\.localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::1\])$/;

/**
 * Validate one exact server origin (spec 19): HTTPS with no path, query,
 * fragment or embedded credential. Loopback HTTP is accepted only when the
 * explicit development-build flag allows it. Returns the normalized origin or
 * null.
 */
export function parseServerOrigin(
  origin: string,
  options: { allowLoopbackHttp: boolean },
): string | null {
  let url: URL;
  try {
    url = new URL(origin.trim());
  } catch {
    return null;
  }
  if (url.username !== "" || url.password !== "") {
    return null;
  }
  if (url.pathname !== "/" || url.search !== "" || url.hash !== "") {
    return null;
  }
  if (url.protocol === "https:") {
    return url.origin;
  }
  if (url.protocol === "http:" && options.allowLoopbackHttp) {
    if (LOOPBACK_HOSTNAME_PATTERN.test(url.hostname)) {
      return url.origin;
    }
    return null;
  }
  return null;
}

const DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS = 80;

/** Validate one trimmed 1–80 display-character device name (spec 11.1). */
export function validateDeviceName(name: string): string | null {
  const trimmed = name.trim();
  if (trimmed.length === 0 || trimmed.length > DEVICE_NAME_MAXIMUM_LENGTH_CHARACTERS) {
    return null;
  }
  return trimmed;
}
