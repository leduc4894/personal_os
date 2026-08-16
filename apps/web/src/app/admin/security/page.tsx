import { connection } from "next/server";

import { SecurityPanel } from "../../../features/authentication/SecurityPanel";

export const metadata = {
  title: "Security",
};

export default async function SecurityPage() {
  await connection();
  return (
    <main>
      <h1>Security</h1>
      <SecurityPanel />
    </main>
  );
}
