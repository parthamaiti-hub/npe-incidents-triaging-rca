"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { CLASSIFICATION_STATUS_OPTIONS, classificationStatusLabel } from "@/lib/classification";
import { useCategoryOptions, useSourceSystems } from "@/lib/queries/catalog";
import { humanizeRcaStatus, RCA_STATUS_OPTIONS } from "@/lib/rca";

/**
 * Filters drive the URL query string directly (not local state) so a
 * filtered view is bookmarkable/shareable and survives back/forward
 * navigation -- read back by the dashboard page via its own
 * `useSearchParams()`.
 */
export function IncidentFilters() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { data: sourceSystems } = useSourceSystems();
  const { data: categories } = useCategoryOptions();

  function setParam(key: string, value: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (value) params.set(key, value);
    else params.delete(key);
    params.delete("offset"); // any filter change restarts pagination
    router.push(`${pathname}?${params.toString()}`);
  }

  return (
    <div className="mt-3 flex flex-wrap items-end gap-3">
      <label className="text-xs text-muted">
        Search
        <input
          defaultValue={searchParams.get("q") ?? ""}
          onBlur={(e) => setParam("q", e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && setParam("q", e.currentTarget.value)}
          placeholder="jira key or subject"
          className="mt-1 block w-48 rounded border border-grid px-2 py-1 text-sm text-heading"
        />
      </label>

      <label className="text-xs text-muted">
        Classification
        <select
          defaultValue={searchParams.get("classification_status") ?? ""}
          onChange={(e) => setParam("classification_status", e.target.value)}
          className="mt-1 block rounded border border-grid px-2 py-1 text-sm text-heading"
        >
          <option value="">Any</option>
          {CLASSIFICATION_STATUS_OPTIONS.map((status) => (
            <option key={status} value={status}>
              {classificationStatusLabel(status)}
            </option>
          ))}
        </select>
      </label>

      <label className="text-xs text-muted">
        RCA Outcome
        <select
          defaultValue={searchParams.get("rca_status") ?? ""}
          onChange={(e) => setParam("rca_status", e.target.value)}
          className="mt-1 block rounded border border-grid px-2 py-1 text-sm text-heading"
        >
          <option value="">Any</option>
          {RCA_STATUS_OPTIONS.map((status) => (
            <option key={status} value={status}>
              {humanizeRcaStatus(status)}
            </option>
          ))}
        </select>
      </label>

      <label className="text-xs text-muted">
        Source System
        <select
          defaultValue={searchParams.get("source_system_id") ?? ""}
          onChange={(e) => setParam("source_system_id", e.target.value)}
          className="mt-1 block rounded border border-grid px-2 py-1 text-sm text-heading"
        >
          <option value="">Any</option>
          {sourceSystems?.map((system) => (
            <option key={system.id} value={system.id}>
              {system.name}
            </option>
          ))}
        </select>
      </label>

      <label className="text-xs text-muted">
        Category
        <select
          defaultValue={searchParams.get("category") ?? ""}
          onChange={(e) => setParam("category", e.target.value)}
          className="mt-1 block rounded border border-grid px-2 py-1 text-sm text-heading"
        >
          <option value="">Any</option>
          {categories?.map((category) => (
            <option key={category} value={category}>
              {category}
            </option>
          ))}
        </select>
      </label>

      <label className="text-xs text-muted">
        From
        <input
          type="date"
          defaultValue={searchParams.get("date_from")?.slice(0, 10) ?? ""}
          onChange={(e) => setParam("date_from", e.target.value)}
          className="mt-1 block rounded border border-grid px-2 py-1 text-sm text-heading"
        />
      </label>

      <label className="text-xs text-muted">
        To
        <input
          type="date"
          defaultValue={searchParams.get("date_to")?.slice(0, 10) ?? ""}
          onChange={(e) => setParam("date_to", e.target.value)}
          className="mt-1 block rounded border border-grid px-2 py-1 text-sm text-heading"
        />
      </label>
    </div>
  );
}
