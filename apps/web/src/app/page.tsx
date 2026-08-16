import { connection } from "next/server";

import { SessionRedirect } from "./session-redirect";

export default async function WorkspaceRootPage() {
  await connection();
  return (
    <main>
      <SessionRedirect />
    </main>
  );
}
