import { connection } from "next/server";

import { DeviceList } from "../../../features/devices/DeviceList";

export const metadata = {
  title: "Devices",
};

export default async function AdminDevicesPage() {
  await connection();
  return (
    <main>
      <h1>Devices</h1>
      <DeviceList />
    </main>
  );
}
