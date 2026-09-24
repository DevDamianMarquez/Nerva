"use client";

import { LogOut } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { Logo } from "@/components/logo";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth-context";
import { isActivePath, primaryNav, settingsNav, type NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const letters =
    parts.length >= 2 ? [parts[0]?.[0], parts[parts.length - 1]?.[0]] : [name.slice(0, 2)];
  return letters.join("").toUpperCase() || "?";
}

function roleLabel(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1);
}

interface SidebarContentProps {
  /** Called after a navigation link is followed (used to close the mobile drawer). */
  onNavigate?: () => void;
}

function NavLink({
  item,
  active,
  onNavigate,
}: {
  item: NavItem;
  active: boolean;
  onNavigate?: () => void;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      className={cn(
        "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm font-medium transition-colors",
        active
          ? "bg-sidebar-accent text-sidebar-accent-foreground"
          : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground",
      )}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      {item.label}
    </Link>
  );
}

export function SidebarContent({ onNavigate }: SidebarContentProps) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, workspace, logout } = useAuth();

  const onSignOut = async () => {
    await logout();
    onNavigate?.();
    router.replace("/login");
  };

  return (
    <div className="flex h-full flex-col bg-sidebar text-sidebar-foreground">
      <div className="flex h-14 shrink-0 items-center px-4">
        <Link
          href="/today"
          onClick={onNavigate}
          aria-label="Nerva, go to Today"
          className="rounded-md"
        >
          <Logo />
        </Link>
      </div>

      <nav aria-label="Primary" className="flex-1 overflow-y-auto px-2 py-2">
        <ul className="space-y-0.5">
          {primaryNav.map((item) => (
            <li key={item.href}>
              <NavLink
                item={item}
                active={isActivePath(pathname, item.href)}
                onNavigate={onNavigate}
              />
            </li>
          ))}
        </ul>
      </nav>

      <div className="shrink-0 border-t border-sidebar-border p-2">
        <NavLink
          item={settingsNav}
          active={isActivePath(pathname, settingsNav.href)}
          onNavigate={onNavigate}
        />
        <div className="mt-1 flex items-center gap-2.5 px-2.5 py-2">
          <Avatar className="size-7">
            <AvatarFallback className="text-[0.65rem] font-semibold">
              {workspace ? initials(workspace.name) : "…"}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1 leading-tight">
            <p className="truncate text-sm font-medium">{workspace?.name ?? "Loading…"}</p>
            <p className="truncate text-xs text-muted-foreground">
              {user?.name}
              {workspace ? ` · ${roleLabel(workspace.role)}` : null}
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Sign out"
            onClick={() => void onSignOut()}
          >
            <LogOut aria-hidden="true" />
          </Button>
        </div>
      </div>
    </div>
  );
}
