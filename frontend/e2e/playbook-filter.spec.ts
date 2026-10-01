import { expect, test } from "./fixtures";

// Playbooks for RCA tab -- the name search and category dropdown filter the
// definition list client-side. Read-only: expectations are derived from the
// live definitions list at test time, so no seeding and no reliance on
// specific rows existing.

interface Definition {
  id: string;
  source_system_id: string;
  category: string;
}

test("filter playbooks by name and category", async ({ page, request }) => {
  const response = await request.get("/api/backend/workflows/definitions");
  expect(response.ok()).toBeTruthy();
  const definitions = (await response.json()) as Definition[];
  test.skip(definitions.length === 0, "no playbooks defined");

  await page.goto("/playbooks");
  const list = page.getByRole("list", { name: "Playbooks" });
  const items = list.getByRole("listitem");
  await expect(items).toHaveCount(definitions.length);

  // Category dropdown: only that category's playbooks remain.
  const category = definitions[0].category;
  await page.getByLabel("Filter by category").selectOption(category);
  await expect(items).toHaveCount(definitions.filter((d) => d.category === category).length);
  for (const text of await items.allInnerTexts()) expect(text).toContain(category);

  // Name search combines with the category filter: the definition id is unique.
  await page.getByLabel("Search playbooks by name").fill(definitions[0].id.toLowerCase());
  await expect(items).toHaveCount(1);

  // Clearing the category keeps the name match.
  await page.getByLabel("Filter by category").selectOption("");
  await expect(items).toHaveCount(1);

  // Search by source system id matches all of that system's playbooks.
  const systemId = definitions[0].source_system_id;
  await page.getByLabel("Search playbooks by name").fill(systemId);
  await expect(items).toHaveCount(
    definitions.filter((d) => d.source_system_id.toLowerCase().includes(systemId.toLowerCase()) || d.id.toLowerCase().includes(systemId.toLowerCase())).length,
  );

  // No match -> empty-state message.
  await page.getByLabel("Search playbooks by name").fill("zz-no-such-playbook-zz");
  await expect(items).toHaveCount(0);
  await expect(page.getByText("No playbooks match the filters.")).toBeVisible();
});
