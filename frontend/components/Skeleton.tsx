/** Minimal building block for loading-state placeholders -- a pulsing gray bar, composed into table rows / content blocks
 * by the pages that need them. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-grid ${className}`} />;
}

export function SkeletonTableRows({ rows = 5, columns = 7 }: { rows?: number; columns?: number }) {
  return (
    <>
      {Array.from({ length: rows }).map((_, r) => (
        <tr key={r} className="border-b border-grid last:border-0">
          {Array.from({ length: columns }).map((_, c) => (
            <td key={c} className="px-3 py-3">
              <Skeleton className="h-4 w-full max-w-32" />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}
