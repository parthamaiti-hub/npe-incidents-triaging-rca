import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

import { Providers } from "./providers";
import { HealthBanner } from "@/components/HealthBanner";
import { OperatorIdentityProvider } from "@/components/OperatorIdentityProvider";
import { TopNav } from "@/components/TopNav";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "NPE Incident Triage",
  description: "Automated RCA triage for non-production environment incidents",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col bg-canvas text-heading">
        <Providers>
          <OperatorIdentityProvider>
            <HealthBanner />
            <TopNav />
            <div className="flex-1">{children}</div>
          </OperatorIdentityProvider>
        </Providers>
      </body>
    </html>
  );
}
