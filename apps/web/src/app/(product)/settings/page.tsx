import type { Metadata } from "next";
import { Suspense } from "react";

import { SettingsView } from "./settings-view";

export const metadata: Metadata = { title: "Settings" };

export default function SettingsPage() {
  // SettingsView reads `?integration=` (set by the Gmail OAuth callback redirect) via
  // useSearchParams, which Next.js requires a Suspense boundary for.
  return (
    <Suspense>
      <SettingsView />
    </Suspense>
  );
}
