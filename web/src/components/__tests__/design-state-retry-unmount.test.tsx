/**
 * Issue #354 — the design-state refetch effect's 300 ms retry timer must be
 * cancelled on unmount.
 *
 * Regression test for the vitest teardown flake: the design-state fetch
 * effect (App.tsx, deps [projectId, apiClient]) schedules a setTimeout inside
 * its .catch that calls setDesignStateStale(true) ~300 ms later. Before the
 * fix the effect returned no cleanup, so the timer survived unmount and fired
 * into a torn-down tree — surfacing as an unhandled rejection during
 * `npm test` teardown.
 *
 * The test drives the exact failure path with the real 300 ms timer:
 *
 *  1. a project is created (the composer's first send — the design-state
 *     effect only fetches once projectId is non-null),
 *  2. that refetch REJECTS — the .catch arms the ~300 ms retry timer,
 *  3. the component is unmounted BEFORE the retry window elapses,
 *  4. the retry window elapses — with the fix the pending timer is cancelled
 *     by the effect cleanup, so the retry fetch never happens and no
 *     setDesignStateStale fires after unmount.
 *
 * With the fix: after 350 ms getDesignState has been called exactly once.
 * Without the fix (the cleanup removed): the retry fires, getDesignState is
 * called a second time, and the post-unmount state update surfaces as an
 * unhandled rejection — the assertion fails.
 */

import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../App";
import { ApiClient } from "../../lib/api";
import type { Project } from "../../lib/api";
import type { ModelViewerHandle } from "../../components/viewer/ModelViewer";

