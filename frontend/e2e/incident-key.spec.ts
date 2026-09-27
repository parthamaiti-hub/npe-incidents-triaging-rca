import { expect, test } from "./fixtures";

// An incident pushed by webhook without a Jira key gets a generated
// int_MMDDYYYYHHMMSS_NNNNN ID, is classified and gets its first RCA
// automatically (the running worker), and can be reprocessed from the Retry
// tab by that same ID. Uses a timestamp-unique subject so the idempotency
// check never treats it as a duplicate of an earlier run.
test("webhook incident without a Jira key gets an ID, an automatic RCA, and can be retried", async ({ page, request }) => {
  const stamp = Date.now();
  const posted = await request.post("/api/backend/webhooks/teams", {
    data: {
      id: `e2e-msg-${stamp}`,
      text: `**Subject:** E2E incident-key check ${stamp}\n**Environment:** NPE\n**Impact:** nothing matches a known system.\n`,
    },
  });
  expect(posted.ok(), await posted.text()).toBeTruthy();
  const { incident_id: incidentId } = await posted.json();

  // Wait for the worker: classification, then the automatic RCA.
  let incidentKey = "";
  await expect
    .poll(
      async () => {
        const incident = await (await request.get(`/api/backend/incidents/${incidentId}`)).json();
        incidentKey = incident.incident_key;
        const runs = await (await request.get(`/api/backend/workflows/executions?incident_key=${incidentKey}`)).json();
        return runs.map((r: { triggered_by: string }) => r.triggered_by);
      },
      { timeout: 30_000 },
    )
    .toEqual(["auto"]);
  expect(incidentKey).toMatch(/^int_\d{14}_\d{5}$/);

  // The dashboard lists it under its generated ID, with the RCA outcome.
  await page.goto(`/?q=${incidentKey}`);
  await page.getByRole("link", { name: incidentKey, exact: true }).click();
  await expect(page.getByRole("heading", { name: incidentKey })).toBeVisible();

  // Retry by the same ID: re-classifies and records a second attempt.
  await page.goto(`/retry?q=${incidentKey}`);
  await page.getByText(incidentKey, { exact: true }).click();
  await page.getByRole("button", { name: "Execute" }).click();
  await expect(page.getByText(/triggered_by=retry/)).toBeVisible();

  const runs = await (await request.get(`/api/backend/workflows/executions?incident_key=${incidentKey}`)).json();
  expect(runs.map((r: { triggered_by: string }) => r.triggered_by)).toEqual(["retry", "auto"]);
});
