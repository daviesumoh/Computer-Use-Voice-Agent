import { createClient } from "@supabase/supabase-js";
import { expect, test, type Page } from "@playwright/test";
import { totp } from "./totp";

// Local Supabase only. The secret key and Turnstile dummy token below are the public
// development defaults from `supabase status` and Cloudflare's Turnstile testing docs.
const SUPABASE_URL = "http://127.0.0.1:55321";
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY ?? "";
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!;
const TURNSTILE_DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX";

const EMAIL = "e2e-owner@example.test";
const PASSWORD = "Correct-Horse-Battery-9";

const admin = createClient(SUPABASE_URL, SECRET_KEY, { auth: { persistSession: false } });
let totpSecret = "";
let lastCodeWindow = -1;

/** A fresh code; waits for the next 30 s window if this one's code was already used. */
async function nextCode(): Promise<string> {
  let window = Math.floor(Date.now() / 30_000);
  if (window === lastCodeWindow) {
    await new Promise((r) => setTimeout(r, 30_000 - (Date.now() % 30_000) + 500));
    window = Math.floor(Date.now() / 30_000);
  }
  lastCodeWindow = window;
  return totp(totpSecret);
}

async function signInWithPassword(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  const submit = page.getByRole("button", { name: "Sign in" });
  await expect(submit).toBeEnabled({ timeout: 15_000 }); // waits for the Turnstile token
  await submit.click();
  await page.waitForURL("**/mfa");
}

test.beforeAll(async () => {
  const { data: existing } = await admin.auth.admin.listUsers({ perPage: 1000 });
  for (const user of existing.users.filter((u) => u.email === EMAIL)) {
    await admin.auth.admin.deleteUser(user.id);
  }
  const { data, error } = await admin.auth.admin.createUser({
    email: EMAIL,
    password: PASSWORD,
    email_confirm: true,
  });
  if (error) throw error;
  const userId = data.user.id;
  for (const { error } of [
    await admin.from("profiles").insert({ id: userId }),
    await admin.from("tasks").insert({
      user_id: userId, short_code: "T1", title: "Compare laptop prices", spec: {}, budget_cents: 300,
    }),
  ]) {
    if (error) throw error;
  }
});

test("signed-out visitors are sent to the login page", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("first sign-in enrols an authenticator, then shows the dashboard", async ({ page }) => {
  await signInWithPassword(page);

  // Password alone is not enough: the dashboard still redirects to MFA.
  await page.goto("/");
  await expect(page).toHaveURL(/\/mfa$/);

  totpSecret = (await page.getByTestId("totp-secret").textContent()) ?? "";
  expect(totpSecret).toMatch(/^[A-Z2-7]+=*$/);

  await page.getByLabel("Code").fill("000000");
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("alert")).toBeVisible();

  await page.getByLabel("Code").fill(await nextCode());
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText("Compare laptop prices")).toBeVisible();
});

test("later sign-ins ask for a code instead of re-enrolling", async ({ page }) => {
  await signInWithPassword(page);
  await expect(page.getByTestId("totp-secret")).toHaveCount(0);

  await page.getByLabel("Code").fill(await nextCode());
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByText("Compare laptop prices")).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("a password-only session reads no rows from the API", async () => {
  const client = createClient(SUPABASE_URL, PUBLISHABLE_KEY, { auth: { persistSession: false } });
  const { error } = await client.auth.signInWithPassword({
    email: EMAIL,
    password: PASSWORD,
    options: { captchaToken: TURNSTILE_DUMMY_TOKEN },
  });
  expect(error).toBeNull();

  const { data, error: queryError } = await client.from("tasks").select("id");
  expect(queryError).toBeNull();
  expect(data).toEqual([]); // the task exists (see beforeAll) but needs aal2
});

test("sign-in without a CAPTCHA token is rejected", async () => {
  const client = createClient(SUPABASE_URL, PUBLISHABLE_KEY, { auth: { persistSession: false } });
  const { error } = await client.auth.signInWithPassword({ email: EMAIL, password: PASSWORD });
  expect(error?.message).toMatch(/captcha/i);
});
