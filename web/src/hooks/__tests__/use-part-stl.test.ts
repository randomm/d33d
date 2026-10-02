/**
 * usePartStl (issue #334, D7) — the part-STL fetch hook: identity-keyed
 * refetch, abort + sequence guard, and the 50 MB buffer release.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, cleanup } from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import type { ApiClient } from "../../lib/api";
import { usePartStl } from "../usePartStl";

function stlBuffer(bytes: number[]): ArrayBuffer {
  // A FRESH ArrayBuffer (Uint8Array's .buffer aliases the pool — the
  // hook's zero-fill must not mutate shared memory or other tests).
  const buf = new ArrayBuffer(bytes.length);
  new Uint8Array(buf).set(bytes);
  return buf;
}

function makeClient() {
  return {
    fetchPartStl: vi.fn().mockResolvedValue(stlBuffer([1, 2, 3])),
  } as unknown as ApiClient;
}

describe("usePartStl", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("returns null with no project id (no fetch)", async () => {
    const client = makeClient();
    const { result } = renderHook(() => usePartStl(null, "a:stl:unsettled:1", client));
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current).toBeNull();
    expect(client.fetchPartStl).not.toHaveBeenCalled();
  });

  it("returns null with no part key (no fetch)", async () => {
    const client = makeClient();
    const { result } = renderHook(() => usePartStl(7, null, client));
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current).toBeNull();
    expect(client.fetchPartStl).not.toHaveBeenCalled();
  });

  it("fetches once for a stable part key and holds the buffer", async () => {
    const client = makeClient();
    const { result } = renderHook(() => usePartStl(7, "box.stl:stl:assumed:1", client));
    await act(async () => {
      await Promise.resolve();
    });
    expect(client.fetchPartStl).toHaveBeenCalledTimes(1);
    expect(client.fetchPartStl).toHaveBeenCalledWith(7, expect.any(AbortSignal));
    expect(result.current).toBeInstanceOf(ArrayBuffer);
    expect(result.current!.byteLength).toBe(3);
  });

  it("does NOT refetch when the identity key is unchanged (a refetch-allocation)", async () => {
    const client = makeClient();
    const { rerender } = renderHook(() => usePartStl(7, "box.stl:stl:assumed:1", client));
    await act(async () => {
      await Promise.resolve();
    });
    // Same key again (a design-state refetch allocated a fresh part object —
    // the key is the identity, not the object).
    rerender();
    await act(async () => {
      await Promise.resolve();
    });
    expect(client.fetchPartStl).toHaveBeenCalledTimes(1);
  });

  it("refetches when the identity key changes (a different part)", async () => {
    const client = makeClient();
    const { rerender } = renderHook(({ k }) => usePartStl(7, k, client), {
      initialProps: { k: "a.stl:stl:assumed:1" } as { k: string },
    });
    await act(async () => {
      await Promise.resolve();
    });
    rerender({ k: "b.stl:stl:assumed:2" });
    await act(async () => {
      await Promise.resolve();
    });
    expect(client.fetchPartStl).toHaveBeenCalledTimes(2);
  });

  it("a failed fetch leaves the buffer null (never a fabricated model)", async () => {
    const client = makeClient();
    vi.mocked(client.fetchPartStl).mockRejectedValue(new Error("404"));
    const { result } = renderHook(() => usePartStl(7, "x.stl:stl:unsettled:null", client));
    await act(async () => {
      await Promise.resolve();
    });
    expect(client.fetchPartStl).toHaveBeenCalledTimes(1);
    expect(result.current).toBeNull();
  });

  it("an out-of-order, slower response never clobbers a newer buffer", async () => {
    const bufA = stlBuffer([1, 1, 1]);
    const bufB = stlBuffer([2, 2, 2]);
    let resolveA: (v: ArrayBuffer) => void = () => {};
    const slowA = new Promise<ArrayBuffer>((r) => (resolveA = r));
    const client = {
      fetchPartStl: vi
        .fn()
        .mockReturnValueOnce(slowA)
        .mockResolvedValue(bufB),
    } as unknown as ApiClient;

    const { result, rerender } = renderHook(
      ({ k }) => usePartStl(7, k, client),
      { initialProps: { k: "a:stl:assumed:1" } as { k: string } },
    );
    await act(async () => {
      await Promise.resolve();
    });
    // The part changes before A resolves (the identity key flips).
    rerender({ k: "b:stl:assumed:2" });
    await act(async () => {
      await Promise.resolve();
    });
    // A is still in flight (or just settled) — settle it now, AFTER B.
    act(() => resolveA(bufA));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    // B (the newer buffer) holds — the stale A never overwrote it.
    expect(result.current).toBe(bufB);
    expect(client.fetchPartStl).toHaveBeenCalledTimes(2);
  });
});
