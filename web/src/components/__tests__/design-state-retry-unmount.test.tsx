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

// ModelViewer owns a real three.js WebGLRenderer, which jsdom cannot
// construct — mock it the same way app-layout.test.tsx does; this test
// never asserts on the viewer, it only needs App to render.
vi.mock("../../components/viewer/ModelViewer", async () => {
  const actual = await vi.importActual<typeof import("../../components/viewer/ModelViewer")>(
    "../../components/viewer/ModelViewer",
  );
  const MockModelViewer = (props: { data: ArrayBuffer | null }) => {
    useEffect(() => {
      props.onReady?.({
        scene: {} as never,
        camera: { position: { x: 0, y: 100, z: 200 } } as never,
        renderer: { domElement: document.createElement("canvas") } as never,
        controls: { target: { x: 0, y: 0, z: 0 } } as never,
        raycaster: {} as never,
        modelRoot: null,
      });
    }, [props, props.data]);
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
    client = new ApiClient("http://test");
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
    // single ~300 ms retry.
    vi.spyOn(client, "getDesignState").mockRejectedValue(new Error("network blip"));

    const { unmount } = render(<App client={client} />);

    const firstRunInput = screen.getByTestId("first-run-input");
    fireEvent.change(firstRunInput, { target: { value: "make a box" } });
    fireEvent.click(screen.getByTestId("first-run-start-btn"));

    // Wait for the design-state refetch to be issued (and to reject).
    await waitFor(() => expect(client.getDesignState).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 50));

    // Unmount BEFORE the ~300 ms retry window elapses.
    unmount();

    // The retry window elapses. The effect cleanup must have cancelled the
    // pending timer: no second fetch, and no post-unmount state update
    // (which would surface as an unhandled rejection under vitest teardown).
    await new Promise((r) => setTimeout(r, 350));
    expect(client.getDesignState).toHaveBeenCalledTimes(1);
  });
});
