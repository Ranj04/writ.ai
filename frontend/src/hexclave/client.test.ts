import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const sdk = vi.hoisted(() => ({
  construct: vi.fn(),
  getAccessToken: vi.fn(),
  getAuthorizationHeader: vi.fn(),
  redirectToSignIn: vi.fn(),
}));

vi.mock("@hexclave/react", () => ({
  HexclaveClientApp: class {
    constructor(options: unknown) {
      sdk.construct(options);
    }

    getAccessToken = sdk.getAccessToken;
    getAuthorizationHeader = sdk.getAuthorizationHeader;
    redirectToSignIn = sdk.redirectToSignIn;
  },
}));

describe("Hexclave browser identity", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("VITE_WRITAI_HEXCLAVE_SIGN_IN", "1");
    vi.stubEnv(
      "VITE_HEXCLAVE_PROJECT_ID",
      "54514e09-6629-4265-88cc-85fbb4ad119e",
    );
    sdk.construct.mockReset();
    sdk.getAccessToken.mockReset();
    sdk.getAuthorizationHeader.mockReset();
    sdk.redirectToSignIn.mockReset();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("sends the raw access token expected by the backend resolver", async () => {
    sdk.getAccessToken.mockResolvedValue(" raw-access-token ");
    const { hexclaveApprovalToken } = await import("./client");

    await expect(hexclaveApprovalToken()).resolves.toBe("raw-access-token");
    expect(sdk.getAuthorizationHeader).not.toHaveBeenCalled();
    expect(sdk.construct).toHaveBeenCalledWith(
      expect.objectContaining({
        projectId: "54514e09-6629-4265-88cc-85fbb4ad119e",
        tokenStore: "cookie",
        urls: { default: { type: "hosted" } },
      }),
    );
  });

  it("starts hosted sign-in without approving anything", async () => {
    sdk.redirectToSignIn.mockResolvedValue(undefined);
    const { redirectToHexclaveSignIn } = await import("./client");

    await expect(redirectToHexclaveSignIn()).resolves.toBe(true);
    expect(sdk.redirectToSignIn).toHaveBeenCalledOnce();
    expect(sdk.getAccessToken).not.toHaveBeenCalled();
  });

  it("does not construct the SDK when browser sign-in is disabled", async () => {
    vi.stubEnv("VITE_WRITAI_HEXCLAVE_SIGN_IN", "0");
    const { hexclaveClient, hexclaveSignInEnabled } = await import("./client");

    expect(hexclaveSignInEnabled()).toBe(false);
    expect(hexclaveClient()).toBeNull();
    expect(sdk.construct).not.toHaveBeenCalled();
  });

  it("does not offer sign-in without a public project id", async () => {
    vi.stubEnv("VITE_HEXCLAVE_PROJECT_ID", "");
    const { hexclaveClient, hexclaveSignInEnabled } = await import("./client");

    expect(hexclaveSignInEnabled()).toBe(false);
    expect(hexclaveClient()).toBeNull();
    expect(sdk.construct).not.toHaveBeenCalled();
  });
});
