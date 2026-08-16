import { describe, expect, it } from "vitest";

import { formatDeviceTimestamp, platformClassLabel, remainingExpiryLabel } from "./device-display";

describe("formatDeviceTimestamp", () => {
  it("renders a localized label for a valid timestamp", () => {
    const label = formatDeviceTimestamp("2027-02-16T09:00:00Z");
    expect(label).not.toBe("—");
    expect(label).toContain("2027");
    expect(label).not.toBe("2027-02-16T09:00:00Z");
  });

  it("uses a blank marker for absent and invalid timestamps", () => {
    expect(formatDeviceTimestamp(null)).toBe("—");
    expect(formatDeviceTimestamp(undefined)).toBe("—");
    expect(formatDeviceTimestamp("")).toBe("—");
    expect(formatDeviceTimestamp("not-a-timestamp")).toBe("—");
  });
});

describe("remainingExpiryLabel", () => {
  const now = new Date("2026-08-16T09:00:00Z");

  it("rounds the remaining lifetime up to whole minutes", () => {
    expect(remainingExpiryLabel("2026-08-16T09:10:00Z", now)).toBe("10 minutes");
    expect(remainingExpiryLabel("2026-08-16T09:09:31Z", now)).toBe("10 minutes");
    expect(remainingExpiryLabel("2026-08-16T09:00:30Z", now)).toBe("1 minute");
  });

  it("never reports a negative lifetime", () => {
    expect(remainingExpiryLabel("2026-08-16T08:59:00Z", now)).toBe("0 minutes");
  });

  it("flags an unparseable expiry", () => {
    expect(remainingExpiryLabel("not-a-timestamp", now)).toBe("unknown time");
  });
});

describe("platformClassLabel", () => {
  it("maps the closed platform classes to human labels", () => {
    expect(platformClassLabel("obsidian_desktop")).toBe("Desktop");
    expect(platformClassLabel("obsidian_mobile")).toBe("Mobile");
  });
});
