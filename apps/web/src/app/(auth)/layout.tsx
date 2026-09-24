import type { ReactNode } from "react";

import { Logo } from "@/components/logo";

/** Shared chrome for the signed-out pages: a logo and a centered card, nothing else. */
export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col bg-background">
      <header className="px-4 py-6 sm:px-8">
        <Logo />
      </header>
      <main className="flex flex-1 items-center justify-center px-4 pb-16">
        <div className="w-full max-w-sm">{children}</div>
      </main>
    </div>
  );
}
