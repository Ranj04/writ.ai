import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("service health", () => {
  it("checks all three services with the active signal", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await expect(api.health(controller.signal)).resolves.toEqual({
      authority: true,
      agent: true,
      executor: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/:8001\/health$/),
        expect.stringMatching(/:8002\/health$/),
        expect.stringMatching(/:8003\/health$/),
      ]),
    );
    fetchMock.mock.calls.forEach(([, init]) => {
      expect(init).toEqual({ signal: controller.signal });
    });
  });

  it("marks only the unavailable service offline", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (url.includes(":8002")) {
          return Promise.reject(new TypeError("Failed to fetch"));
        }
        return Promise.resolve(new Response(null, { status: 200 }));
      }),
    );

    await expect(api.health()).resolves.toEqual({
      authority: true,
      agent: false,
      executor: true,
    });
  });
});
