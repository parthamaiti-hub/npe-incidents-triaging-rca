import { expect, test } from "./fixtures";

// System Mapping Data tab -- create a source system, add a
// footprint, edit it, confirm delete is blocked while the footprint still
// references the system (409 surfaced verbatim), then delete the footprint
// and finally the source system. Timestamp-unique id so this always
// targets a fresh row regardless of what earlier runs left behind.
test("create, edit, and delete a source system and its footprint", async ({ page }) => {
  const id = `E2ETEST_SYS_${Date.now()}`;

  await page.goto("/system-mapping");
  await page.click('button:has-text("Add source system")');

  // getByLabel({ exact: true }) throughout this spec -- Playwright's
  // default substring, case-insensitive text matching means a short label
  // like "Id" also matches inside unrelated text containing "id" (e.g. the
  // footprint form's "Must be a **valid** regular expression" hint sits
  // inside the "Value" label, and "valid" contains "id") -- found by
  // actually running this test against the live stack, not by inspection.
  const createForm = page.locator("form", { hasText: "Add source system" });
  await createForm.getByLabel("Id", { exact: true }).fill(id);
  await createForm.getByLabel("Name", { exact: true }).fill("E2E Test System");
  await createForm.getByLabel("Code", { exact: true }).fill("E2E");
  await createForm.getByLabel("Type", { exact: true }).fill("Application");
  await createForm.getByLabel("Environment", { exact: true }).fill("NPE");
  await createForm.getByLabel("Owning team (display)", { exact: true }).fill("e2e-team");
  await createForm.getByLabel("Description", { exact: true }).fill("created by system-mapping.spec.ts");
  await createForm.getByRole("button", { name: "Save" }).click();

  // The new system appears in the left list and can be selected. Scoped by
  // the timestamp-unique id, not the "E2E Test System" display name --
  // that name repeats across every run, including any leftover rows a
  // previously-failed run didn't clean up, so it isn't unique on its own.
  await page.locator("tr", { hasText: id }).click();
  await expect(page.getByText(`(${id})`)).toBeVisible();

  // Add a footprint -- only one "Add footprint" button exists at this
  // point (no rows yet), so no scoping needed for this step.
  await page.getByRole("button", { name: "Add footprint" }).click();
  const addFootprintForm = page.locator("form", { hasText: "Add footprint" });
  await addFootprintForm.getByLabel("Id", { exact: true }).fill(`${id}_FP1`);
  await addFootprintForm.getByLabel("Footprint type", { exact: true }).fill("hostname");
  await addFootprintForm.getByLabel("Value", { exact: true }).fill("e2e-host");
  await addFootprintForm.getByRole("button", { name: "Save" }).click();

  const footprintRow = page.locator("tr", { hasText: "e2e-host" });
  await expect(footprintRow).toBeVisible();

  // Edit the footprint's value -- scoped to its own row, since the source
  // system's own "Edit source system" button is also on screen by now.
  await footprintRow.getByRole("button", { name: "Edit" }).click();
  const editFootprintForm = page.locator("form", { hasText: `Edit ${id}_FP1` });
  await editFootprintForm.getByLabel("Value", { exact: true }).fill("e2e-host-updated");
  await editFootprintForm.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("e2e-host-updated")).toBeVisible();

  // Deleting the source system while the footprint still references it
  // must be blocked (409), surfaced verbatim.
  await page.getByRole("button", { name: "Delete source system" }).click();
  await page.getByRole("button", { name: "Yes" }).click();
  await expect(page.getByText(new RegExp(`Cannot delete SourceSystem '${id}'`))).toBeVisible();

  // Delete the footprint (row-scoped), then the source system succeeds.
  const updatedFootprintRow = page.locator("tr", { hasText: "e2e-host-updated" });
  await updatedFootprintRow.getByRole("button", { name: "Delete" }).click();
  await updatedFootprintRow.getByRole("button", { name: "Yes" }).click();
  await expect(page.getByText("No footprints configured for this system yet.")).toBeVisible();

  await page.getByRole("button", { name: "Delete source system" }).click();
  await page.getByRole("button", { name: "Yes" }).click();
  await expect(page.getByText("Select a source system on the left, or add a new one.")).toBeVisible();
  await expect(page.locator("tr", { hasText: id })).toHaveCount(0);
});
