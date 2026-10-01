import type { APIRequestContext, Page } from "@playwright/test";

import { expect, test } from "./fixtures";

// Editing an existing playbook: graph edits and YAML edits over one draft,
// saved as a pending edit, then approved as version N+1. Each test seeds its
// own playbook under a timestamp-unique category through the real backend
// (via the app's /api/backend proxy), so no reliance on existing data.

const PARAMS = { env: "NPE", app: "E2ETEST", lookback_minutes: 30 };

interface Seeded {
  category: string;
  definitionId: string;
}

async function seedPlaybook(request: APIRequestContext): Promise<Seeded> {
  const systems = await request.get("/api/backend/catalog/source-systems");
  expect(systems.ok()).toBeTruthy();
  const sourceSystemId = (await systems.json())[0].id as string;
  const category = `E2E EDIT ${Date.now()}`;

  const built = await request.post("/api/backend/workflows/build-requests", {
    data: {
      source_system_id: sourceSystemId,
      category,
      requested_functions: [{ call: "error_logs", with: PARAMS }],
      requested_by: "e2e-test",
    },
  });
  expect(built.status(), await built.text()).toBe(201);
  const approved = await request.post(`/api/backend/workflows/build-requests/${(await built.json()).id}/approve`, {
    data: { approved_by: "e2e-test" },
  });
  expect(approved.ok(), await approved.text()).toBeTruthy();
  return { category, definitionId: (await approved.json()).workflow_definition_id };
}

async function versionCount(request: APIRequestContext, definitionId: string): Promise<number> {
  const response = await request.get(`/api/backend/workflows/definitions/${definitionId}/versions`);
  return (await response.json()).length;
}

async function openEditor(page: Page, category: string) {
  await page.goto("/playbooks");
  // Scoped to the list: the category also appears as an option in the category filter.
  await page.getByRole("list", { name: "Playbooks" }).getByText(category).click();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(yamlPane(page)).toContainText("error_logs");
}

function yamlPane(page: Page) {
  return page.getByRole("region", { name: "Playbook YAML" });
}

/** Replaces the CodeMirror document. insertText avoids auto-indent on newlines. */
async function replaceYaml(page: Page, text: string) {
  await yamlPane(page).locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.insertText(text);
}

test("graph edit: add and reorder a step, save, approve as v2", async ({ page, request }) => {
  const { category } = await seedPlaybook(request);
  await openEditor(page, category);

  await page.getByRole("button", { name: "+ Add step" }).click();
  await page.locator("select", { has: page.locator("option", { hasText: "Select a check function" }) }).selectOption("recent_deployments");
  await page.fill('input[name="env"]', PARAMS.env);
  await page.fill('input[name="app"]', PARAMS.app);
  await page.fill('input[name="lookback_minutes"]', "60");
  await page.getByRole("button", { name: "Add step", exact: true }).click();

  // The graph edit regenerates the YAML pane.
  await expect(yamlPane(page)).toContainText("recent_deployments");
  await expect(page.getByTestId("workflow-step")).toHaveCount(2);

  await page.getByRole("button", { name: "Move recent_deployments up" }).click();
  await expect(page.getByTestId("workflow-step").first()).toContainText("recent_deployments");

  await page.getByLabel("Change note").fill("add deployments check first");
  await page.getByRole("button", { name: "Save as new version" }).click();
  await expect(page.getByText(/Saved as a pending edit of v1/)).toBeVisible();

  await page.getByRole("button", { name: "Approve & publish" }).click();
  const versionSelect = page.getByLabel("Version");
  await expect(versionSelect.locator("option", { hasText: "v2 (approved)" })).toHaveCount(1);
  await expect(versionSelect.locator("option", { hasText: "v1 (superseded)" })).toHaveCount(1);
  await expect(page.getByTestId("workflow-step").first()).toContainText("recent_deployments");
});

test("YAML edit: valid YAML applies to the graph", async ({ page, request }) => {
  const { category } = await seedPlaybook(request);
  await openEditor(page, category);

  await replaceYaml(
    page,
    [
      "do:",
      "  - deploysFirst:",
      "      call: recent_deployments",
      `      with: {env: ${PARAMS.env}, app: ${PARAMS.app}, lookback_minutes: 60}`,
      "  - logs:",
      "      call: error_logs",
      `      with: {env: ${PARAMS.env}, app: ${PARAMS.app}, lookback_minutes: 30}`,
      "",
    ].join("\n"),
  );
  await page.getByRole("button", { name: "Validate & Apply" }).click();

  await expect(page.getByTestId("workflow-step")).toHaveCount(2);
  await expect(page.getByTestId("workflow-step").first()).toContainText("deploysFirst");
  await expect(page.getByTestId("playbook-errors")).toHaveCount(0);
});

test("YAML edit: syntax error is reported and nothing is saved", async ({ page, request }) => {
  const { category, definitionId } = await seedPlaybook(request);
  await openEditor(page, category);

  await replaceYaml(page, "do:\n  - broken:\n      call: [oops\n");
  await page.getByRole("button", { name: "Validate & Apply" }).click();
  await expect(page.getByTestId("playbook-errors")).toContainText("YAML syntax error");
  await expect(page.getByTestId("workflow-step")).toHaveCount(1); // graph unchanged

  await page.getByRole("button", { name: "Save as new version" }).click();
  await expect(page.getByTestId("playbook-errors")).toContainText("nothing was saved");
  expect(await versionCount(request, definitionId)).toBe(1);
});

test("YAML edit: unknown function is reported against its step", async ({ page, request }) => {
  const { category, definitionId } = await seedPlaybook(request);
  await openEditor(page, category);

  await replaceYaml(page, "do:\n  - mystery:\n      call: not_a_real_check\n      with: {}\n");
  await page.getByRole("button", { name: "Validate & Apply" }).click();

  await expect(page.getByTestId("playbook-errors")).toContainText("Unknown function");
  await expect(page.getByTestId("workflow-step").first()).toHaveAttribute("data-error", "true");
  expect(await versionCount(request, definitionId)).toBe(1);
});
