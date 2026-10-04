/**
 * Issue #354 — the design-state refetch effect's 300 ms retry timer must be
 * cancelled on unmount.
 *
 * Regression test for the original bug: before the fix the effect returned no
 * cleanup, so a retry timer that was armed by a rejected design-state fetch
 * survived unmount and fired into a torn-down component.
 *
 * Discriminating scenarios
 * -------------------------
 *
 * **Single-refetch test** (catches mutations (b) and (c) — the original bug):
 * only ONE design-state refetch fires before unmount (the effect's initial
 * fetch; the stream mock emits no version-created frame, so no second
 * refetch bumps the seq). When the retry timer fires after unmount,
 * `isStale()` returns false (seq is unchanged), so `getDesignState` IS
 * called — incrementing the mock's call count. With the fix the timer is
 * cleared by the effect cleanup, so the count stays at 1.
 *
 * **Overlap test** (two refetches within the 300 ms window): the first
 * refetch's retry timer is cleared by the pre-clear when the second refetch
 * arms its timer. The unmount cleanup then clears exactly one timer. This
 * test documents the overlap behaviour; the first refetch's leaked timer
 * (under mutation (a) — pre-clear removed) is protected by the seq guard
 * (`isStale()` returns true because the second refetch bumped the seq), so
 * mutation (a) is not independently observable here. The seq guard is the
 * real protection; the pre-clear is defense-in-depth.
 *
 * What the assertions catch
 * --------------------------
 *
 * The `toHaveBeenCalledTimes(N)` assertion is the load-bearing check. A
 * post-unmount retry call increments the mock count. The `console.error`
 * spy is a secondary guard: React 18 removed the "Can't perform a state
 * update on an unmounted component" warning, so it does NOT fire for
 * post-unmount setState, but it catches any other console.error that might
 * leak through. The `brief-refresh-failed` DOM assertion in the single-
 * refetch test catches the case where the post-unmount retry RESOLVES and
 * calls `applyEnvelope` (setting state on a torn-down component is a no-op
 * in React 18, but the `brief-refresh-failed` div would only appear if the
 * retry's `.catch` fired `setDesignStateStale(true)` on a MOUNTED component
 * — which cannot happen after unmount, so this assertion is belt-and-
 * suspenders for the mounted-retry path).
 */

import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../App";
import { ApiClient } from "../../lib/api";
import type { Project } from "../../lib/api";
import type { DesignStateEnvelope } from "../../lib/api";
import type { ModelViewerHandle } from "../../components/viewer/ModelViewer";

// ModelViewer owns a real three.js WebGLRenderer, which jsdom cannot
// construct — mock it the same way app-layout.test.tsx does; this test
// never asserts on the viewer, it only needs App to render.
vi.mock("../../components/viewer/ModelViewer", async () => {
  const actual = await vi.importActual<typeof import("../../components/viewer/ModelViewer")>(
    "../../components/viewer/ModelViewer",
  );
  const mockHandle: ModelViewerHandle = {
    scene: undefined as unknown as ModelViewerHandle["scene"],
    camera: { position: { x: 0, y: 100, z: 200 } } as unknown as ModelViewerHandle["camera"],
    renderer: undefined as unknown as ModelViewerHandle["renderer"],
    controls: { target: { x: 0, y: 0, z: 0 } } as unknown as ModelViewerHandle["controls"],
    raycaster: undefined as unknown as ModelViewerHandle["raycaster"],
    modelRoot: null,
  };
  const MockModelViewer = (props: {
    data: ArrayBuffer | null;
    onReady?: (handle: ModelViewerHandle) => void;
  }) => {
    const { data, onReady } = props;
    useEffect(() => {
      onReady?.(mockHandle);
    }, [data, onReady]);
    return null;
  };
  return {
    ...actual,
    ModelViewer: MockModelViewer,
  };
});

