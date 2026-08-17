import { connection } from "next/server";

import { PolicyEditor } from "../../../features/exclusion-policy/PolicyEditor";

export const metadata = {
  title: "Policy",
};

export default async function AdminPolicyPage() {
  await connection();
  return (
    <main>
      <h1>Exclusion policy</h1>
      <PolicyEditor />
    </main>
  );
}
