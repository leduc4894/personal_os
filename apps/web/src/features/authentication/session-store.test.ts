import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { sessionData } from "../../testing/api-mock-builders";
import {
  createAuthenticationSessionStore,
  useAuthenticationSession,
} from "./session-store";

describe("createAuthenticationSessionStore", () => {
  it("keeps the session in memory only and never touches web storage", () => {
    const store = createAuthenticationSessionStore();
    act(() => {
      store.setSession(sessionData("active"));
    });
    expect(store.getSession()).toMatchObject({ state: "active" });
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
    act(() => {
      store.clear();
    });
    expect(store.getSession()).toBeNull();
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });

  it("notifies subscribers on every transition", () => {
    const store = createAuthenticationSessionStore();
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    act(() => {
      store.setSession(sessionData("pending_totp"));
    });
    act(() => {
      store.clear();
    });
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    act(() => {
      store.setSession(sessionData("active"));
    });
    expect(listener).toHaveBeenCalledTimes(2);
  });
});

describe("useAuthenticationSession", () => {
  it("reflects store transitions without persistence", () => {
    const store = createAuthenticationSessionStore();
    const { result } = renderHook(() => useAuthenticationSession(store));
    expect(result.current).toBeNull();
    act(() => {
      store.setSession(sessionData("active"));
    });
    expect(result.current).toMatchObject({ state: "active" });
    act(() => {
      store.clear();
    });
    expect(result.current).toBeNull();
    expect(localStorage).toHaveLength(0);
    expect(sessionStorage).toHaveLength(0);
  });
});
