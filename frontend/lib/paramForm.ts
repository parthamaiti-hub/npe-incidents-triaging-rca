import { z } from "zod";

export interface ParamSpec {
  name: string;
  type: "string" | "int" | "float" | "bool" | "list[string]" | "dict";
  required: boolean;
}

/** Builds a zod schema for a function's raw HTML-form values (everything
 * comes back as a string or boolean from inputs) -- coercion into the
 * actually-typed value happens separately in `coerceParamValue`. */
export function buildParamsFormSchema(params: ParamSpec[]) {
  const shape: Record<string, z.ZodTypeAny> = {};
  for (const param of params) {
    if (param.type === "bool") {
      shape[param.name] = z.boolean();
      continue;
    }
    let field = z.string();
    if (param.required) field = field.min(1, "required");
    if (param.type === "dict") {
      field = field.refine(
        (value) => value.trim() === "" || isValidJsonObject(value),
        "must be valid JSON",
      );
    }
    shape[param.name] = param.required ? field : field.optional();
  }
  return z.object(shape);
}

function isValidJsonObject(value: string): boolean {
  try {
    const parsed = JSON.parse(value);
    return typeof parsed === "object" && parsed !== null;
  } catch {
    return false;
  }
}

export function defaultFormValues(params: ParamSpec[]): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const param of params) values[param.name] = param.type === "bool" ? false : "";
  return values;
}

/** Turns raw form values (strings/booleans) into the actually-typed `with`
 * params object the backend expects. */
export function coerceParamValues(params: ParamSpec[], raw: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const param of params) {
    const value = raw[param.name];
    if (value === "" || value === undefined) continue; // omit unset optional params
    switch (param.type) {
      case "int":
        result[param.name] = parseInt(String(value), 10);
        break;
      case "float":
        result[param.name] = parseFloat(String(value));
        break;
      case "bool":
        result[param.name] = Boolean(value);
        break;
      case "list[string]":
        result[param.name] = String(value)
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        break;
      case "dict":
        result[param.name] = JSON.parse(String(value));
        break;
      default:
        result[param.name] = value;
    }
  }
  return result;
}
