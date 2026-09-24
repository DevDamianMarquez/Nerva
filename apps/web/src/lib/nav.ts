import {
  Activity,
  CalendarCheck,
  Inbox,
  MessageSquareText,
  Settings,
  Target,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

export const primaryNav: readonly NavItem[] = [
  { href: "/today", label: "Today", icon: CalendarCheck },
  { href: "/inbox", label: "Inbox", icon: Inbox },
  { href: "/opportunities", label: "Opportunities", icon: Target },
  { href: "/nerva", label: "Nerva", icon: MessageSquareText },
  { href: "/activity", label: "Activity", icon: Activity },
];

export const settingsNav: NavItem = {
  href: "/settings",
  label: "Settings",
  icon: Settings,
};

export function isActivePath(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}
