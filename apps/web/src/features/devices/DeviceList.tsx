"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";

import { rateLimitedRetryMessage } from "../authentication/rate-limit-copy";
import {
  browserSessionStore,
  type AuthenticationSessionStore,
} from "../authentication/session-store";
import {
  createBrowserDeviceAdministrationClient,
  type AdminDeviceData,
  type DeviceAdministrationClient,
} from "./device-administration-client";
import { formatDeviceTimestamp, platformClassLabel } from "./device-display";
import { DeviceRevokeDialog } from "./DeviceRevokeDialog";

export interface DeviceListProps {
  client?: DeviceAdministrationClient;
  sessionStore?: AuthenticationSessionStore;
}

/**
 * The Admin devices surface (spec 18.3): one row per plugin device with
 * exactly the spec display fields. Revoked rows stay read-only; active rows
 * offer the guarded revocation dialog. The bootstrap device never appears —
 * the server excludes it from the list contract.
 */
export function DeviceList({
  client = createBrowserDeviceAdministrationClient(),
  sessionStore = browserSessionStore,
}: DeviceListProps): ReactNode {
  const router = useRouter();
  const [devices, setDevices] = useState<readonly AdminDeviceData[] | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [revokingDevice, setRevokingDevice] = useState<AdminDeviceData | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (errorMessage !== null) {
      errorRef.current?.focus();
    }
  }, [errorMessage]);

  const loadDevices = useCallback(async (): Promise<void> => {
    setErrorMessage(null);
    const result = await client.listAdminDevices();
    if (result.ok) {
      setDevices(result.data.devices);
      return;
    }
    if (result.error.code === "authentication_required") {
      router.replace("/login");
      return;
    }
    setErrorMessage(
      rateLimitedRetryMessage(result.error) ?? "The device list could not be loaded. Try again.",
    );
  }, [client, router]);

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
      sessionStore.setSession(result.data);
      void loadDevices();
    });
    return () => {
      cancelled = true;
    };
  }, [client, router, sessionStore, loadDevices]);

  if (devices === null && errorMessage === null) {
    return <p role="status">Loading devices…</p>;
  }

  return (
    <div className="device-list">
      {errorMessage !== null && (
        <p ref={errorRef} role="alert" tabIndex={-1} className="error-message">
          {errorMessage}
        </p>
      )}
      {devices !== null && devices.length === 0 && <p>No devices are registered yet.</p>}
      {devices !== null && devices.length > 0 && (
        <table>
          <caption className="visually-hidden">Registered plugin devices</caption>
          <thead>
            <tr>
              <th scope="col">Device name</th>
              <th scope="col">Type</th>
              <th scope="col">Platform</th>
              <th scope="col">Plugin version</th>
              <th scope="col">Status</th>
              <th scope="col">Registered</th>
              <th scope="col">Last seen</th>
              <th scope="col">Revoked</th>
              <th scope="col">Family expires</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {devices.map((device) => (
              <tr key={device.device_id}>
                <th scope="row">{device.device_name}</th>
                <td>{platformClassLabel(device.platform_class)}</td>
                <td>{device.platform_name}</td>
                <td>{device.plugin_version}</td>
                <td>{device.status === "revoked" ? "Revoked" : "Active"}</td>
                <td>
                  <time dateTime={device.registered_at}>
                    {formatDeviceTimestamp(device.registered_at)}
                  </time>
                </td>
                <td>
                  {device.last_seen_at === null ? (
                    "Not seen yet"
                  ) : (
                    <time dateTime={device.last_seen_at}>
                      {formatDeviceTimestamp(device.last_seen_at)}
                    </time>
                  )}
                </td>
                <td>
                  {device.revoked_at === null ? (
                    "—"
                  ) : (
                    <time dateTime={device.revoked_at}>{formatDeviceTimestamp(device.revoked_at)}</time>
                  )}
                </td>
                <td>
                  {device.family_absolute_expires_at === null ? (
                    "—"
                  ) : (
                    <time dateTime={device.family_absolute_expires_at}>
                      {formatDeviceTimestamp(device.family_absolute_expires_at)}
                    </time>
                  )}
                </td>
                <td>
                  {device.status === "active" ? (
                    <button
                      type="button"
                      aria-label={`Revoke ${device.device_name}`}
                      onClick={() => setRevokingDevice(device)}
                    >
                      Revoke
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {revokingDevice !== null && (
        <DeviceRevokeDialog
          client={client}
          device={revokingDevice}
          onClosed={() => setRevokingDevice(null)}
          onRevoked={() => {
            setRevokingDevice(null);
            void loadDevices();
          }}
        />
      )}
    </div>
  );
}
