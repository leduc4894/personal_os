"use client";

import { useSyncExternalStore } from "react";

import type { SessionData } from "../../api/authentication-client";

/**
 * Memory-only session state. Nothing here ever reaches ``localStorage`` or
 * ``sessionStorage``; the authoritative session lives in the API's cookie.
 */
export interface AuthenticationSessionStore {
  getSession(): SessionData | null;
  setSession(session: SessionData): void;
  clear(): void;
  subscribe(listener: () => void): () => void;
}

export function createAuthenticationSessionStore(): AuthenticationSessionStore {
  let currentSession: SessionData | null = null;
  const listeners = new Set<() => void>();

  function emit(): void {
    for (const listener of listeners) {
      listener();
    }
  }

  return {
    getSession: () => currentSession,
    setSession(session) {
      currentSession = session;
      emit();
    },
    clear() {
      currentSession = null;
      emit();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/**
 * The module-level store the browser pages share. It is deliberately not
 * persisted: a page reload re-derives everything from ``GET /api/auth/session``.
 */
export const browserSessionStore: AuthenticationSessionStore = createAuthenticationSessionStore();

export function useAuthenticationSession(store: AuthenticationSessionStore): SessionData | null {
  return useSyncExternalStore(store.subscribe, store.getSession, store.getSession);
}
