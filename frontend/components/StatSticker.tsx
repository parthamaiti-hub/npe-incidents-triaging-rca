"use client";

import { useIncidentStats } from "@/lib/queries/stats";

const CHIPS: { key: "total" | "succeeded" | "failed" | "unidentified"; label: string; dot: string }[] = [
  { key: "total", label: "Total", dot: "bg-heading" },
  { key: "succeeded", label: "Success", dot: "bg-focus" },
  { key: "failed", label: "Failed", dot: "bg-danger" },
  { key: "unidentified", label: "Unidentified", dot: "bg-highlight" },
];

function formatAvg(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.round(seconds / 60)}m`;
}

// A single line of stats, sticker-style on the right of the top nav. A
// colored dot stands in for an icon here rather than an emoji.
export function StatSticker() {
  const { data, isError } = useIncidentStats("7d");

  if (isError) {
    return <span className="text-sm text-danger">stats unavailable</span>;
  }

  return (
    <div className="flex items-center gap-4 text-sm">
      {CHIPS.map(({ key, label, dot }) => (
        <span key={key} className="flex items-center gap-1.5">
          <span className={`h-2 w-2 rounded-full ${dot}`} aria-hidden />
          <span className="text-muted">{label}</span>
          <span className="font-semibold text-heading">{data ? data[key] : "—"}</span>
        </span>
      ))}
      <span className="flex items-center gap-1.5">
        <span className="h-2 w-2 rounded-full bg-muted" aria-hidden />
        <span className="text-muted">Avg time</span>
        <span className="font-semibold text-heading">{data ? formatAvg(data.avg_process_seconds) : "—"}</span>
      </span>
    </div>
  );
}
