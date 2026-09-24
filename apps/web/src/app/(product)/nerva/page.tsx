import type { Metadata } from "next";

import { NervaChat } from "@/components/nerva-chat";
import { PageHeader } from "@/components/page-header";
import { suggestedPrompts } from "@/lib/sample-data";

// `absolute` skips the "· Nerva" suffix, which would read "Nerva · Nerva".
export const metadata: Metadata = { title: { absolute: "Nerva" } };

export default function NervaPage() {
  return (
    <div className="mx-auto flex h-full w-full max-w-3xl flex-col px-4 pt-6 sm:px-8 sm:pt-10">
      <div className="pb-6">
        <PageHeader
          title="Nerva"
          description="Ask about your pipeline, your conversations, or anything waiting on your approval."
        />
      </div>
      <NervaChat suggestions={suggestedPrompts} />
    </div>
  );
}
