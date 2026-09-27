"use client";

import { useState } from "react";

import { buildParamsFormSchema, coerceParamValues, defaultFormValues, type ParamSpec } from "@/lib/paramForm";
import type { DraftTask, FunctionSpec, RetryPolicy } from "@/lib/workflowDraft";

interface StepEditorPanelProps {
  functions: FunctionSpec[];
  /** The step being edited, or null to create a new one. */
  initial: DraftTask | null;
  title: string;
  submitLabel: string;
  onSubmit: (task: DraftTask) => void;
  onCancel?: () => void;
}

/** Stored param value -> the raw string/boolean an input holds. */
function toFormValue(param: ParamSpec, value: unknown): unknown {
  if (value === undefined || value === null) return param.type === "bool" ? false : "";
  switch (param.type) {
    case "bool":
      return Boolean(value);
    case "list[string]":
      return Array.isArray(value) ? value.join(", ") : String(value);
    case "dict":
      return JSON.stringify(value);
    default:
      return String(value);
  }
}

function initialValues(params: ParamSpec[], task: DraftTask | null): Record<string, unknown> {
  const values = defaultFormValues(params);
  if (task) for (const param of params) values[param.name] = toFormValue(param, task.with[param.name]);
  return values;
}

const DEFAULT_RETRY: RetryPolicy = { max_attempts: 3, delay_seconds: 2, exponential_backoff: true };

/**
 * Add/edit form for one playbook step: pick a check function, fill its
 * declared params (required ones enforced), optionally override retry.
 * Params the function doesn't declare are kept as-is rather than dropped.
 */
export function StepEditorPanel({ functions, initial, title, submitLabel, onSubmit, onCancel }: StepEditorPanelProps) {
  const [call, setCall] = useState(initial?.call ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const spec = functions.find((f) => f.name === call) ?? null;
  const params = (spec?.params ?? []) as ParamSpec[];
  const [values, setValues] = useState<Record<string, unknown>>(() => initialValues(params, initial));
  const [retryOn, setRetryOn] = useState(!!initial?.retry);
  const [retry, setRetry] = useState<RetryPolicy>(initial?.retry ?? spec?.default_retry ?? DEFAULT_RETRY);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const unknownCall = !!call && !spec;

  function changeCall(next: string) {
    setCall(next);
    setErrors({});
    const nextSpec = functions.find((f) => f.name === next);
    const nextParams = (nextSpec?.params ?? []) as ParamSpec[];
    // Keep what was typed for params the new function also declares.
    const carried = initialValues(nextParams, initial?.call === next ? initial : null);
    for (const p of nextParams) if (values[p.name] !== undefined && values[p.name] !== "") carried[p.name] = values[p.name];
    setValues(carried);
    if (!retryOn && nextSpec) setRetry(nextSpec.default_retry);
  }

  function submit() {
    if (!spec) {
      setErrors({ call: "choose a function" });
      return;
    }
    const parsed = buildParamsFormSchema(params).safeParse(values);
    const nextErrors: Record<string, string> = {};
    if (!parsed.success) {
      for (const issue of parsed.error.issues) nextErrors[String(issue.path[0])] = issue.message;
    }
    for (const p of params) {
      const raw = values[p.name];
      if ((p.type === "int" || p.type === "float") && raw !== "" && raw !== undefined && Number.isNaN(Number(raw))) {
        nextErrors[p.name] = "must be a number";
      }
    }
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    const declared = new Set(params.map((p) => p.name));
    const extras = Object.fromEntries(Object.entries(initial?.call === call ? initial.with : {}).filter(([k]) => !declared.has(k)));
    const task: DraftTask = { call, with: { ...extras, ...coerceParamValues(params, values) } };
    if (name.trim() && name.trim() !== call) task.name = name.trim();
    if (retryOn) task.retry = retry;
    onSubmit(task);
  }

  return (
    <div className="rounded-md border border-grid bg-white p-4">
      <h3 className="text-sm font-semibold text-heading">{title}</h3>

      <label className="mt-3 block text-xs text-muted">
        Function
        <select
          value={call}
          onChange={(e) => changeCall(e.target.value)}
          className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
        >
          <option value="">Select a check function…</option>
          {unknownCall && <option value={call}>{call} (not registered)</option>}
          {functions.map((f) => (
            <option key={f.name} value={f.name} disabled={f.has_implementation === false}>
              {f.name}
              {f.has_implementation === false ? " (no implementation)" : ""}
            </option>
          ))}
        </select>
      </label>
      {errors.call && <p className="mt-1 text-xs text-danger">{errors.call}</p>}
      {unknownCall && <p className="mt-1 text-xs text-danger">&quot;{call}&quot; is not a registered function -- choose another.</p>}
      {spec && <p className="mt-1 text-xs text-muted">{spec.description}</p>}

      <label className="mt-3 block text-xs text-muted">
        Step name (optional)
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={call || "defaults to the function name"}
          className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
        />
      </label>

      {params.length > 0 && (
        <fieldset className="mt-3">
          <legend className="text-xs font-semibold uppercase tracking-wide text-muted">Parameters</legend>
          {params.map((p) => (
            <label key={p.name} className="mt-2 block text-xs text-muted">
              <code className="text-heading">{p.name}</code> <span>({p.type})</span>
              {p.required && <span className="text-danger"> *</span>}
              {p.type === "bool" ? (
                <input
                  type="checkbox"
                  name={p.name}
                  checked={Boolean(values[p.name])}
                  onChange={(e) => setValues({ ...values, [p.name]: e.target.checked })}
                  className="ml-2 align-middle"
                />
              ) : (
                <input
                  name={p.name}
                  value={String(values[p.name] ?? "")}
                  onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}
                  placeholder={p.type === "list[string]" ? "comma-separated" : p.type === "dict" ? '{"key": "value"}' : undefined}
                  className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
                />
              )}
              {errors[p.name] && <span className="mt-0.5 block text-danger">{errors[p.name]}</span>}
            </label>
          ))}
        </fieldset>
      )}

      {spec && (
        <div className="mt-3">
          <label className="text-xs text-muted">
            <input type="checkbox" checked={retryOn} onChange={(e) => setRetryOn(e.target.checked)} className="mr-1 align-middle" />
            Override retry policy
          </label>
          {retryOn && (
            <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-muted">
              <label>
                Max attempts
                <input
                  type="number"
                  min={1}
                  value={retry.max_attempts ?? 3}
                  onChange={(e) => setRetry({ ...retry, max_attempts: Number(e.target.value) })}
                  className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
                />
              </label>
              <label>
                Delay (seconds)
                <input
                  type="number"
                  min={0}
                  value={retry.delay_seconds ?? 2}
                  onChange={(e) => setRetry({ ...retry, delay_seconds: Number(e.target.value) })}
                  className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
                />
              </label>
              <label className="col-span-2">
                <input
                  type="checkbox"
                  checked={retry.exponential_backoff ?? true}
                  onChange={(e) => setRetry({ ...retry, exponential_backoff: e.target.checked })}
                  className="mr-1 align-middle"
                />
                Exponential backoff
              </label>
            </div>
          )}
        </div>
      )}

      <div className="mt-4 flex gap-2">
        <button type="button" onClick={submit} className="rounded bg-focus px-3 py-1.5 text-sm font-medium text-white">
          {submitLabel}
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} className="rounded border border-grid px-3 py-1.5 text-sm text-heading">
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
