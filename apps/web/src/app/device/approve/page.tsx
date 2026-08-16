import { connection } from "next/server";

import { DeviceApproval } from "../../../features/devices/DeviceApproval";

export const metadata = {
  title: "Approve device",
};

export default async function DeviceApprovalPage() {
  await connection();
  return (
    <main>
      <DeviceApproval />
    </main>
  );
}
