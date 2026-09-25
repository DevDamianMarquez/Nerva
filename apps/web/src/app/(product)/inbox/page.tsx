import type { Metadata } from "next";
import { Suspense } from "react";

import { InboxView } from "./inbox-view";

export const metadata: Metadata = { title: "Inbox" };

export default function InboxPage() {
  // InboxView reads `?conversation=` (set by Today's "Needs your attention" links)
  // via useSearchParams, which Next.js requires a Suspense boundary for.
  return (
    <Suspense>
      <InboxView />
    </Suspense>
  );
}
