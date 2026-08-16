import type { AdminDeviceData } from "./device-administration-client";

type DevicePlatformClass = AdminDeviceData["platform_class"];

const MISSING_VALUE = "—";

/** Formats one API timestamp for the device surfaces; absent values stay marked. */
export function formatDeviceTimestamp(isoTimestamp: string | null | undefined): string {
  if (isoTimestamp === null || isoTimestamp === undefined || isoTimestamp === "") {
    return MISSING_VALUE;
  }
  const parsed = new Date(isoTimestamp);
  if (Number.isNaN(parsed.getTime())) {
    return MISSING_VALUE;
  }
  return parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** The remaining pending-grant lifetime in whole minutes, never negative. */
export function remainingExpiryLabel(expiresAt: string, now: Date = new Date()): string {
  const remainingMs = new Date(expiresAt).getTime() - now.getTime();
  if (Number.isNaN(remainingMs)) {
    return "unknown time";
  }
  const remainingMinutes = Math.max(0, Math.ceil(remainingMs / 60_000));
  return `${remainingMinutes} minute${remainingMinutes === 1 ? "" : "s"}`;
}

/** The human class label of one plugin platform (spec 18.3 display fields). */
export function platformClassLabel(platformClass: DevicePlatformClass): string {
  return platformClass === "obsidian_desktop" ? "Desktop" : "Mobile";
}
