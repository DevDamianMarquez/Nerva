import { describe, expect, it } from "vitest";

import { isActivePath, primaryNav, settingsNav } from "@/lib/nav";

describe("isActivePath", () => {
  it("matches the exact path", () => {
    expect(isActivePath("/inbox", "/inbox")).toBe(true);
  });

  it("matches nested paths under a section", () => {
    expect(isActivePath("/opportunities/123", "/opportunities")).toBe(true);
  });

  it("does not match a different section that merely shares a prefix", () => {
    expect(isActivePath("/nerva-labs", "/nerva")).toBe(false);
    expect(isActivePath("/today", "/inbox")).toBe(false);
  });
});

describe("navigation", () => {
  it("lists the primary sections in order, with Settings kept separate", () => {
    expect(primaryNav.map((item) => item.label)).toEqual([
      "Today",
      "Inbox",
      "Opportunities",
      "Nerva",
      "Activity",
    ]);
    expect(settingsNav.href).toBe("/settings");
  });

  it("points every item at a distinct route", () => {
    const hrefs = [...primaryNav, settingsNav].map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });
});
