import { connection } from "next/server";

import { LoginForm } from "../../features/authentication/LoginForm";

export const metadata = {
  title: "Sign in",
};

export default async function LoginPage() {
  await connection();
  return (
    <main>
      <LoginForm />
    </main>
  );
}
