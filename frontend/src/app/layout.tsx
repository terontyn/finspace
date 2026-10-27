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
        {process.env.NEXT_PUBLIC_DEMO_MODE === "true" && <div role="status" style={{position:"fixed",bottom:0,left:0,right:0,zIndex:99999,background:"#fff0b5",color:"#30270d",padding:"8px",textAlign:"center",fontSize:"13px"}}>ДЕМО · Все финансовые данные вымышлены · API adapter только для просмотра · PostgreSQL и интеграции не подключены</div>}
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
