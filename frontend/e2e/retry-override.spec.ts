import { expect, test } from "./fixtures";

const BACKEND_URL = process.env.BACKEND_URL_FOR_TESTS ?? "http://localhost:8420";

// Happy path: retry with an edited mapping. Seeds via
// the real backend rather than assuming leftover manual-testing state:
// (1) an RCA run against TT-1 so it has an ingested Incident + a latest
// execution, (2) a fresh build+approve for (SYS_HSI, FUNCTIONAL DEFECT
// (QA/UAT)) so its workflow always has at least one `superseded` version
// to pick in the override dropdown, regardless of database history.
test("retry with an explicit mapping override records mapping_overridden", async ({ page, request }) => {
  const rcaSeed = await request.post(`${BACKEND_URL}/rca/TT-1`);
  expect(rcaSeed.ok()).toBeTruthy();

  const build = await request.post(`${BACKEND_URL}/workflows/build-requests`, {
    data: {
      source_system_id: "SYS_HSI",
      category: "FUNCTIONAL DEFECT (QA/UAT)",
      requested_functions: [{ call: "error_logs", with: { env: "NPE", app: "HSI", lookback_minutes: 15 } }],
      requested_by: "e2e-seed",
    },
  });
  expect(build.ok()).toBeTruthy();
  const buildBody = await build.json();
  const approve = await request.post(`${BACKEND_URL}/workflows/build-requests/${buildBody.id}/approve`, {
    data: { approved_by: "e2e-seed" },
  });
  expect(approve.ok()).toBeTruthy();

  await page.goto("/retry?q=TT-1");
  await page.getByText("TT-1", { exact: true }).click();
  await expect(page.getByText(/Current mapping:/)).toBeVisible();
  // Current mapping / versions / latest execution are 3 sequential
  // dependent queries -- give them a moment to settle before interacting.
  await page.waitForTimeout(1000);

  await page.click('input[type="checkbox"]');
  const versionSelect = page.locator("select").filter({ hasText: "superseded" });
  await expect(versionSelect).toBeVisible();
  const options = await versionSelect.locator("option").allTextContents();
  const supersededIndex = options.findIndex((o) => o.includes("superseded"));
  expect(supersededIndex).toBeGreaterThanOrEqual(0);
  await versionSelect.selectOption({ index: supersededIndex });

  await page.click('button:has-text("Execute")');
  await expect(page.getByText("Mapping overridden for this run")).toBeVisible({ timeout: 15000 });
});
