import { connection } from "next/server";

import { LifecycleRejections } from "../../../features/source-lifecycle/LifecycleRejections";

export const metadata = {
  title: "Lifecycle",
};

export default async function AdminLifecyclePage() {
  await connection();
  return (
    <main>
      <h1>Lifecycle</h1>
      <LifecycleRejections />
    </main>
  );
}
