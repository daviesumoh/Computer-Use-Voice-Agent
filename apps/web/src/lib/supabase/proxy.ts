import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

const PUBLIC_PATHS = ["/login"];
// Authenticated by their own signatures, not a user session.
const WEBHOOK_PREFIX = "/api/twilio/";
const MFA_PATH = "/mfa";

/**
 * Refreshes the session cookie and routes by sign-in state:
 * not signed in → /login; password only (aal1) → /mfa; MFA verified (aal2) → requested page.
 */
export async function updateSession(request: NextRequest) {
  if (request.nextUrl.pathname.startsWith(WEBHOOK_PREFIX)) return NextResponse.next();

  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet, headers) {
          cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
          Object.entries(headers).forEach(([key, value]) => response.headers.set(key, value));
        },
      },
    },
  );

  // Nothing may run between createServerClient and getClaims (session refresh depends on it).
  const { data } = await supabase.auth.getClaims();
  const claims = data?.claims;
  const path = request.nextUrl.pathname;

  const target = !claims
    ? PUBLIC_PATHS.includes(path) ? null : "/login"
    : claims.aal !== "aal2"
      ? path === MFA_PATH ? null : MFA_PATH
      : path === MFA_PATH || PUBLIC_PATHS.includes(path) ? "/" : null;

  if (target === null) return response;

  const url = request.nextUrl.clone();
  url.pathname = target;
  url.search = "";
  const redirect = NextResponse.redirect(url);
  // Keep any refreshed session cookies on the redirect.
  response.cookies.getAll().forEach((cookie) => redirect.cookies.set(cookie));
  return redirect;
}