// ModelViewer owns a real three.js WebGLRenderer, which jsdom cannot
// construct — mock it the same way app-layout.test.tsx does; this test
// never asserts on the viewer, it only needs App to render.
vi.mock("../../components/viewer/ModelViewer", async () => {
  const actual = await vi.importActual<typeof import("../../components/viewer/ModelViewer")>(
    "../../components/viewer/ModelViewer",
  );
  // The handle's three.js fields (THREE.Scene, WebGLRenderer, …) are inert
  // in this test — App only stores the handle, and poseSignature reads
  // camera.position. Build it from the real ModelViewerHandle type (no cast
  // to any) and give the one field App actually reads a real shape.
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

  it("cancels the 300 ms retry timer on unmount (no fetch, no stale-set after unmount)", async () => {
    // First send creates the project; that is what flips projectId from null
    // to non-null and triggers the design-state refetch effect.
    const project = { id: 1, name: "t", storage: { present: false, git: false } } as unknown as Project;
    vi.spyOn(client, "createProject").mockResolvedValue(project);
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onProgress("version-created", { step: "version-created", version_id: 3 });
      handlers.onDone?.({});
    });
    // The version-created refetch (call 1) rejects — the .catch schedules the
    // single ~300 ms retry. getProject also rejects in jsdom (no base URL);
    // its console.warn is inert (it does NOT arm a design-state timer), but
    // a console.error (a post-unmount state update) would fail the suite.
    const logErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(client, "getDesignState").mockRejectedValue(new Error("network blip"));

    const { unmount } = render(<App client={client} />);

    const firstRunInput = screen.getByTestId("first-run-input");
    fireEvent.change(firstRunInput, { target: { value: "make a box" } });
    fireEvent.click(screen.getByTestId("first-run-start-btn"));

    // Wait for the design-state refetch to be issued (and to reject). The
    // version-created frame ALSO refetches (issue #123), so the mock
    // reaches a second call before the retry window — the retry timer is
    // armed by the FIRST failure and cleared by the arming of the second
    // (one tracked timer, issue #354 overlap fix).
    await waitFor(() => expect(client.getDesignState).toHaveBeenCalledTimes(2));
    // 150 ms: both retry timers armed and pending when unmount runs.
    // NOTE — the mutation check (unconditional null, no pre-clear) is NOT
    // reliably caught by this test: the two refetches in one effect run
    // reject within ~1 ms of each other, so the unmount cleanup (which
    // cancels the LATEST timer) lands within milliseconds of the older
    // timer's arm and cancels it by luck. The dedicated overlap test below
    // (two sends) is the one that discriminates.
    await new Promise((r) => setTimeout(r, 150));

    // Unmount BEFORE the ~300 ms retry window elapses.
    unmount();

    // The retry window elapses. The effect cleanup must have cancelled the
    // pending timer: no further fetch, and no post-unmount state update
    // (which would surface as an unhandled rejection under vitest teardown).
    await new Promise((r) => setTimeout(r, 350));
    expect(client.getDesignState).toHaveBeenCalledTimes(2);
    expect(logErrorSpy).not.toHaveBeenCalled();
  });

  it("overlapping failed refetches: the older retry cannot fire after unmount (issue #354)", async () => {
    // Two overlapping failed refetches within the 300 ms retry window. The
    // effect's refetch (refetch #1) rejects and arms retry timer A; a
    // version-created frame from the design-loop stream (refetch #2) rejects
    // before 300 ms elapse and arms retry timer B. Without the fix, timer A
    // (the older, untracked one) either untracks timer B or survives the
    // unmount cleanup — either way a retry fetch fires into a torn-down
    // component. With the fix, arming B clears A first, so the unmount
    // cleanup cancels exactly the one pending timer.
    const projectA = { id: 1, name: "t", storage: { present: false, git: false } } as unknown as Project;
    vi.spyOn(client, "createProject").mockResolvedValue(projectA);
    // The first stream (first-run) opens the design loop and emits
    // version-created — that frame is what triggers refetch #2. A second
    // composer send opens another stream; the same frame triggers refetch
    // #2 on the first send. So the overlap is: effect refetch #1 rejects,
    // then the version-created frame fires refetch #2, which also rejects
    // within 300 ms.
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onProgress("version-created", { step: "version-created", version_id: 3 });
      handlers.onDone?.({});
    });
    // The second send POSTs through postChat before opening its stream —
    // without this mock it rejects in jsdom and the stream (and refetch #3)
    // never opens.
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "listVersions").mockResolvedValue([]);
    const logErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    // Both refetches reject — each .catch arms its own retry timer within
    // the 300 ms window.
    vi.spyOn(client, "getDesignState").mockRejectedValue(new Error("network blip"));

    const { unmount } = render(<App client={client} />);

    // First send creates project 1 — the design-state refetch effect fires
    // and rejects (refetch #1). The version-created frame from that send
    // fires refetch #2 as well (issue #123) — both inside one effect run,
    // milliseconds apart, so they do NOT discriminate the overlap (see the
    // NOTE above).
    const firstRunInput = screen.getByTestId("first-run-input");
    fireEvent.change(firstRunInput, { target: { value: "make a box" } });
    fireEvent.click(screen.getByTestId("first-run-start-btn"));
    await waitFor(() => expect(client.getDesignState).toHaveBeenCalledTimes(2));

    // Refetch #2 (the one that armed retry timer B): a SECOND send opens a
    // fresh design-loop stream; its version-created frame triggers
    // refetchDesignState directly — well more than 1 ms after timer A was
    // armed, so timer B outlives any accidental unmount cleanup of timer A.
    const chatInput = screen.getByTestId("chat-input");
    fireEvent.change(chatInput, { target: { value: "make it bigger" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));
    // Refetch #2 fires (via version-created) and rejects within the
    // 300 ms window — timer B is now armed while timer A is still
    // pending, with no cleanup in between.
    await waitFor(() => expect(client.getDesignState).toHaveBeenCalledTimes(3));

    // Unmount while retry timer B (and, without the fix, timer A too) is
    // pending — well before the 300 ms window elapses.
    unmount();

    // Wait past the 300 ms retry window for BOTH timers. With the fix no
    // retry fetch fires after unmount: the three initial refetches are the
    // only calls, and no post-unmount state update surfaces as an error.
    await new Promise((r) => setTimeout(r, 400));
    expect(client.getDesignState).toHaveBeenCalledTimes(3);
    expect(logErrorSpy).not.toHaveBeenCalled();
  });
});
