"use client";

import { Plus, Target } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState } from "@/components/empty-state";
import { LoadError } from "@/components/load-error";
import { PageContainer, PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, ApiError } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type { Opportunity, OpportunityStage } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

const stages: readonly OpportunityStage[] = ["new", "qualified", "proposal", "won", "lost"];
const stageLabels: Record<OpportunityStage, string> = {
  new: "New",
  qualified: "Qualified",
  proposal: "Proposal",
  won: "Won",
  lost: "Lost",
};
const columns = ["Opportunity", "Stage", "Value", "Created"] as const;

function formatValue(cents: number | null, currency: string): string {
  if (cents === null) return "—";
  // A fixed locale keeps this deterministic regardless of the visitor's OS/browser locale.
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(cents / 100);
}

export function OpportunitiesView() {
  const { workspace } = useAuth();
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    if (!workspace) return;
    setState("loading");
    try {
      const list = await api.get<Opportunity[]>(`/v1/workspaces/${workspace.id}/opportunities`);
      setOpportunities(list);
      setState("ready");
    } catch {
      setState("error");
    }
  }, [workspace]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  if (!workspace) return null;
  const workspaceId = workspace.id;

  const onCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCreateError(null);
    setCreating(true);
    try {
      await api.post<Opportunity>(`/v1/workspaces/${workspaceId}/opportunities`, {
        title: title.trim(),
      });
      setTitle("");
      setOpen(false);
      await load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Something went wrong.");
    } finally {
      setCreating(false);
    }
  };

  const counts = Object.fromEntries(
    stages.map((stage) => [stage, opportunities.filter((o) => o.stage === stage).length]),
  ) as Record<OpportunityStage, number>;

  return (
    <PageContainer width="wide">
      <PageHeader
        title="Opportunities"
        description="Leads and deals Nerva has identified or you have added, tracked by stage."
        actions={
          <Sheet open={open} onOpenChange={setOpen}>
            <SheetTrigger asChild>
              <Button>
                <Plus data-icon="inline-start" aria-hidden="true" />
                New opportunity
              </Button>
            </SheetTrigger>
            <SheetContent>
              <SheetHeader>
                <SheetTitle>New opportunity</SheetTitle>
                <SheetDescription>Give it a title. You can add more detail later.</SheetDescription>
              </SheetHeader>
              <form id="new-opportunity" onSubmit={onCreate} className="space-y-4 px-4">
                <div className="space-y-1.5">
                  <label htmlFor="opportunity-title" className="text-sm font-medium">
                    Title
                  </label>
                  <Input
                    id="opportunity-title"
                    required
                    maxLength={200}
                    value={title}
                    onChange={(event) => setTitle(event.target.value)}
                    disabled={creating}
                    autoFocus
                  />
                </div>
                {createError ? (
                  <p role="alert" className="text-sm text-destructive">
                    {createError}
                  </p>
                ) : null}
              </form>
              <SheetFooter>
                <Button type="submit" form="new-opportunity" disabled={creating || !title.trim()}>
                  {creating ? "Creating…" : "Create opportunity"}
                </Button>
              </SheetFooter>
            </SheetContent>
          </Sheet>
        }
      />

      <dl className="flex flex-wrap gap-x-8 gap-y-3 border-b pb-5">
        {stages.map((stage) => (
          <div key={stage} className="min-w-16">
            <dt className="text-xs text-muted-foreground">{stageLabels[stage]}</dt>
            <dd className="text-lg font-semibold tabular-nums">{counts[stage]}</dd>
          </div>
        ))}
      </dl>

      {state === "error" ? (
        <LoadError onRetry={() => void load()} />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                {columns.map((column) => (
                  <TableHead key={column} className="h-9 text-xs font-medium text-muted-foreground">
                    {column}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {state === "loading" ? (
                <TableRow className="hover:bg-transparent">
                  <TableCell
                    colSpan={columns.length}
                    className="py-14 text-center text-muted-foreground"
                  >
                    Loading…
                  </TableCell>
                </TableRow>
              ) : opportunities.length === 0 ? (
                <TableRow className="hover:bg-transparent">
                  <TableCell colSpan={columns.length} className="p-0">
                    <EmptyState
                      icon={Target}
                      title="No opportunities yet"
                      description="When Nerva spots a lead in your conversations, it appears here for you to review. Connect your inbox and it will start looking, or add one yourself."
                    >
                      <Button variant="outline" asChild>
                        <Link href="/inbox">Go to Inbox</Link>
                      </Button>
                    </EmptyState>
                  </TableCell>
                </TableRow>
              ) : (
                opportunities.map((opportunity) => (
                  <TableRow key={opportunity.id}>
                    <TableCell className="font-medium">{opportunity.title}</TableCell>
                    <TableCell>{stageLabels[opportunity.stage]}</TableCell>
                    <TableCell>
                      {formatValue(opportunity.estimated_value_cents, opportunity.currency)}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {new Date(opportunity.created_at).toLocaleDateString()}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </PageContainer>
  );
}
