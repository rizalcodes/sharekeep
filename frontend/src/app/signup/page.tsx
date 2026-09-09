import { AuthForm } from "@/components/auth-form";

export const metadata = { title: "Create account · Secure File Sharing" };

export default function SignupPage() {
  return <AuthForm mode="signup" />;
}
