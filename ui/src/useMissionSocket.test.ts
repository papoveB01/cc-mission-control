import { describe, expect, it } from "vitest";
import { backoffDelay, connectionAfterFailure } from "./useMissionSocket";

describe("reconnect policy", () => {
  it("backs off 0.5 s up to 5 s", () => {
    expect([0, 1, 2, 3, 4, 5, 9].map(backoffDelay)).toEqual([500, 1000, 2000, 4000, 5000, 5000, 5000]);
  });
  it("goes offline after 3 consecutive failures", () => {
    expect(connectionAfterFailure(1)).toBe("reconnecting");
    expect(connectionAfterFailure(2)).toBe("reconnecting");
    expect(connectionAfterFailure(3)).toBe("offline");
  });
});
