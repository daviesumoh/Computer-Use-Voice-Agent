import { createClient } from "@supabase/supabase-js";
import { expect, test, type Page } from "@playwright/test";
import { totp } from "./totp";

// Dashboard -> worker, authenticated with the owner's own Supabase token. The worker reads
// the aal and the time of the last TOTP code from that token (see worker/auth.py).
const SUPABASE_URL = "http://127.0.0.1:55321";
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY ?? "";
const admin = createClient(SUPABASE_URL, SECRET_KEY, { auth: { persistSession: false } });

const EMAIL = "e2e-approver@example.test";
const PASSWORD = "Correct-Horse-Battery-9";

// Each code can be used once per 30 s window, so these tests sometimes wait for the next one.
test.describe.configure({ timeout: 120_000 });

let userId = "";
let taskId = "";
let totpSecret = "";
let lastCodeWindow = -1;

/** A code from a window not used yet; Supabase rejects a code that was already spent. */
async function nextCode(): Promise<string> {
  let window = Math.floor(Date.now() / 30_000);
  if (window === lastCodeWindow) {
    await new Promise((r) => setTimeout(r, 30_000 - (Date.now() % 30_000) + 500));
    window = Math.floor(Date.now() / 30_000);
  }
  lastCodeWindow = window;
  return totp(totpSecret);
}

async function addApproval(summary: string) {
  const { data, error } = await admin
    .from("approvals")
    .insert({
      user_id: userId,
      task_id: taskId,
      action_class: "purchase",
      summary,
      intent_hash: crypto.randomUUID(),
      expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
    })
    .select("id")
    .single();
  if (error) throw error;
  return data.id as string;
}

async function status(table: string, id: string) {
  const { data } = await admin.from(table).select("status").eq("id", id).single();
  return data?.status;
}

async function signIn(page: Page, first: boolean) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  const submit = page.getByRole("button", { name: "Sign in" });
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();
  await page.waitForURL("**/mfa");
  if (first) totpSecret = (await page.getByTestId("totp-secret").textContent()) ?? "";
  await page.getByLabel("Code").fill(await nextCode());
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/$/);
}

test.beforeAll(async () => {
  const { data: existing } = await admin.auth.admin.listUsers({ perPage: 1000 });
  for (const user of existing.users.filter((u) => u.email === EMAIL)) {
    await admin.auth.admin.deleteUser(user.id);
  }
  const created = await admin.auth.admin.createUser({
    email: EMAIL,
    password: PASSWORD,
    email_confirm: true,
  });
  if (created.error) throw created.error;
  userId = created.data.user.id;
  await admin.from("profiles").insert({ id: userId });
  const task = await admin
    .from("tasks")
    .insert({
      user_id: userId, short_code: "T1", title: "Buy the Aurora TV", spec: {}, budget_cents: 300,
      status: "waiting_user", waiting_reason: "approval",
    })
    .select("id")
    .single();
  if (task.error) throw task.error;
  taskId = task.data.id;
});

test("approving needs a fresh authenticator code and then lets the task carry on", async ({ page }) => {
  const approvalId = await addApproval("Buy the Aurora TV: click 'Place order' ($499.99)");
  await signIn(page, true);
  await page.goto("/approvals");
  await expect(page.getByText("click 'Place order' ($499.99)")).toBeVisible();

  // A wrong code is refused by the authentication service and nothing changes.
  await page.getByLabel("Authenticator code").fill("000000");
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(await status("approvals", approvalId)).toBe("pending");

  await page.getByLabel("Authenticator code").fill(await nextCode());
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("purchase · approved")).toBeVisible();
  await expect.poll(() => status("approvals", approvalId)).toBe("approved");
  await expect.poll(() => status("tasks", taskId)).toBe("running"); // the worker woke the task
});

test("denying needs no code", async ({ page }) => {
  const approvalId = await addApproval("Send the order enquiry email");
  await admin.from("tasks").update({ status: "waiting_user", waiting_reason: "approval" }).eq("id", taskId);
  await signIn(page, false);
  await page.goto("/approvals");
  const item = page.getByTestId("approval").filter({ hasText: "Send the order enquiry email" });
  await item.getByRole("button", { name: "Deny" }).click();
  await expect(item.getByText("purchase · denied")).toBeVisible();
  await expect.poll(() => status("approvals", approvalId)).toBe("denied");
});

test("the emergency stop banner clears only with a fresh code", async ({ page }) => {
  await admin.from("user_settings").upsert({ user_id: userId, emergency_stop: true });
  await signIn(page, false);
  const banner = page.getByRole("alert").filter({ hasText: "Emergency stop is on" });
  await expect(banner).toBeVisible();

  await banner.getByLabel("Authenticator code").fill(await nextCode());
  await banner.getByRole("button", { name: "Clear emergency stop" }).click();
  await expect(banner).toHaveCount(0);
  const { data } = await admin.from("user_settings").select("emergency_stop").eq("user_id", userId).single();
  expect(data?.emergency_stop).toBe(false);
});
