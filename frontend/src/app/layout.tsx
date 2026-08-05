import type { Metadata } from "next";
import { Inter, Unbounded } from "next/font/google";
import type { ReactNode } from "react";

import { AuthProvider } from "@/components/auth-provider";

import "./globals.css";

const inter = Inter({
  display: "swap",
  subsets: ["cyrillic", "latin"],
  variable: "--font-inter",
  weight: "variable",
});

const unbounded = Unbounded({
  display: "swap",
  subsets: ["cyrillic", "latin"],
  variable: "--font-unbounded",
  weight: "variable",
});

export const metadata: Metadata = {
  title: "Финпространство — личные финансы",
  description: "Ваше личное и семейное финансовое пространство",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="ru" className={`${inter.variable} ${unbounded.variable}`}>
      <body>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
