"use client";

interface ParamSpec {
  name: string;
  type: "string" | "int" | "float" | "bool" | "list[string]" | "dict";
  required: boolean;
}

/**
 * Read-only display of a check_type version's *declared*
 * parameter contract (name/type/required) -- distinct from
 * FunctionSourcePanel's params section, which shows a workflow task's
 * *bound* argument values. There's no execution/task context here, so
 * there's nothing to bind values to; this is the contract on its own.
 */
export function CheckTypeParamsView({ params }: { params: ParamSpec[] }) {
  if (params.length === 0) {
    return <p className="mt-1 text-sm text-muted">No parameters.</p>;
  }

  return (
    <table className="mt-1 w-full text-sm">
      <thead>
        <tr className="border-b border-grid text-left text-xs uppercase tracking-wide text-muted">
          <th className="py-1 pr-3">Name</th>
          <th className="py-1 pr-3">Type</th>
          <th className="py-1">Required</th>
        </tr>
      </thead>
      <tbody>
        {params.map((p) => (
          <tr key={p.name} className="border-b border-grid last:border-0">
            <td className="py-1 pr-3">
              <code className="text-heading">{p.name}</code>
            </td>
            <td className="py-1 pr-3 text-muted">{p.type}</td>
            <td className="py-1">
              {p.required ? <span className="text-danger">required</span> : <span className="text-muted">optional</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
