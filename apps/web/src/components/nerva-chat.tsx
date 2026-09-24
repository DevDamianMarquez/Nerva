"use client";

import { ArrowUp, Info } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { LogoMark } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

interface ChatMessage {
  id: number;
  role: "user" | "notice";
  text: string;
}

const NOT_CONNECTED_NOTICE =
  "Nerva can’t answer yet. The assistant is connected in a later release, and this message has not been sent anywhere.";

interface NervaChatProps {
  suggestions: readonly string[];
}

/**
 * The workspace chat surface. It is visual only in v0.1: there is no model behind it,
 * messages never leave the browser, and the interface says so instead of pretending.
 */
export function NervaChat({ suggestions }: NervaChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const nextId = useRef(1);
  const endRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    // Keep the newest message in view; nothing to do before the first message is sent.
    if (messages.length > 0) endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const send = () => {
    const text = draft.trim();
    if (!text) return;
    const userId = nextId.current++;
    const noticeId = nextId.current++;
    setMessages((current) => [
      ...current,
      { id: userId, role: "user", text },
      { id: noticeId, role: "notice", text: NOT_CONNECTED_NOTICE },
    ]);
    setDraft("");
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    send();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        role="log"
        aria-label="Conversation with Nerva"
        aria-live="polite"
        className="min-h-0 flex-1 space-y-6 overflow-y-auto overscroll-contain py-2"
      >
        <div className="flex gap-3">
          <LogoMark className="mt-0.5 size-6" />
          <div className="min-w-0 space-y-4">
            <div className="space-y-2">
              <p className="font-medium">Nerva</p>
              <p>
                I can tell you which conversations need a reply, spot new leads, and draft
                follow-ups for you to approve. This workspace is a preview for now, so I can&rsquo;t
                answer yet.
              </p>
              <p className="text-muted-foreground">
                Here is what you will be able to ask once your inbox is connected:
              </p>
            </div>
            <ul className="flex flex-wrap gap-2">
              {suggestions.map((suggestion) => (
                <li key={suggestion}>
                  <button
                    type="button"
                    onClick={() => {
                      setDraft(suggestion);
                      // Hand focus to the composer so Enter sends, and typing edits the prompt.
                      composerRef.current?.focus();
                    }}
                    className="rounded-full border px-3 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                  >
                    {suggestion}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {messages.map((message) =>
          message.role === "user" ? (
            <div key={message.id} className="flex justify-end">
              <p className="max-w-[85%] rounded-2xl bg-secondary px-3.5 py-2 whitespace-pre-wrap">
                {message.text}
              </p>
            </div>
          ) : (
            <p key={message.id} className="flex items-start gap-2 text-xs text-muted-foreground">
              <Info className="mt-px size-3.5 shrink-0" aria-hidden="true" />
              {message.text}
            </p>
          ),
        )}
        <div ref={endRef} />
      </div>

      <form onSubmit={onSubmit} className="shrink-0 pt-4 pb-6">
        <div className="rounded-xl border bg-background p-2 shadow-xs focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/30">
          <label htmlFor="nerva-composer" className="sr-only">
            Message Nerva
          </label>
          <Textarea
            id="nerva-composer"
            ref={composerRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Ask about your pipeline, conversations or pending work…"
            rows={1}
            className="max-h-40 min-h-10 resize-none border-0 bg-transparent px-2 py-1.5 shadow-none focus-visible:ring-0 dark:bg-transparent"
          />
          <div className="flex items-center justify-between gap-3 pt-1 pl-2">
            <p className="text-xs text-muted-foreground">
              Preview only. Messages stay in this tab.
            </p>
            <Button type="submit" size="icon" aria-label="Send message" disabled={!draft.trim()}>
              <ArrowUp aria-hidden="true" />
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}
