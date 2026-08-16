"use client";

import { useEffect, type ReactNode } from "react";
import { useRouter } from "next/navigation";

import {
  createBrowserAuthenticationClient,
  type AuthenticationClient,
} from "../api/authentication-client";

export interface SessionRedirectProps {
  client?: AuthenticationClient;
}

/**
 * The workspace root decides its destination from ``GET /api/auth/session``
 * alone. Return URLs from the address bar are never honored.
 */
export function SessionRedirect({ client = createBrowserAuthenticationClient() }: SessionRedirectProps): ReactNode {
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;
    void client.getSession().then((result) => {
      if (cancelled) {
        return;
      }
      router.replace(result.ok && result.data.state === "active" ? "/admin/devices" : "/login");
    });
    return () => {
      cancelled = true;
    };
  }, [client, router]);

  return <p role="status">Checking your session…</p>;
}
