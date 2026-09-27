import { expect, test } from "./fixtures";

const BACKEND_URL = process.env.BACKEND_URL_FOR_TESTS ?? "http://localhost:8421";

// Happy path: dashboard -> incident detail. Seeds via
// the real backend (a live RCA run against a known-real ticket -- this
// exercises the actual Jira fetch path too, not just stored data), then
// drives the UI: load the dashboard, click through, assert the RCA
// sections and executed-workflow graph render.
test("dashboard shows a processed incident and its detail page renders RCA sections", async ({ page, request }) => {
  const seed = await request.post(`${BACKEND_URL}/rca/TT-1`);
  expect(seed.ok()).toBeTruthy();

  await page.goto("/?q=TT-1");
  const row = page.getByRole("link", { name: "TT-1", exact: true });
  await expect(row).toBeVisible();

  await row.click();
  await expect(page).toHaveURL(/\/TT-1$/);
  await expect(page.getByRole("heading", { name: "TT-1" })).toBeVisible();
  await expect(page.getByText(/RCA OUTCOME/i)).toBeVisible();
  await expect(page.locator(".react-flow__node").first()).toBeVisible();
});
