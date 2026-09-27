import { test as base } from "@playwright/test";

// Pre-seeds the operator identity before every test
// navigates, so the blocking "who's operating this session?" modal never
// gets in the way of the actual flow under test.
export const test = base.extend({
  // Playwright's fixture callback parameter is conventionally named `use`,
  // which collides with React 19's `use()` hook and trips
  // react-hooks/rules-of-hooks -- renamed here, purely cosmetic, Playwright
  // doesn't care about the name since fixtures are positional.
  page: async ({ page }, runTest) => {
    await page.addInitScript(() => {
      window.localStorage.setItem("npe-operator-identity", "e2e-test");
    });
    await runTest(page);
  },
});

export { expect } from "@playwright/test";
