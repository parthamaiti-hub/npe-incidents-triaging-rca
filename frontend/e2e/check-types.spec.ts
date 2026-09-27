import { expect, test } from "./fixtures";

// Check Type Registry tab -- browse a known check_type, confirm
// its declared parameters and real Python source render, then switch to
// its v1 (a synthesized template string, not a real file -- see
// app/routers/functions.py::get_function_source) and confirm that shows
// instead of stale v2 source. Requires seed_functional_dummy_versions to
// have run (scripts/start.ps1 does this) so v2 is a real Python module,
// not just a v1-only registry.
test("browse a check type's parameters and source, and switch versions", async ({ page }) => {
  await page.goto("/check-types");

  // "error_logs" is a substring of "pipeline_error_logs" too, so both the
  // search filter and any text-based row locator match both -- found by
  // actually running this against the live stack. Click the exact code
  // element, not a substring-matched row.
  await page.getByPlaceholder("Search check types…").fill("error_logs");
  await page.getByText("error_logs", { exact: true }).click();

  await expect(page.getByRole("heading", { name: "error_logs" })).toBeVisible();

  // Parameters render as a read-only contract table (name/type/required),
  // not bound values.
  // By role: the page subtitle also contains "parameters", so a text match
  // would hit two elements once the detail has loaded.
  await expect(page.getByRole("heading", { name: "Parameters" })).toBeVisible();
  const paramsTable = page.locator("table").first();
  await expect(paramsTable.getByText("env")).toBeVisible();

  // Output shape is the static, honest note -- same for every check type.
  await expect(page.getByText(/"status": "OK" \| "WARN" \| "ERROR"/)).toBeVisible();

  // v2 is active by default (seed_functional_dummy_versions supersedes v1)
  // and is a real Python module -- source renders via syntax highlighting.
  const versionSelect = page.getByLabel("Version");
  await expect(versionSelect).toBeVisible();
  await expect(versionSelect).toHaveValue("2");
  await expect(page.getByText("async def run(params: dict)")).toBeVisible();

  // v1 is a synthesized template string, not a file read -- switching to
  // it must replace the v2 source, not leave it stale underneath.
  await versionSelect.selectOption({ label: "v1" });
  await expect(page.getByText(/status='WARN', details_template=/)).toBeVisible();
  await expect(page.getByText("async def run(params: dict)")).toHaveCount(0);
});
