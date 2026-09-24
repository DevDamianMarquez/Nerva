import type { ReactNode } from "react";

import { MobileNav } from "@/components/mobile-nav";
import { SidebarContent } from "@/components/sidebar-content";

/**
 * The application frame: a fixed sidebar on large screens, a top bar with a drawer on
 * small ones, and a single scrolling content region. `dvh` keeps the frame correct
 * while mobile browser toolbars collapse and expand.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-dvh overflow-hidden">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-2 focus:text-primary-foreground"
      >
        Skip to content
      </a>
      <aside className="hidden w-60 shrink-0 border-r border-sidebar-border lg:block">
        <SidebarContent />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <MobileNav />
        <main
          id="main"
          className="min-h-0 flex-1 [scrollbar-gutter:stable] overflow-y-auto overscroll-contain"
        >
          {children}
        </main>
      </div>
    </div>
  );
}
