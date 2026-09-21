import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, Inter } from "next/font/google";
import "./globals.css";

// Font pairing locked at M0 (Design.md §4 and §7):
// IBM Plex Sans for headings — clean, slightly technical, appropriate to a
// security product; Inter for body, consistent with prior projects.
const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  display: "swap",
});

const plexSans = IBM_Plex_Sans({
  variable: "--font-plex",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
  display: "swap",
});

/**
 * Rendered per request, so `proxy.ts` can mint a CSP nonce for each document (M8). A page
 * generated at build time has no request to take a nonce from, and its inline hydration
 * script would then be refused by the policy.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Secure File Sharing Platform",
  description:
    "Secure file storage and sharing for teams who care where their files go.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${plexSans.variable} ${plexMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
