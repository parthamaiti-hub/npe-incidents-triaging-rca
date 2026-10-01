import { expect, test } from "./fixtures";

// Happy path: playbook build -> approve. Uses a
// timestamp-unique category so this always targets a genuinely new
// (source_system, category) pair regardless of what earlier test/manual
// runs left behind -- no reliance on a clean database.
test("build wizard creates and approves a new workflow version", async ({ page }) => {
  const category = `E2E TEST CATEGORY ${Date.now()}`;

  await page.goto("/playbooks/build");
  await page.selectOption("select", { label: "Data Contract Dashboard" });
  await page.fill('input[placeholder="e.g. FUNCTIONAL DEFECT (QA/UAT)"]', category);

  await page.locator("select").nth(1).selectOption("error_logs");
  await page.fill('input[name="env"]', "NPE");
  await page.fill('input[name="app"]', "E2ETEST");
  await page.fill('input[name="lookback_minutes"]', "30");
  await page.click('button:has-text("Add to workflow")');

  await page.click('button:has-text("Build workflow")');
  await expect(page.getByText("Preview")).toBeVisible();

  await page.click('button:has-text("Approve workflow")');
  await expect(page.getByText(/Approved as version 1\./)).toBeVisible({ timeout: 15000 });

  // The new version must show up immediately in the playbooks list --
  // no reload, no manual refresh. <option> text isn't "visible" per
  // Playwright's definition even when its <select> is rendered, so assert
  // on the option's existence/selected state rather than visibility.
  await page.getByRole("link", { name: "View in Playbooks" }).click();
  await expect(page).toHaveURL(/\/playbooks$/);
  // Scoped to the list: the category also appears as an option in the category filter.
  await page.getByRole("list", { name: "Playbooks" }).getByText(category).click();
  const versionSelect = page.getByLabel("Version");
  await expect(versionSelect).toBeVisible();
  await expect(versionSelect.locator("option", { hasText: "v1 (approved)" })).toHaveCount(1);
});
