import { AuthForm } from "@/components/auth-form";

export const metadata = { title: "Sign in · Secure File Sharing" };

export default function LoginPage() {
  return <AuthForm mode="login" />;
}
