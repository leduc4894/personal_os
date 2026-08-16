import { describe, expect, it, vi } from "vitest";

import {
  DEVICE_CREDENTIAL_RECORD_NAME,
  isSecretRecordNameValid,
  parseDeviceSecretRecord,
  readDeviceSecretRecord,
  writeActiveDeviceRecord,
  writeClearedTombstone,
  writePendingGrantRecord,
} from "./secret-storage-record";
import type { SecretStorageRecordAdapter } from "./contracts";

const POLLING_SECRET = "pg1.6e5cb1a2-0000-4000-8000-00000000000a.polling-secret";
const REFRESH_CREDENTIAL = "rt1.33333333-3333-4333-8333-333333333333.refresh-secret";

function createSecretStorage(initial: Record<string, string> = {}): {
  store: SecretStorageRecordAdapter;
  stored: Map<string, string>;
} {
  const stored = new Map<string, string>(Object.entries(initial));
  const store: SecretStorageRecordAdapter = {
    setSecret: vi.fn((recordName: string, value: string) => {
      stored.set(recordName, value);
    }),
    getSecret: vi.fn((recordName: string) => stored.get(recordName) ?? null),
  };
  return { store, stored };
}

function lastStoredJson(store: SecretStorageRecordAdapter): string {
  const calls = (store.setSecret as ReturnType<typeof vi.fn>).mock.calls;
  const lastCall = calls[calls.length - 1];
  if (lastCall === undefined) {
    throw new Error("setSecret was never called");
  }
  return String(lastCall[1]);
}

describe("SecretStorage record naming", () => {
  it("uses a record name of lowercase ASCII letters, digits and dashes only", () => {
    expect(isSecretRecordNameValid(DEVICE_CREDENTIAL_RECORD_NAME)).toBe(true);
    expect(isSecretRecordNameValid("device-credential-2")).toBe(true);
  });

  it("rejects names outside the identifier grammar", () => {
    for (const invalidName of [
      "",
      "Device",
      "device_credential",
      "device credential",
      "device.credential",
      "credential/",
    ]) {
      expect(isSecretRecordNameValid(invalidName)).toBe(false);
    }
  });
});

describe("parseDeviceSecretRecord", () => {
  it("round-trips the pending-grant, active and cleared records", () => {
    const pending = parseDeviceSecretRecord(
      JSON.stringify({
        record_version: 1,
        state: "pending_grant",
        polling_secret: POLLING_SECRET,
      }),
    );
    expect(pending).toEqual({
      record_version: 1,
      state: "pending_grant",
      polling_secret: POLLING_SECRET,
    });

    const active = parseDeviceSecretRecord(
      JSON.stringify({
        record_version: 1,
        state: "active",
        refresh_credential: REFRESH_CREDENTIAL,
        refresh_generation: 3,
        pending_rotation_id: "22222222-2222-4222-8222-222222222222",
      }),
    );
    expect(active).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: REFRESH_CREDENTIAL,
      refresh_generation: 3,
      pending_rotation_id: "22222222-2222-4222-8222-222222222222",
    });

    const cleared = parseDeviceSecretRecord(
      JSON.stringify({ record_version: 1, state: "cleared", cleared_reason: "token_reuse" }),
    );
    expect(cleared).toEqual({ record_version: 1, state: "cleared", cleared_reason: "token_reuse" });
  });

  it("returns null for absent, corrupt or foreign values", () => {
    for (const invalidValue of [
      null,
      "",
      "not json",
      JSON.stringify({ record_version: 2, state: "active" }),
      JSON.stringify({ record_version: 1, state: "unknown_state" }),
      JSON.stringify({ record_version: 1, state: "cleared", cleared_reason: "not_a_reason" }),
      JSON.stringify([1, 2, 3]),
    ]) {
      expect(parseDeviceSecretRecord(invalidValue)).toBeNull();
    }
  });
});

describe("record writes verify their readback", () => {
  it("stores the pending-grant record and reads it back", () => {
    const { store, stored } = createSecretStorage();
    writePendingGrantRecord(store, DEVICE_CREDENTIAL_RECORD_NAME, POLLING_SECRET);
    expect(JSON.parse(stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "pending_grant",
      polling_secret: POLLING_SECRET,
    });
    expect(store.getSecret).toHaveBeenCalledWith(DEVICE_CREDENTIAL_RECORD_NAME);
  });

  it("stores the active record with its pending rotation identity", () => {
    const { store, stored } = createSecretStorage();
    writeActiveDeviceRecord(store, DEVICE_CREDENTIAL_RECORD_NAME, {
      refresh_credential: REFRESH_CREDENTIAL,
      refresh_generation: 4,
      pending_rotation_id: "22222222-2222-4222-8222-222222222222",
    });
    expect(JSON.parse(stored.get(DEVICE_CREDENTIAL_RECORD_NAME) ?? "{}")).toEqual({
      record_version: 1,
      state: "active",
      refresh_credential: REFRESH_CREDENTIAL,
      refresh_generation: 4,
      pending_rotation_id: "22222222-2222-4222-8222-222222222222",
    });
  });

  it("stores exactly the credential-free tombstone on clearing", () => {
    const { store } = createSecretStorage();
    writeClearedTombstone(store, DEVICE_CREDENTIAL_RECORD_NAME, "token_reuse");
    expect(JSON.parse(lastStoredJson(store))).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "token_reuse",
    });
    expect(lastStoredJson(store)).not.toContain("pg1.");
    expect(lastStoredJson(store)).not.toContain("rt1.");
    expect(lastStoredJson(store)).not.toContain("rotation");
  });

  it("refuses to proceed when the readback does not match the write", () => {
    const stored = new Map<string, string>();
    const mismatching: SecretStorageRecordAdapter = {
      setSecret: vi.fn((recordName: string, value: string) => {
        stored.set(recordName, value);
      }),
      getSecret: vi.fn(() => null),
    };
    expect(() =>
      writeClearedTombstone(mismatching, DEVICE_CREDENTIAL_RECORD_NAME, "self_disconnect"),
    ).toThrow();
  });

  it("reads the stored record back through the adapter", () => {
    const { store } = createSecretStorage({
      [DEVICE_CREDENTIAL_RECORD_NAME]: JSON.stringify({
        record_version: 1,
        state: "cleared",
        cleared_reason: "grant_expired",
      }),
    });
    expect(readDeviceSecretRecord(store, DEVICE_CREDENTIAL_RECORD_NAME)).toEqual({
      record_version: 1,
      state: "cleared",
      cleared_reason: "grant_expired",
    });
    expect(readDeviceSecretRecord(store, "absent-record")).toBeNull();
  });
});
