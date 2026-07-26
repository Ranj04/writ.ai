import { HexclaveClientApp } from "@hexclave/react";

/**
 * The browser's Hexclave identity, used by the `/approvals` surface only.
 *
 * Why this exists: the approval screen used to read its credential from
 * `globalThis.__WRITAI_APPROVAL_TOKEN__`, a value an operator pasted into the
 * console for one session. Lane D chose that deliberately over a `VITE_*`
 * variable — Vite inlines those into the bundle, which would ship an approval
 * credential to every visitor — but recorded it as "still for a human"
 * (ASSUMPTIONS A2). This is that human's answer: a real sign-in, so the person
 * approving in the browser is a Hexclave user the server can resolve and check.
 *
 * **Constructed lazily, and never at import time.** `new HexclaveClientApp()`
 * THROWS when no project id is configured, so building it at module scope made
 * merely importing this file fatal — which white-screened `/approvals` on any
 * machine without Hexclave set up, and took three test files down with it. An
 * unconfigured integration must degrade to a labelled fallback, not a crash, so
 * every failure here resolves to "no identity" instead.
 *
 * `tokenStore: "cookie"` keeps the session out of `localStorage`, so an XSS on
 * this page cannot read the approval credential out of JS-readable storage.
 */
function createClient() {
  return new HexclaveClientApp({
    tokenStore: "cookie",
    urls: { default: { type: "hosted" } },
  });
}

// Inferred, not annotated: `HexclaveClientApp` is generic, and writing the bare
// name widens `tokenStore` to include null and stops matching the provider prop.
type HexclaveClient = ReturnType<typeof createClient>;

let cached: HexclaveClient | null | undefined;

export function hexclaveClient(): HexclaveClient | null {
  if (cached !== undefined) return cached;
  try {
    cached = createClient();
  } catch (error) {
    // No project configured. This is a supported state: the approval screen
    // renders a labelled rehearsal and nothing is ever posted.
    console.warn(
      `[writai/hexclave] identity unavailable, approvals will rehearse (${
        error instanceof Error ? error.message.split("\n")[0] : String(error)
      })`,
    );
    cached = null;
  }
  return cached;
}

/**
 * The bearer value for the current signed-in user, or `null` when nobody is
 * signed in — or when Hexclave is not configured at all.
 *
 * Returning `null` rather than throwing is load-bearing: the approval screen
 * treats "no identity" as a labelled rehearsal, which is exactly what should
 * happen. It must never become a silent live approval.
 */
export async function hexclaveApprovalToken(): Promise<string | null> {
  const app = hexclaveClient();
  if (app === null) return null;
  try {
    const header = await app.getAuthorizationHeader();
    if (typeof header !== "string" || !header.trim()) return null;
    // The SDK returns a full `Authorization` header value. The server takes a
    // bare token in the approval envelope, so strip a Bearer prefix if present.
    return header.replace(/^Bearer\s+/i, "").trim() || null;
  } catch (error) {
    // Not signed in, or offline. Both mean "no identity", and neither may
    // approve anything.
    console.warn(
      `[writai/hexclave] no approval identity available (${
        error instanceof Error ? error.message : String(error)
      })`,
    );
    return null;
  }
}
