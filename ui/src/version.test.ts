import { describe, expect, it } from "vitest";
import { decideReload, RELOAD_KEY } from "./version";

function memory() {
  const m = new Map<string, string>();
  return {
    m,
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  };
}

describe("decideReload", () => {
  it("does nothing when versions match or the server sends none", () => {
    const s = memory();
    expect(decideReload("0.2.1", "0.2.1", s)).toBe("none");
    expect(decideReload("0.2.1", "", s)).toBe("none");
    expect(decideReload("0.2.1", null, s)).toBe("none");
    expect(decideReload("0.2.1", undefined, s)).toBe("none");
  });

  it("reloads once for a mismatch and remembers the pair", () => {
    const s = memory();
    expect(decideReload("0.2.0", "0.2.1", s)).toBe("reload");
    expect(s.m.get(RELOAD_KEY)).toBe("0.2.0->0.2.1");
  });

  it("does not reload again for the same pair (falls back to the manual banner)", () => {
    const s = memory();
    expect(decideReload("0.2.0", "0.2.1", s)).toBe("reload");
    expect(decideReload("0.2.0", "0.2.1", s)).toBe("manual");
    expect(decideReload("0.2.0", "0.2.1", s)).toBe("manual");
  });

  it("reloads again for a different pair", () => {
    const s = memory();
    decideReload("0.2.0", "0.2.1", s);
    expect(decideReload("0.2.0", "0.2.2", s)).toBe("reload");
  });

  it("forgets the marker once the page has caught up, so a later release can reload", () => {
    const s = memory();
    decideReload("0.2.0", "0.2.1", s);
    expect(decideReload("0.2.1", "0.2.1", s)).toBe("none");
    expect(s.m.has(RELOAD_KEY)).toBe(false);
    expect(decideReload("0.2.1", "0.2.2", s)).toBe("reload");
  });

  it("never loops when storage is missing or throws", () => {
    expect(decideReload("0.2.0", "0.2.1", null)).toBe("manual");
    const bad = {
      getItem: () => { throw new Error("denied"); },
      setItem: () => { throw new Error("denied"); },
      removeItem: () => { throw new Error("denied"); },
    };
    expect(decideReload("0.2.0", "0.2.1", bad)).toBe("manual");
    expect(decideReload("0.2.1", "0.2.1", bad)).toBe("none");
  });
});
