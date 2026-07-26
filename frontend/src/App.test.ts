import { describe, expect, it } from "vitest";
import { routeForPath } from "./App";

describe("application routing", () => {
  it("opens Workspace at the root and the legacy workspace path", () => {
    expect(routeForPath("/")).toBe("workspace");
    expect(routeForPath("/live-workspace")).toBe("workspace");
    expect(routeForPath("/unknown")).toBe("workspace");
  });

  it("opens the approval screen and the developer view on the approvals route", () => {
    expect(routeForPath("/approvals")).toBe("approvals");
    expect(routeForPath("/approvals/why")).toBe("approvals");
  });

  it("keeps seeded examples on the scenario route", () => {
    expect(routeForPath("/scenario-lab")).toBe("examples");
    expect(routeForPath("/scenario-lab?scenario=api-read-only")).toBe(
      "examples",
    );
  });
});