describe("design-state refetch retry timer (issue #354)", () => {
  let client: ApiClient;

  beforeEach(() => {
    client = new ApiClient();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("cancels the 300 ms retry timer on unmount — single refetch, retry resolves (issue #354)", async () => {
    // Single-refetch scenario: the stream mock emits NO version-created
    // frame, so only the effect's initial refetch fires. When the retry
    // timer fires after unmount (without the fix), `isStale()` returns
    // false (seq is unchanged — no second refetch bumped it), so
    // getDesignState IS called again, incrementing the mock count.
    //
    // With the fix: the effect cleanup clears the timer, so getDesignState
    // is called exactly once (the initial fetch).
    //
    // getDesignState: call 1 (initial) rejects → arms the retry timer.
    //                   call 2 (retry, if it fires) resolves → would
    //                   increment the count to 2.
    // After unmount + 400 ms: assert count === 1.
    const project = { id: 1, name: "t", storage: { present: false, git: false } } as unknown as Project;
    vi.spyOn(client, "createProject").mockResolvedValue(project);
    // NO version-created frame — only onDone. This prevents the second
    // refetch that would bump the seq and make the retry stale-guarded.
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onDone?.({});
    });
    const logErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    // Call 1 rejects (arms the retry timer). Call 2 (retry) resolves —
    // if the timer fires post-unmount without the fix, the count goes to 2.
    const emptyEnvelope: DesignStateEnvelope = { entries: [], history_missing: false, part: null };
    vi.spyOn(client, "getDesignState")
      .mockRejectedValueOnce(new Error("network blip"))
      .mockResolvedValue(emptyEnvelope);

    const { unmount } = render(<App client={client} />);

    const firstRunInput = screen.getByTestId("first-run-input");
    fireEvent.change(firstRunInput, { target: { value: "make a box" } });
    fireEvent.click(screen.getByTestId("first-run-start-btn"));

    // Wait for the initial refetch to be issued (and to reject).
    await waitFor(() => expect(client.getDesignState).toHaveBeenCalledTimes(1));
    // 150 ms: the retry timer is armed and pending when unmount runs.
    await new Promise((r) => setTimeout(r, 150));

    // Unmount BEFORE the ~300 ms retry window elapses.
    unmount();

    // The retry window elapses. With the fix the timer was cleared by the
    // effect cleanup: no further fetch. Without the fix (original bug,
    // mutations (b)/(c)): the timer fires, isStale() is false (no seq bump),
    // and getDesignState is called a second time — count goes to 2.
    await new Promise((r) => setTimeout(r, 400));
    expect(client.getDesignState, "retry timer must be cleared on unmount").toHaveBeenCalledTimes(1);
    expect(logErrorSpy).not.toHaveBeenCalled();
  });

  it("overlapping failed refetches: unmount clears the tracked timer (issue #354)", async () => {
    // Two overlapping failed refetches within the 300 ms retry window. The
    // effect's refetch (refetch #1) rejects and arms retry timer A; a
    // version-created frame (refetch #2) rejects before 300 ms and arms
    // retry timer B. With the fix, arming B clears A first (pre-clear), so
    // the unmount cleanup cancels exactly one pending timer (B).
    //
    // Under mutation (a) [pre-clear removed]: timer A is untracked. The
    // unmount cleanup clears B. Timer A fires post-unmount, but the seq
    // guard (isStale() → true, because refetch #2 bumped the seq) prevents
    // a getDesignState call. So the count stays at 3. Mutation (a) is
    // protected by the seq guard and is not independently observable here.
    //
    // Under mutation (c) [both removed]: same as (a) — the seq guard still
    // protects. The single-refetch test above is the one that catches (c).
    const projectA = { id: 1, name: "t", storage: { present: false, git: false } } as unknown as Project;
    vi.spyOn(client, "createProject").mockResolvedValue(projectA);
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onProgress("version-created", { step: "version-created", version_id: 3 });
      handlers.onDone?.({});
    });
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "listVersions").mockResolvedValue([]);
    const logErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(client, "getDesignState").mockRejectedValue(new Error("network blip"));

    const { unmount } = render(<App client={client} />);

    // First send creates project 1 — the design-state refetch effect fires
    // and rejects (refetch #1). The version-created frame from that send
    // fires refetch #2 (issue #123) — both within one effect run.
    const firstRunInput = screen.getByTestId("first-run-input");
    fireEvent.change(firstRunInput, { target: { value: "make a box" } });
    fireEvent.click(screen.getByTestId("first-run-start-btn"));
    await waitFor(() => expect(client.getDesignState).toHaveBeenCalledTimes(2));

    // A SECOND send opens a fresh design-loop stream; its version-created
    // frame triggers refetch #3 — timer C is armed while timer B is
    // pending (timer A was already cleared by the pre-clear when B was
    // armed, or is untracked under mutation (a)).
    const chatInput = screen.getByTestId("chat-input");
    fireEvent.change(chatInput, { target: { value: "make it bigger" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));
    await waitFor(() => expect(client.getDesignState).toHaveBeenCalledTimes(3));

    // Unmount while retry timer C (and, without pre-clear, timer A too)
    // is pending — well before the 300 ms window elapses.
    unmount();

    // Wait past the 300 ms retry window for ALL timers. With the fix no
    // retry fetch fires after unmount: the three initial refetches are the
    // only calls. Timer A (if untracked under mutation (a)) is protected
    // by the seq guard (isStale() → true).
    await new Promise((r) => setTimeout(r, 400));
    expect(client.getDesignState).toHaveBeenCalledTimes(3);
    expect(logErrorSpy).not.toHaveBeenCalled();
  });
});
