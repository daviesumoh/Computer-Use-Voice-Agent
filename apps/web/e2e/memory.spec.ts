import { createClient } from "@supabase/supabase-js";
import { expect, test, type Page } from "@playwright/test";
import { totp } from "./totp";

const SUPABASE_URL = "http://127.0.0.1:55321";
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY ?? "";
const admin = createClient(SUPABASE_URL, SECRET_KEY, { auth: { persistSession: false } });

const EMAIL = "e2e-memory@example.test";
const PASSWORD = "Correct-Horse-Battery-9";

// One sign-in serves every test; only clearing everything needs a second, fresh code.
test.describe.configure({ mode: "serial", timeout: 120_000 });

let userId = "";
let totpSecret = "";
let page: Page;

async function memory(text: string) {
  const { data } = await admin.from("memories").select("id, status").eq("user_id", userId).eq("canonical_text", text).single();
  return data;
}

async function remember(text: string, extra: Record<string, unknown> = {}) {
  const { data, error } = await admin
    .from("memories")
    .insert({
      user_id: userId, type: "rule", canonical_text: text, origin: "user_stated",
      cue: "explicit_rule", ...extra,
    })
    .select("id")
    .single();
  if (error) throw error;
  return data.id as string;
}

test.beforeAll(async ({ browser }) => {
  const { data: existing } = await admin.auth.admin.listUsers({ perPage: 1000 });
  for (const user of existing.users.filter((u) => u.email === EMAIL)) {
    await admin.auth.admin.deleteUser(user.id);
  }
  const created = await admin.auth.admin.createUser({ email: EMAIL, password: PASSWORD, email_confirm: true });
  if (created.error) throw created.error;
  userId = created.data.user.id;
  await admin.from("profiles").insert({ id: userId });

  const bestBuy = await remember("Always buy electronics from Best Buy.");
  await admin.from("memory_sources").insert({
    memory_id: bestBuy, source_type: "message", source_id: crypto.randomUUID(),
    span: "always use Best Buy for electronics",
  });
  await remember("Prefers window seats on flights.", { type: "preference" });
  await remember("Prefers Costco for tires.", {
    type: "preference", status: "pending_confirm", origin: "inferred", cue: "repeated_choice",
  });

  page = await browser.newPage({ baseURL: "http://localhost:3100" });
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  const submit = page.getByRole("button", { name: "Sign in" });
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();
  await page.waitForURL("**/mfa");
  totpSecret = (await page.getByTestId("totp-secret").textContent()) ?? "";
  await page.getByLabel("Code").fill(totp(totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/$/);
});

test.afterAll(async () => {
  await page.close();
});

test("lists what is remembered with where it came from, and filters it", async () => {
  await page.goto("/memory");
  await expect(page.getByTestId("memory")).toHaveCount(2); // the unconfirmed habit is listed apart
  await page.getByText("Where this came from").click();
  await expect(page.getByText("You said: “always use Best Buy for electronics”")).toBeVisible();

  await page.getByLabel("Search").fill("window seats");
  await page.getByRole("button", { name: "Filter" }).click();
  await expect(page.getByTestId("memory")).toHaveCount(1);
  await expect(page.getByText("Prefers window seats on flights.")).toBeVisible();
  await page.goto("/memory");
});

test("forgetting hides a memory and restoring brings it back", async () => {
  const item = page.getByTestId("memory").filter({ hasText: "window seats" });
  await item.getByRole("button", { name: "Forget" }).click();
  await expect(item.getByText("forgotten, can be restored")).toBeVisible();
  expect((await memory("Prefers window seats on flights."))?.status).toBe("deleted");

  await item.getByRole("button", { name: "Restore" }).click();
  await expect(item.getByText("forgotten")).toHaveCount(0);
  expect((await memory("Prefers window seats on flights."))?.status).toBe("active");
});

test("a noticed habit is remembered only after the owner says so", async () => {
  const habit = page.getByTestId("pending-memory");
  await expect(habit).toContainText("Prefers Costco for tires.");
  await habit.getByRole("button", { name: "Remember", exact: true }).click();
  await expect(page.getByTestId("pending-memory")).toHaveCount(0);
  expect((await memory("Prefers Costco for tires."))?.status).toBe("active");
});

test("pausing memory is saved", async () => {
  await page.getByRole("button", { name: "Pause memory" }).click();
  await expect(page.getByText("Memory is paused")).toBeVisible();
  const { data } = await admin.from("user_settings").select("memory_enabled").eq("user_id", userId).single();
  expect(data?.memory_enabled).toBe(false);
  await page.getByRole("button", { name: "Resume memory" }).click();
  await expect(page.getByText("Memory is on.")).toBeVisible();
});

test("an open alert is shown on the task list", async () => {
  await admin.from("alerts").insert({ user_id: userId, kind: "task_failures", detail: "3 tasks failed in the last day" });
  await page.goto("/");
  await expect(page.getByTestId("alerts")).toContainText("3 tasks failed in the last day");
});

test("the task list and alerts update on their own, without reloading", async () => {
  await page.goto("/");
  await expect(page.getByTestId("alerts")).toBeVisible();
  await page.waitForTimeout(1500); // let the live connection open

  await admin.from("tasks").insert({
    user_id: userId, short_code: "T9", title: "Live update probe", spec: {}, budget_cents: 300,
  });
  await expect(page.getByText("Live update probe")).toBeVisible({ timeout: 10_000 });

  await admin.from("alerts").update({ resolved_at: new Date().toISOString() }).eq("user_id", userId);
  await expect(page.getByTestId("alerts")).toHaveCount(0, { timeout: 10_000 });
});

test("clearing everything needs a fresh authenticator code", async () => {
  await page.goto("/memory");
  await page.getByLabel("Authenticator code").fill("000000");
  await page.getByRole("button", { name: "Clear", exact: true }).click();
  await expect(page.getByRole("status")).toHaveCount(0);
  expect((await memory("Always buy electronics from Best Buy."))?.status).toBe("active");

  // The sign-in already spent this 30-second window's code.
  const wait = 30_000 - (Date.now() % 30_000) + 500;
  await page.waitForTimeout(wait);
  await page.getByLabel("Authenticator code").fill(totp(totpSecret));
  await page.getByRole("button", { name: "Clear", exact: true }).click();
  await expect(page.getByText(/Cleared \d+\./)).toBeVisible();
  await expect(page.getByText("Nothing is remembered yet.")).toHaveCount(0); // forgotten ones stay listed to undo
  const { data } = await admin.from("memories").select("status").eq("user_id", userId);
  expect(new Set(data?.map((m) => m.status))).toEqual(new Set(["deleted"]));
});
