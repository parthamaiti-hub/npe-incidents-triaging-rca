"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { OperatorIdentityChip } from "@/components/OperatorIdentityChip";
import { StatSticker } from "@/components/StatSticker";

const TABS = [
  { href: "/", label: "RCA of Incidents" },
  { href: "/playbooks", label: "Playbooks for RCA" },
  { href: "/retry", label: "Retry RCA" },
  { href: "/system-mapping", label: "System Mapping Data" },
  { href: "/check-types", label: "Check Type Registry" },
];

export function TopNav() {
  const pathname = usePathname();

  return (
    <header className="flex items-center justify-between gap-6 border-b border-grid bg-white px-6 py-3">
      <nav className="flex gap-6">
        {TABS.map((tab) => {
          const active = tab.href === "/" ? pathname === "/" : pathname.startsWith(tab.href);
          return (
            <Link
              key={tab.href}
              href={tab.href}
              className={
                active
                  ? "border-b-2 border-focus pb-1 font-semibold text-heading"
                  : "pb-1 text-muted hover:text-heading"
              }
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
      <div className="flex items-center gap-4">
        <StatSticker />
        <OperatorIdentityChip />
      </div>
    </header>
  );
}
