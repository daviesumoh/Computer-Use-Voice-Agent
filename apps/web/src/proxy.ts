import { type NextRequest } from "next/server";
import { updateSession } from "@/lib/supabase/proxy";

export async function proxy(request: NextRequest) {
  return await updateSession(request);
}

export const config = {
  // Never intercept framework internals (assets, HMR) or the files a browser fetches on its
  // own to run notifications.
  matcher: ["/((?!_next/|favicon.ico|sw.js|manifest.webmanifest).*)"],
};
