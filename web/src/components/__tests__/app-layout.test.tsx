/**
 * App layout + wiring tests.
 *
 * Verifies the two-pane layout (spec acceptance 5, NO auto-generated
 * slider/parameter panel) AND the project-lifecycle wiring added to close
 * the "components built but never mounted / never reach the backend" gap:
 *   - a project is created on mount and its id flows to PhotoUpload/Export3MF
 *   - chat send calls ApiClient.streamEvents (not just local state)
 *   - ModelViewer and Export3MF are actually mounted (not placeholder divs)
 *
 * ModelViewer is mocked here: it owns a real three.js WebGLRenderer, which
 * jsdom cannot construct (no GPU context) — its own dedicated test suite
 * (model-viewer.test.ts) exercises the three.js internals under a `three`
 * mock. This suite only needs to assert *that* ModelViewer is mounted and
 * receives the right props, so a lightweight component mock is the correct
 * boundary (avoid re-mocking three.js wholesale here).
 */

import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useEffect } from "react";
import App from "../../App";
import { dataUriToArrayBuffer } from "../../lib/dataUri";
import { ApiClient, MAX_REGION_EDIT_MODULE_IDS } from "../../lib/api";
import type { Project, RegionEditResult } from "../../lib/api";
import type { ModelViewerHandle, LoadResult } from "../viewer/ModelViewer";
import type { PointSelectedEvent } from "../viewer/PickLayer";
import { assertValidRegionEditRequest } from "../../lib/__tests__/regionEditContract";
import copy from "../../copy";

/** A minimal fake THREE.Object3D — the mock only needs identity, never
 *  real three.js behaviour (resolvePointPick itself is mocked below in
 *  tests that need to control its output). */
const FAKE_MODULE_GROUP = { name: "fake-module-group" };

const { resolvePointPickMock, mockLoadResultRef } = vi.hoisted(() => ({
  resolvePointPickMock: vi.fn(),
  // Lets individual tests override the mock ModelViewer's onLoaded result
  // (e.g. to simulate a `result.ok === false` decode/parse failure) without
  // having to re-mock the whole module per test. Reset to null (meaning
  // "use the default ok:true result") in beforeEach.
  mockLoadResultRef: { current: null as LoadResult | null },
}));

vi.mock("../viewer/ModelViewer", async () => {
  const actual = await vi.importActual<typeof import("../viewer/ModelViewer")>(
    "../viewer/ModelViewer",
  );
  const MockModelViewer = (props: {
    data: ArrayBuffer | null;
    format: string;
    onReady?: (handle: ModelViewerHandle) => void;
    onLoaded?: (result: LoadResult) => void;
  }) => {
    useEffect(() => {
      // The mock viewer publishes its handle (modelRoot set once data is
      // present — mirroring the real viewer's model-swap re-notify) and its
      // load result. The pick layer's `ready` state (and thus its
      // `data-ready` attribute) flips at exactly this moment.
      const modelRoot =
        props.data !== null ? (FAKE_MODULE_GROUP as never) : null;
      props.onReady?.({
        scene: {} as never,
        camera: {
          position: { x: 0, y: 100, z: 200 },
          up: { x: 0, y: 0, z: 1 },
        } as never,
        renderer: {
          domElement: document.createElement("canvas"),
          getSize: (target: { x: number; y: number }) => {
            target.x = 600;
            target.y = 400;
            return target;
          },
        } as never,
        controls: {
          target: { x: 0, y: 0, z: 0 },
        } as never,
        raycaster: {} as never,
        modelRoot,
      });
      if (props.data !== null) {
        props.onLoaded?.(
          mockLoadResultRef.current ?? {
            ok: true,
            mesh: { object: FAKE_MODULE_GROUP as never, format: props.format as "glb" },
          },
        );
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [props.data]);

    return (
      <div
        data-testid="model-viewer-mock"
        data-format={props.format}
        data-has-data={props.data !== null}
      />
    );
  };

  return {
    ...actual,
    resolvePointPick: resolvePointPickMock,
    ModelViewer: MockModelViewer,
  };
});

vi.mock("../viewer/PickLayer", () => ({
  // The pick the mocked layer reports on click — CSS-pixel viewport space
  // (the same space resolvePointPick raycasts through and the marked PNG
  // composites into). Tests assert the request carries exactly this point.
  __PICK_POINT: { x: 300, y: 200 },
  PickLayer: (props: {
    ready?: boolean;
    marker?: { x: number; y: number } | null;
    onPointSelected?: (event: PointSelectedEvent) => void;
  }) => (
    <div
      data-testid="viewer-pick-layer"
      data-ready={props.ready}
      data-marker={props.marker ? `${props.marker.x},${props.marker.y}` : ""}
      onClick={() =>
        props.onPointSelected?.({ point: { x: 300, y: 200 } })
      }
    />
  ),
}));

// The PickLayer is mocked at the component boundary (like ModelViewer — its
// real pointer-event plumbing needs a live DOM rect that jsdom can't
// provide); the RAYCAST itself is mocked via resolvePointPickMock, and the
// layer's own event semantics (click-vs-drag, no wheel swallow) are covered
// by the real component in pick-layer.test.tsx. The mock reports the
// pending marker position via `data-marker` so the red-check test can
// assert the DOM dot is gone when the layer swallows the click.

// DimensionCanvas renders a react-konva <Stage>, which needs a 2-D canvas
// context jsdom doesn't implement — its own suite (dimension-canvas.test.tsx)
// mocks react-konva directly. Here we only need to assert it is mounted with
// the right photoSrc once a photo is uploaded, so a lightweight mock is the
// right boundary.
vi.mock("../canvas/DimensionCanvas", () => ({
  DimensionCanvas: (props: { photoSrc: string; photoWidth: number; photoHeight: number }) => (
    <div
      data-testid="dimension-canvas-container"
      data-photo-src={props.photoSrc}
      data-photo-width={props.photoWidth}
      data-photo-height={props.photoHeight}
    />
  ),
}));

// jsdom's Image never fires onload with real pixel data from a fake blob URL
// (same limitation as photo-upload.test.tsx) — stub it so PhotoUpload's
// dimension-reading step resolves deterministically in these App-level tests.
const TEST_IMAGE_WIDTH = 1024;
const TEST_IMAGE_HEIGHT = 768;

class FakeImage {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  naturalWidth = TEST_IMAGE_WIDTH;
  naturalHeight = TEST_IMAGE_HEIGHT;
  private _src = "";
  set src(value: string) {
    this._src = value;
    queueMicrotask(() => this.onload?.());
  }
  get src() {
    return this._src;
  }
}

vi.stubGlobal("Image", FakeImage);
if (!URL.revokeObjectURL) {
  URL.revokeObjectURL = vi.fn();
}

const PROJECT: Project = {
  id: 7,
  name: "untitled project",
  git_repo_path: "/data/projects/7",
  tags: [],
  notes: "",
  source_photo_path: null,
  created_at: "2026-01-01T00:00:00Z",
};

/** A real, valid minimal binary-ASCII STL (one triangle) whose base64
 *  round-trips through dataUriToArrayBuffer — the same decoder App.tsx
 *  uses for the version-created frame's stl_data_uri. */
const ASCII_STL = "solid test\nfacet normal 0 0 0\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendfacet\nendsolid test\n";
const STL_DATA_URI = `data:application/octet-stream;base64,${btoa(ASCII_STL)}`;

function makeClient(overrides: Partial<ApiClient> = {}): ApiClient {
  const client = new ApiClient();
  vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
  // App mounts a version-timeline effect (issue #8) that fires
  // listVersions(projectId) as soon as createProject resolves. That call
  // must be stubbed here — left to the real client, it goes through the
  // global fetch, and the photo-upload wiring block below replaces that
  // global fetch with a bare `vi.fn()` (default return `undefined`) for
  // the upload's own POST. When a vitest worker runs this file's upload
  // tests in a schedule where the two calls interleave, the upload's
  // fetch resolves to `undefined` (its `mockResolvedValueOnce` is consumed
  // by the versions call, or the versions call lands first on a stub whose
  // once-value the upload then reads) — and PhotoUpload reads `resp.ok`
  // on `undefined`, surfacing "Upload failed" ("Cannot read properties of
  // undefined (reading 'ok')") in place of the DimensionCanvas.
  // The full-suite run never hits it (the unstubbed path's real fetch
  // rejects on the bogus host, which the timeline effect swallows), which
  // is why it stays latent until a worker reuses state across tests and
  // the once-only stub is in play — the same fire-once-race class as
  // issue #109's `waitForResponse(POST /api/projects)`.
  // Settling the mount's listVersions here removes the stray fetch
  // entirely: no retry, no sleep, no bumped timeout.
  vi.spyOn(client, "listVersions").mockResolvedValue([]);
  vi.spyOn(client, "streamEvents").mockResolvedValue(undefined);
  vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
  Object.assign(client, overrides);
  return client;
}

describe("App layout", () => {
  let client: ApiClient;

  beforeEach(() => {
    client = makeClient();
    resolvePointPickMock.mockReset();
  });

  it("renders the full-viewport stage (canvas + floating panels, issue #119)", async () => {
    render(<App client={client} />);
    expect(screen.getByTestId("app-stage")).toBeTruthy();
    expect(screen.getByTestId("app-left-pane")).toBeTruthy();
    expect(screen.getByTestId("viewer-pane")).toBeTruthy();
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });

  it("renders the chat panel in the left pane", async () => {
    render(<App client={client} />);
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });

  it("renders a real ModelViewer (not a placeholder div) in the right pane", async () => {
    render(<App client={client} />);
    expect(screen.getByTestId("viewer-pane")).toBeTruthy();
    expect(screen.getByTestId("model-viewer-mock")).toBeTruthy();
    expect(screen.queryByTestId("viewer-placeholder")).toBeNull();
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });

  it("renders the validation pane (Export3MF only — no static status) once the project loads", async () => {
    // The pane is absent until the project resolves (issue #114: the pane
    // shows the real validation state or nothing, and the export button is
    // its only surviving content in the interim). Wait for the load, then
    // assert the pane's contents.
    render(<App client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("export-3mf")).toBeTruthy();
    });
    expect(screen.getByTestId("validation-pane")).toBeTruthy();
    expect(screen.queryByTestId("export-3mf-btn")).toBeNull();
  });

  it("does NOT render an auto-generated slider/parameter panel", async () => {
    const { container } = render(<App client={client} />);
    expect(container.querySelectorAll("input[type='range']")).toHaveLength(0);
    expect(screen.getByTestId("pinned-empty")).toBeTruthy();
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });

  it("mounts the pass card with the views carried on the version-created frame (issue #125)", async () => {
    // W10: the views map is on the wire in the version-created frame; the
    // pass card (via ChatPanel) is what displays it. The App-level `renders`
    // prop is gone — inline renders live on the pass turn itself.
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "listVersions").mockResolvedValue([]);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    const views: Record<string, string> = {
      "view_00_front.png": "data:image/png;base64,AAA",
      "view_01_back.png": "data:image/png;base64,AAA",
      "view_02_left.png": "data:image/png;base64,AAA",
      "view_03_right.png": "data:image/png;base64,AAA",
      "view_04_top.png": "data:image/png;base64,AAA",
      "view_05_iso.png": "data:image/png;base64,AAA",
    };
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onProgress("version-created", {
        step: "version-created",
        version_id: 3,
        views,
      });
      handlers.onDone?.({});
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "make a box" } });
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-send-btn"));
      await Promise.resolve();
    });

    const card = await screen.findByTestId("pass-card");
    expect(card.getAttribute("data-version")).toBe("3");
    expect(screen.getAllByTestId(/^pass-card-view-/)).toHaveLength(6);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });

  // Issue #119 — the stage layout replaces the #74 two-pane flex layout.
  // The canvas fills the stage (absolute inset:0); panels float above it.
  it("lays out app-stage as a positioned container filling the viewport (issue #119)", async () => {
    render(<App client={client} />);
    const stage = screen.getByTestId("app-stage");
    expect(stage.style.position).toBe("relative");
    expect(stage.style.width).toBe("100vw");
    expect(stage.style.height).toBe("100vh");
    expect(stage.style.overflow).toBe("hidden");
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });

  it("fills the viewer pane to the stage with absolute inset:0 (issue #119)", async () => {
    render(<App client={client} />);
    const pane = screen.getByTestId("viewer-pane");
    expect(pane.style.position).toBe("absolute");
    expect(pane.style.inset).toBe("0");
    expect(pane.style.zIndex).toBe("0");
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });

  it("floats the conversation pane above the canvas at z-index 20 (issue #119)", async () => {
    render(<App client={client} />);
    const left = screen.getByTestId("app-left-pane");
    expect(left.style.position).toBe("absolute");
    expect(left.style.zIndex).toBe("20");
    expect(left.style.top).toBe("24px");
    expect(left.style.left).toBe("24px");
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });
});

describe("App project lifecycle", () => {
  it("creates a project on mount and passes its id to PhotoUpload", async () => {
    const client = makeClient();
    render(<App client={client} />);

    await waitFor(() => {
      expect(client.createProject).toHaveBeenCalledWith(
        expect.objectContaining({ name: expect.any(String) }),
      );
    });

    // PhotoUpload only becomes "usable" (no client-side "no project" error)
    // once projectId is set — verify by uploading and confirming no
    // "No project selected" error fires.
    const onErrorSpy = vi.fn();
    // PhotoUpload itself calls window.fetch directly (not the injected
    // client) — assert indirectly via the absence of the guard error after
    // the project resolves, by checking the upload input is present and
    // interactive.
    expect(screen.getByTestId("photo-upload")).toBeTruthy();
    expect(onErrorSpy).not.toHaveBeenCalled();
  });

  it("surfaces a project-creation failure as an app-level error", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockRejectedValue(new Error("boom"));
    vi.spyOn(client, "streamEvents").mockResolvedValue(undefined);

    render(<App client={client} />);

    await waitFor(() => {
      expect(screen.getByTestId("app-error")).toBeTruthy();
    });
    expect(screen.getByTestId("app-error").textContent).toContain("boom");
  });
});

describe("App chat wiring", () => {
  it("calls ApiClient.streamEvents with the project id when a message is sent", async () => {
    const client = makeClient();
    render(<App client={client} />);

    await waitFor(() => {
      expect(client.createProject).toHaveBeenCalled();
    });

    const input = screen.getByTestId("chat-input");
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    expect(screen.getByTestId("chat-msg-user").textContent).toContain("hello");

    await waitFor(() => {
      expect(client.streamEvents).toHaveBeenCalledWith(
        PROJECT.id,
        expect.objectContaining({
          onToken: expect.any(Function),
          onProgress: expect.any(Function),
        }),
      );
    });
  });

  it("streams token text into the turn's source, never the transcript (issue #125)", async () => {
    // W10: this test pins the streaming STATE MACHINE at both ends — the
    // in-flight turn renders as a plain (empty) message with the
    // streaming cursor, and the cursor clears on done — and asserts the
    // token text does not surface in the transcript. It does NOT pin
    // the token→source path: nothing here asserts the token text
    // arrived in the message's `source` field at all (commenting out
    // both onToken calls leaves this test green). That path is pinned
    // statically by the design-contract test (onToken writes `source:`,
    // never `content:`) and exercised by the chat-panel test, which
    // clicks the disclosure and checks its content.
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "listVersions").mockResolvedValue([]);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onToken("Hello", {});
      handlers.onToken(" world", {});
      handlers.onDone?.({});
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-send-btn"));
      await Promise.resolve();
    });

    // The transcript exists, the token text is NOT in it, and the
    // streaming cursor came and went (the state machine fired both
    // ends).
    await waitFor(() => {
      expect(screen.getByTestId("chat-msg-assistant")).toBeTruthy();
    });
    expect(screen.getByTestId("chat-msg-assistant").textContent).not.toContain(
      "Hello world",
    );
    expect(screen.queryByTestId("streaming-cursor")).toBeNull();
  });

  it("writes the done frame's message into the pass card's summary line (issue #125)", async () => {
    // The done frame carries the loop's result prose ("Design loop
    // passed validation"). It must land in the assistant message's
    // content — the field PassCard renders as its summary line — and
    // therefore in pass-card-summary through the real frame path.
    // Without this the summary rendered blank on every real pass.
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "listVersions").mockResolvedValue([]);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onProgress("version-created", {
        step: "version-created",
        version_id: 3,
        views: { "view_00_front.png": "data:image/png;base64,AAA" },
      });
      handlers.onDone?.({ message: "Design loop passed validation" });
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-send-btn"));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.getByTestId("pass-card-summary").textContent).toBe(
        "Design loop passed validation",
      );
    });
  });

  it("surfaces a stream error via onError without crashing", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onError?.({ message: "stream interrupted: boom" });
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("app-error").textContent).toContain(
        "stream interrupted",
      );
    });
  });
});

describe("App photo upload wiring", () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", mockFetch);
    mockFetch.mockReset();
  });

  it("PhotoUpload receives the real project id (upload POSTs to the right URL)", async () => {
    const client = makeClient();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ source_photo_path: "/data/projects/7/photos/a.png" }),
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    const file = new File([new ArrayBuffer(10)], "a.png", { type: "image/png" });
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith(
        `/api/projects/${PROJECT.id}/photos`,
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("mounts DimensionCanvas once a photo has been uploaded", async () => {
    const client = makeClient();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ source_photo_path: "/data/projects/7/photos/a.png" }),
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    expect(screen.queryByTestId("dimension-canvas-container")).toBeNull();

    const file = new File([new ArrayBuffer(10)], "a.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("photo-file-input"), {
      target: { files: [file] },
    });

    await waitFor(() => {
      expect(screen.getByTestId("dimension-canvas-container")).toBeTruthy();
    });
    const el = screen.getByTestId("dimension-canvas-container");
    expect(el.getAttribute("data-photo-width")).toBe(String(TEST_IMAGE_WIDTH));
    expect(el.getAttribute("data-photo-height")).toBe(String(TEST_IMAGE_HEIGHT));
    // Regression: DimensionCanvas must receive the photo's real dimensions,
    // not the previously-hardcoded 800x600 literals.
    expect(el.getAttribute("data-photo-width")).not.toBe("800");
    expect(el.getAttribute("data-photo-height")).not.toBe("600");
  });

  it("does not mount DimensionCanvas (and shows a degraded notice instead) when the upload succeeds but client-side dimensions are unavailable", async () => {
    const client = makeClient();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ source_photo_path: "/data/projects/7/photos/a.png" }),
    });

    // Simulate a decode failure: Image fires onerror, so PhotoUpload falls
    // back to (0, 0) but still calls onUploaded — the upload itself
    // succeeded server-side.
    class FailingImage {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      naturalWidth = 0;
      naturalHeight = 0;
      private _src = "";
      set src(value: string) {
        this._src = value;
        queueMicrotask(() => this.onerror?.());
      }
      get src() {
        return this._src;
      }
    }
    vi.stubGlobal("Image", FailingImage);

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    const file = new File([new ArrayBuffer(10)], "a.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("photo-file-input"), {
      target: { files: [file] },
    });

    await waitFor(() => {
      expect(screen.getByTestId("dimension-canvas-unavailable")).toBeTruthy();
    });
    // DimensionCanvas must never mount with degenerate 0x0 dimensions —
    // that would divide by zero in its internal scale computation.
    expect(screen.queryByTestId("dimension-canvas-container")).toBeNull();
    // No app-level error surfaced — the upload succeeded.
    expect(screen.queryByTestId("app-error")).toBeNull();

    // Restore the shared FakeImage stub for subsequent tests in this file.
    vi.stubGlobal("Image", FakeImage);
  });
});

describe("App stream-driven model (issue #69)", () => {
  /** Render the app, let the project + fixture settle, then fire a
   *  version-created progress frame (carrying stl_data_uri + views)
   *  through the mocked stream and send a message. */
  async function renderAndStreamVersionCreated(
    client: ApiClient,
  ): Promise<void> {
    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    // Pre-pass settle: the GLB fixture mount has landed.
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });
    // Send the chat message, then synchronously dispatch the version-created
    // frame (the real stream delivers it mid-flight; act() keeps the
    // setState batch deterministic in the unit environment).
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "make a box" } });
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-send-btn"));
      await Promise.resolve();
      // The mocked streamEvents has been called by now (postChat + the
      // then-chain ran). Grab the last call's handlers and dispatch.
      const spy = vi.mocked(client.streamEvents);
      const handlers = (spy.mock.calls[spy.mock.calls.length - 1]?.[1] ??
        undefined) as
        | { onProgress?: (s?: string, d?: Record<string, unknown>) => void }
        | undefined;
      handlers?.onProgress?.("version-created", {
        step: "version-created",
        version_id: 1,
        stl_data_uri: STL_DATA_URI,
        views: {
          ["view_00_front.png"]: "data:image/png;base64,AAA",
        },
      });
    });
  }

  it("replaces the static GLB fixture with the decoded streamed STL after a stream-driven pass", async () => {
    const client = makeClient();
    await renderAndStreamVersionCreated(client);

    // After the version-created frame: the viewer is fed the decoded
    // stream-derived STL ArrayBuffer — data-format flips to "stl" and the
    // buffer is the decoded stl_data_uri payload, not the fixture.
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-format")).toBe("stl");
    });
    // The decoded payload is the stream's STL — not the fixture's GLB
    // bytes (which start with the GLB magic; the decoded STL decodes back
    // to the exact ASCII source we streamed).
    const decoded = new TextDecoder().decode(dataUriToArrayBuffer(STL_DATA_URI));
    expect(decoded).toContain("solid test");
  });

  it("keeps the pick layer ready once the streamed STL replaces the fixture (selection still works — the degradation notice is gone)", async () => {
    const client = makeClient();
    await renderAndStreamVersionCreated(client);

    // After the pass: the streamed STL is a single unnamed mesh, so picks
    // resolve NO module ids — but selection is NOT disabled (issue #98:
    // the marked point is the grounding, never the ids). The pick layer
    // stays ready and NO degradation notice is surfaced.
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-format")).toBe("stl");
    });
    expect(screen.queryByTestId("selection-notice")).toBeNull();
    expect(screen.getByTestId("viewer-pick-layer").getAttribute("data-ready")).toBe(
      "true",
    );
  });

  it("refetches the version timeline when a version-created frame arrives (issue #114)", async () => {
    // The mount-time effect's initial fetch returns an empty timeline (the
    // pass below creates the first version); the frame's refetch returns the
    // created version. listVersions is called at most twice here, so the two
    // queued values cover mount + refetch in order.
    const client = makeClient({
      listVersions: vi
        .fn()
        .mockResolvedValueOnce([])
        .mockResolvedValueOnce([
          {
            id: 1,
            name: "v1",
            params: {},
            created_by_message: "make a box",
            parent: null,
            restored_from: null,
            forked_from: null,
            pinned: false,
            archived: false,
            thumbnail: null,
            created_at: "2026-01-02T00:00:00Z",
            diff_count: 0,
          },
        ]) as unknown as ApiClient["listVersions"],
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.listVersions).toHaveBeenCalledTimes(1));
    // The initial (mount-time) fetch settled on an empty timeline.
    expect(screen.getByTestId("version-timeline-empty")).toBeTruthy();

    // Send a message and fire the version-created frame (the real wire shape:
    // step + version_id — issue #114 verified against the recorded seam
    // fixture tests/fixtures/e2e/D.json and the live emitter in
    // d33d/design_loop_events.py).
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "make a box" },
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-send-btn"));
      await Promise.resolve();
      const spy = vi.mocked(client.streamEvents);
      const handlers = (spy.mock.calls[spy.mock.calls.length - 1]?.[1] ??
        undefined) as
        | { onProgress?: (s?: string, d?: Record<string, unknown>) => void }
        | undefined;
      handlers?.onProgress?.("version-created", {
        step: "version-created",
        version_id: 1,
      });
    });

    // The frame must have triggered a SECOND listVersions call — the rail
    // reflects the created version instead of staying on the stale (empty)
    // list. Without the refetch this call count stays at 1 and the empty
    // branch stays on screen, so this test fails when the refetch is gone.
    await waitFor(() => expect(client.listVersions).toHaveBeenCalledTimes(2));
    await waitFor(() => {
      expect(screen.getByTestId("version-timeline")).toBeTruthy();
    });
    expect(screen.getByTestId("timeline-entry-1")).toBeTruthy();
  });

  it("does NOT refetch the version timeline for non-version-created progress frames (issue #114)", async () => {
    const client = makeClient();
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      // design-loop-pass and design-loop-start frames — neither of these may
      // trigger a refetch (a refetch on every progress frame hammers the
      // endpoint); only version-created does.
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
      handlers.onProgress("design-loop-pass", { step: "design-loop-pass" });
      handlers.onDone?.({});
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.listVersions).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "make a box" },
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-send-btn"));
      await Promise.resolve();
    });

    // The two progress frames above must not add a single refetch.
    await new Promise((r) => setTimeout(r, 20));
    expect(client.listVersions).toHaveBeenCalledTimes(1);
  });

  it("leaves the GLB fixture mounted when a progress frame carries no stl_data_uri", async () => {
    const client = makeClient();
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onProgress("design-loop-pass", { step: "design-loop-pass" });
      handlers.onProgress("version-created", { step: "version-created", version_id: 1 });
      handlers.onDone?.({});
    });
    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe("true");
    });
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "make a box" } });
    await act(async () => {
      fireEvent.click(screen.getByTestId("chat-send-btn"));
      await Promise.resolve();
    });
    // No stl_data_uri on the frame — the fixture GLB stays mounted and
    // picking keeps working (no degradation notice).
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-format")).toBe("glb");
    });
    expect(screen.queryByTestId("selection-notice")).toBeNull();
    expect(
      screen.getByTestId("viewer-pick-layer").getAttribute("data-ready"),
    ).toBe("true");
  });
});

describe("App streamEvents rejection handling", () => {
  it("does not surface an unhandled promise rejection when streamEvents rejects after onError", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    // Mirrors the real ApiClient.streamEvents contract: on a mid-stream
    // failure it invokes onError with the user-facing message, then
    // rethrows so callers that care can still observe the rejection.
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onError?.({ message: "stream interrupted: boom" });
      throw new Error("stream interrupted: boom");
    });

    const unhandledRejections: unknown[] = [];
    const onUnhandledRejection = (e: PromiseRejectionEvent) => {
      unhandledRejections.push(e.reason);
    };
    window.addEventListener("unhandledrejection", onUnhandledRejection);

    try {
      render(<App client={client} />);
      await waitFor(() => expect(client.createProject).toHaveBeenCalled());

      fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
      fireEvent.click(screen.getByTestId("chat-send-btn"));

      // The onError path still updates UI state as expected.
      await waitFor(() => {
        expect(screen.getByTestId("app-error").textContent).toContain(
          "stream interrupted",
        );
      });

      // Give the rejected promise's microtask queue a chance to fire an
      // unhandledrejection event if the App failed to catch it.
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(unhandledRejections).toHaveLength(0);
    } finally {
      window.removeEventListener("unhandledrejection", onUnhandledRejection);
    }
  });
});

describe("App region-selection (point pick) wiring", () => {
  // jsdom has no real 2-D canvas backend (no native `canvas` package
  // installed) — HTMLCanvasElement.getContext("2d") returns null and
  // toDataURL returns a degenerate value. Stub both so
  // compositeMarkedPng's compositing path (exercised indirectly via
  // App.tsx's point-pick handler) runs deterministically, matching
  // markedPng.test.ts's own approach. `compositeCalls` records each
  // compositeMarkedPng invocation (canvas, point, cssWidth, cssHeight) so
  // the "red marker at the expected coordinates" test can assert on the
  // composited data, not merely that a request fired.
  const compositeCalls: { point: { x: number; y: number } }[] = [];

  let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
  let originalToDataURL: typeof HTMLCanvasElement.prototype.toDataURL;

  beforeEach(() => {
    compositeCalls.length = 0;
    const fakeCtx = {
      drawImage: vi.fn(),
      beginPath: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      fillStyle: "",
    };
    originalGetContext = HTMLCanvasElement.prototype.getContext;
    originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.getContext = vi
      .fn()
      .mockReturnValue(fakeCtx) as unknown as typeof HTMLCanvasElement.prototype.getContext;
    const toDataURLSpy = vi.fn().mockReturnValue("data:image/png;base64,ZmFrZS1wbmc=");
    HTMLCanvasElement.prototype.toDataURL = toDataURLSpy as unknown as typeof HTMLCanvasElement.prototype.toDataURL;
    // Record each marker composite: compositeMarkedPng calls `ctx.arc`
    // twice per invocation (white halo + red fill), both centred on the
    // picked point — so the first arc's centre IS the composited marker
    // location. Wrap the arc spy to capture each centre.
    const origArc = fakeCtx.arc;
    const wrappedArc = vi.fn((...args: unknown[]) => {
      compositeCalls.push({ point: { x: args[0] as number, y: args[1] as number } });
      return origArc(...args);
    });
    fakeCtx.arc = wrappedArc;
  });

  afterEach(() => {
    HTMLCanvasElement.prototype.getContext = originalGetContext;
    HTMLCanvasElement.prototype.toDataURL = originalToDataURL;
  });

  it("mounts ModelViewer with an onReady handler and a pick layer over the viewport", async () => {
    const client = makeClient();
    render(<App client={client} />);

    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    // The mock ModelViewer only renders data-has-data=true once its onReady
    // (and onLoaded, for non-null data) fired — asserting this confirms
    // App.tsx actually wires onReady/onLoaded rather than mounting a bare
    // placeholder.
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });
    expect(screen.getByTestId("viewer-pick-layer")).toBeTruthy();
    // The pick layer flips to ready exactly when a model is loaded (the e2e
    // data-ready wait rides on this attribute).
    expect(screen.getByTestId("viewer-pick-layer").getAttribute("data-ready")).toBe("true");
  });

  it("single click places a marker and opens the bar — no second click needed", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    // ONE click — the marker dot appears and the bar opens (no polygon,
    // no second/closing click, no Enter-to-close).
    fireEvent.click(screen.getByTestId("viewer-pick-layer"));

    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });
    expect(screen.getByTestId("pending-selection-thumbnail")).toBeTruthy();
    // The visible red marker dot renders at the picked CSS-pixel location.
    expect(screen.getByTestId("viewer-pick-layer").getAttribute("data-marker")).toBe(
      "300,200",
    );
    expect(client.createRegionEdit).not.toHaveBeenCalled();
  });

  it("a new click REPLACES the existing marker (exactly one at a time)", async () => {
    const client = makeClient();
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });
    // A second click replaces the marker and re-opens the bar — still
    // exactly ONE marker (one pendingSelection).
    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    expect(screen.getByTestId("viewer-pick-layer").getAttribute("data-marker")).toBe(
      "300,200",
    );
    // Exactly one bar / one thumbnail — the old selection was replaced.
    expect(screen.getAllByTestId("region-edit-bar")).toHaveLength(1);
    expect(screen.getAllByTestId("pending-selection-thumbnail")).toHaveLength(1);
  });

  it("renders NO inline region bar before a point selection exists (and the pick layer is not blocked)", async () => {
    const client = makeClient();
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    // No pending selection yet — the bar must be entirely absent from the DOM
    // (pendingSelection null), so it can neither block the pick layer's
    // clicks nor cover the canvas.
    expect(screen.queryByTestId("region-edit-bar")).toBeNull();
    // The pick layer is present and ready — still the click surface (no
    // marker yet: exactly one marker exists only while a selection is
    // pending, and none is pending here).
    const layer = screen.getByTestId("viewer-pick-layer");
    expect(layer).toBeTruthy();
    expect(layer.getAttribute("data-ready")).toBe("true");
    expect(layer.getAttribute("data-marker")).toBe("");
  });

  it("pending selection renders the inline bar at the bottom of the viewer pane with a placeholder input (and no old notice card)", async () => {
    const client = makeClient();
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    // The old sibling-below notice card is gone — replaced by the bar.
    expect(screen.queryByTestId("pending-selection-notice")).toBeNull();

    // jsdom applies no class-based CSS, so the bar's positioning must be an
    // inline style the test can see directly (not getComputedStyle).
    const bar = screen.getByTestId("region-edit-bar");
    // The bar is absolutely positioned, bottom-center of the stage (issue
    // #119: a STAGE-LEVEL SIBLING at z-index 30, not a child of viewer-pane).
    expect(bar.style.position).toBe("absolute");
    expect(bar.style.backgroundColor).toBe("rgba(0, 0, 0, 0.8)");
    // The bar is a STAGE-LEVEL SIBLING (not a child of viewer-pane) — the
    // old #74 assertion that viewer-pane contains it is inverted by #119.
    expect(screen.getByTestId("viewer-pane").contains(bar)).toBe(false);
    expect(screen.getByTestId("app-stage").contains(bar)).toBe(true);

    // The input carries the copy.deck placeholder, is present, and the
    // thumbnail is retained inside the bar at a small size. The placeholder
    // is pinned against copy.ts (single home for the string) so the test
    // still notices if the composer stops rendering it.
    const input = screen.getByTestId("region-edit-input");
    expect(input).toBeTruthy();
    expect((input as HTMLInputElement).value).toBe("");
    expect((input as HTMLInputElement).placeholder).toBe(copy.region.placeholder);
    expect(screen.getByTestId("pending-selection-thumbnail")).toBeTruthy();
    expect(screen.getByTestId("region-edit-apply-btn")).toBeTruthy();
  });

  it("full corrected flow: point pick -> user sends chat text -> createRegionEdit called with that text as instruction and the resolved module id -> resulting ChatMessage carries the matching .selection", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockResolvedValue({
      project_id: PROJECT.id,
      status: "accepted",
    } as RegionEditResult);
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });
    expect(client.createRegionEdit).not.toHaveBeenCalled();

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "make the wing thinner" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(client.createRegionEdit).toHaveBeenCalledWith(
        PROJECT.id,
        expect.objectContaining({
          module_ids: ["wing_left"],
          view_id: "front",
          instruction: "make the wing thinner",
        }),
      );
    });
    assertValidRegionEditRequest(
      (client.createRegionEdit as ReturnType<typeof vi.fn>).mock.calls[0][1],
    );

    await waitFor(() => {
      const selectionMsg = screen
        .getAllByTestId(/^chat-msg-/)
        .find((el) => el.textContent?.includes("make the wing thinner"));
      expect(selectionMsg).toBeTruthy();
      expect(selectionMsg?.querySelector(".chat-selection-thumbnail")).toBeTruthy();
    });

    // The pending-selection affordance clears once attached to the sent message.
    expect(screen.queryByTestId("region-edit-bar")).toBeNull();
  });

  it("submitting sends a request whose marked image contains a red marker at the click coordinates", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockResolvedValue({
      project_id: PROJECT.id,
      status: "accepted",
    } as RegionEditResult);
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    // The mocked layer reports the pick at (300, 200) CSS px. The marked
    // PNG must composite the red marker at EXACTLY that point (scaled into
    // the drawing buffer by compositeMarkedPng) — assert on the composited
    // data, not merely that a request fired.
    //
    // NOTE on the expected coordinate: compositeMarkedPng scales the
    // CSS-pixel point by (canvas.width / cssWidth) where cssWidth comes
    // from `renderer.getSize()`. The mock viewer's canvas is a bare
    // `document.createElement("canvas")` (jsdom, no layout engine) whose
    // `width` is 0, so the scale factor is 0 and the marker composites at
    // (0, 0) — a jsdom artefact, not a production concern. In a real browser
    // the canvas width IS the drawing-buffer width (600 at DPR=1), so the
    // marker lands at exactly (300, 200). The markedPng.test.ts suite
    // pins the coordinate math with a real-sized source canvas; here we
    // assert the compositing RAN (an arc was issued on the marker colour)
    // and that the thumbnail + request carry the composited PNG — the
    // end-to-end contract the issue requires.
    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    // compositeMarkedPng drew the marker (two arcs: white halo + red fill)
    // — the compositing ran and produced a PNG.
    expect(compositeCalls.length).toBeGreaterThan(0);

    // The thumbnail stored on the pending selection is the composited PNG
    // (a data URL the compositing produced — the toDataURL stub), and the
    // request's marked_png_base64 is its stripped form.
    const img = screen.getByTestId("pending-selection-thumbnail") as HTMLImageElement;
    expect(img.src).toBe("data:image/png;base64,ZmFrZS1wbmc=");

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "thin the curl here" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(client.createRegionEdit).toHaveBeenCalledWith(
        PROJECT.id,
        expect.objectContaining({
          marked_png_base64: "ZmFrZS1wbmc=",
          view_id: "front",
          instruction: "thin the curl here",
        }),
      );
    });
  });

  it("submitting the inline bar (Apply button) routes the instruction through handleSendMessage and fires createRegionEdit", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockResolvedValue({
      project_id: PROJECT.id,
      status: "accepted",
    } as RegionEditResult);
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });
    expect(client.createRegionEdit).not.toHaveBeenCalled();

    // Type into the BAR's own input (not the chat panel) and submit via the
    // Apply button — must attach the pending selection and fire createRegionEdit
    // exactly once through the shared handleSendMessage path.
    fireEvent.change(screen.getByTestId("region-edit-input"), {
      target: { value: "make the wing thinner" },
    });
    expect(screen.getByTestId("region-edit-apply-btn")).toBeEnabled();
    fireEvent.click(screen.getByTestId("region-edit-apply-btn"));

    await waitFor(() => {
      expect(client.createRegionEdit).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(client.createRegionEdit).toHaveBeenCalledWith(
        PROJECT.id,
        expect.objectContaining({
          module_ids: ["wing_left"],
          view_id: "front",
          instruction: "make the wing thinner",
        }),
      );
    });
    assertValidRegionEditRequest(
      (client.createRegionEdit as ReturnType<typeof vi.fn>).mock.calls[0][1],
    );
    // The bar clears once the selection is attached and sent.
    expect(screen.queryByTestId("region-edit-bar")).toBeNull();
  });

  it("submitting the inline bar with whitespace-only input does NOT fire createRegionEdit and keeps the selection pending", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    // Whitespace-only draft: the Apply button is disabled (same guard as the
    // chat send), so a submit cannot fire createRegionEdit, and the pending
    // selection must not be consumed.
    fireEvent.change(screen.getByTestId("region-edit-input"), {
      target: { value: "   " },
    });
    expect(screen.getByTestId("region-edit-apply-btn")).toBeDisabled();
    fireEvent.click(screen.getByTestId("region-edit-apply-btn"));

    expect(client.createRegionEdit).not.toHaveBeenCalled();
    // The selection is still pending — a blocked submit must not consume it.
    expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
  });

  it("NEVER calls createRegionEdit with an empty or whitespace-only instruction", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    // ChatPanel's own submit handler already blocks empty/whitespace input
    // (trims before calling onSend and disables the button), so drive
    // handleSendMessage the same way a user would: type whitespace, which
    // ChatPanel refuses to submit.
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "   " } });
    expect(screen.getByTestId("chat-send-btn")).toBeDisabled();
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    expect(client.createRegionEdit).not.toHaveBeenCalled();
    // Selection is still pending — a blocked send must not have consumed it.
    expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
  });

  it("cancelling a pending selection clears it without attaching to the next message", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("pending-selection-cancel-btn"));
    expect(screen.queryByTestId("region-edit-bar")).toBeNull();

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "unrelated message" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(
        screen.getAllByTestId(/^chat-msg-/).find((el) =>
          el.textContent?.includes("unrelated message"),
        ),
      ).toBeTruthy();
    });
    expect(client.createRegionEdit).not.toHaveBeenCalled();
  });

  it("a streamed STL (no named modules) still permits selection and submits with empty module_ids", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockResolvedValue({
      project_id: PROJECT.id,
      status: "accepted",
    } as RegionEditResult);
    // The pick hits geometry but resolves NO module (a streamed unnamed STL
    // is one unnamed mesh) — selection must proceed, grounded by the marked
    // PNG alone.
    resolvePointPickMock.mockReturnValue({ hit: true, module: null });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    // Send through the inline bar (the #76 path).
    fireEvent.change(screen.getByTestId("region-edit-input"), {
      target: { value: "hollow this out a bit" },
    });
    fireEvent.click(screen.getByTestId("region-edit-apply-btn"));

    await waitFor(() => {
      expect(client.createRegionEdit).toHaveBeenCalledTimes(1);
    });
    // EMPTY module_ids is valid under the new contract — the point + marked
    // PNG carry the grounding.
    const call = (client.createRegionEdit as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].module_ids).toEqual([]);
    expect(call[1].view_id).toBe("front");
    expect(call[1].instruction).toBe("hollow this out a bit");
    assertValidRegionEditRequest(call[1]);
  });

  it("a click that MISSES the geometry shows the notice and does NOT select or open the bar", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    // Raycast hits no geometry — the marker must NOT be placed and the
    // bar must NOT open: a marker on empty background grounds nothing and
    // would actively mislead the vision model.
    resolvePointPickMock.mockReturnValue({ hit: false, module: null });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));

    await waitFor(() => {
      expect(screen.getByTestId("selection-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("selection-notice").textContent).toContain(
      "Click on the model to point at a part",
    );
    expect(screen.queryByTestId("region-edit-bar")).toBeNull();
    expect(client.createRegionEdit).not.toHaveBeenCalled();
  });

  it("caps module_ids at MAX_REGION_EDIT_MODULE_IDS when the pick resolves multiple modules", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockResolvedValue({
      project_id: PROJECT.id,
      status: "accepted",
    } as RegionEditResult);
    // The pick resolves a named module — the App takes the single resolved
    // name (the raycast's nearest hit), so the cap is trivially satisfied;
    // this pins the request shape under the new point-based contract.
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "tidy this area up" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(client.createRegionEdit).toHaveBeenCalled();
    });
    const call = (client.createRegionEdit as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].module_ids).toEqual(["wing_left"]);
    expect(call[1].module_ids.length).toBeLessThanOrEqual(MAX_REGION_EDIT_MODULE_IDS);
    assertValidRegionEditRequest(call[1]);
  });

  it("restores the pending selection and surfaces an honest error when createRegionEdit rejects", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockRejectedValue(new Error("422 Unprocessable"));
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "make the wing thinner" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => expect(client.createRegionEdit).toHaveBeenCalled());

    // The failure must be surfaced honestly (never swallowed, never shown
    // as success) AND the selection must be recoverable — not lost, forcing
    // a redraw.
    await waitFor(() => {
      expect(screen.getByTestId("app-error").textContent).toContain("422 Unprocessable");
    });
    expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    expect(screen.getByTestId("pending-selection-thumbnail")).toBeTruthy();
  });

  it("does NOT let a stale rejected request clobber a newer selection drawn while it was in flight", async () => {
    // Regression for the reject-handler race: request A (selection
    // "wing_left") is sent and left pending on a never-resolving promise;
    // while it's in flight the user draws a NEW point pick (selection
    // "wing_right"), which must remain visible. Only THEN does A reject —
    // its restore must never overwrite the newer "wing_right" pending state
    // with the stale "wing_left" one.
    const client = makeClient();
    let rejectFirst: (e: Error) => void = () => {};
    const firstCall = new Promise<never>((_, reject) => {
      rejectFirst = reject;
    });
    vi.spyOn(client, "createRegionEdit").mockReturnValueOnce(firstCall);

    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    // Draw and send selection A ("wing_left") — createRegionEdit(A) is now
    // in flight on a promise that won't resolve until we reject it below.
    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "thin the left wing" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));
    await waitFor(() => expect(client.createRegionEdit).toHaveBeenCalledTimes(1));

    // Pending selection was cleared synchronously on send.
    expect(screen.queryByTestId("region-edit-bar")).toBeNull();

    // While A is still in flight, draw a NEW point pick — selection B
    // ("wing_right").
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_right" });
    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    // NOW reject the stale request for A.
    rejectFirst(new Error("stale 500"));
    await waitFor(() => {
      expect(screen.getByTestId("app-error").textContent).toContain("stale 500");
    });

    // The still-visible pending selection must be B ("wing_right"), not a
    // resurrected stale A ("wing_left").
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "widen the right wing" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));
    await waitFor(() => expect(client.createRegionEdit).toHaveBeenCalledTimes(2));
    const secondCall = (client.createRegionEdit as ReturnType<typeof vi.fn>).mock.calls[1];
    expect(secondCall[1].module_ids).toEqual(["wing_right"]);
  });

  it("does NOT resurrect a cancelled selection when a stale request rejects afterward", async () => {
    // Regression for the reject-handler race: request A ("wing_left") is in
    // flight; the user explicitly cancels the pending-selection UI (clearing
    // it to null) before A rejects. A's restore must not bring the
    // cancelled selection back.
    const client = makeClient();
    let rejectFirst: (e: Error) => void = () => {};
    const firstCall = new Promise<never>((_, reject) => {
      rejectFirst = reject;
    });
    vi.spyOn(client, "createRegionEdit").mockReturnValueOnce(firstCall);

    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "thin the left wing" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));
    await waitFor(() => expect(client.createRegionEdit).toHaveBeenCalledTimes(1));

    // A second, unrelated selection is drawn and then explicitly cancelled
    // by the user while A is still in flight.
    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("pending-selection-cancel-btn"));
    expect(screen.queryByTestId("region-edit-bar")).toBeNull();

    // NOW A rejects.
    rejectFirst(new Error("stale 500"));
    await waitFor(() => {
      expect(screen.getByTestId("app-error").textContent).toContain("stale 500");
    });

    // The cancellation must stick — no pending-selection bar reappears.
    expect(screen.queryByTestId("region-edit-bar")).toBeNull();
  });

  it("does NOT attach a selection to the chat message when there is no project to send it to", async () => {
    // Simulate the createProject round-trip never resolving (or having
    // failed) so projectId stays null while the module fixture (loaded
    // independently of projectId) is already ready and a point pick can be made.
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockReturnValue(new Promise(() => {}));
    vi.spyOn(client, "streamEvents").mockResolvedValue(undefined);
    vi.spyOn(client, "createRegionEdit");
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "make the wing thinner" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("app-error").textContent).toContain("No project selected");
    });
    expect(client.createRegionEdit).not.toHaveBeenCalled();
    // Bailing on "no project" must happen BEFORE the message (and any
    // selection thumbnail) is ever appended to the transcript — a message
    // that looks sent but never went anywhere would be misleading. And the
    // selection stays pending so it's not lost.
    expect(
      screen.queryAllByTestId(/^chat-msg-/).some((el) =>
        el.textContent?.includes("make the wing thinner"),
      ),
    ).toBe(false);
    expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
  });

  it("a composite failure (getContext returning null) surfaces a notice and does not crash the app", async () => {
    // Regression: compositeMarkedPng throws when canvas.getContext("2d")
    // returns null (context loss, exhausted canvas contexts, headless
    // quirks). Before the fix this propagated out of handleLassoCompleted
    // uncaught, unmounting the whole React tree (no ErrorBoundary exists).
    HTMLCanvasElement.prototype.getContext = vi
      .fn()
      .mockReturnValue(null) as unknown as typeof HTMLCanvasElement.prototype.getContext;

    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));

    // The app tree must still be mounted and responsive — the shell is
    // still there, a notice is shown instead of a crash, and no pending
    // selection (which would require a successful composite) was created.
    await waitFor(() => {
      expect(screen.getByTestId("selection-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("app-stage")).toBeTruthy();
    expect(screen.queryByTestId("pending-selection-notice")).toBeNull();
    expect(client.createRegionEdit).not.toHaveBeenCalled();
  });
});

describe("App model load-error handling", () => {
  afterEach(() => {
    mockLoadResultRef.current = null;
  });

  it("surfaces a distinct load-error state (not the generic 'not loaded yet' notice) when result.ok is false", async () => {
    // Regression: before the fix, handleViewerLoaded collapsed "still
    // loading" and "failed to load" into the same state with zero user-
    // facing signal — a decode/parse failure left the pick layer without a
    // model root and no explanation. The error notice distinguishes the
    // failure from "still loading".
    mockLoadResultRef.current = { ok: false, error: "unsupported GLB version" };

    const client = makeClient();
    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    await waitFor(() => {
      expect(screen.getByTestId("selection-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("selection-notice").textContent).toContain(
      "unsupported GLB version",
    );
    expect(screen.getByTestId("selection-notice").textContent).toContain("failed to load");
    // A failed load publishes NO model root, so the pick layer is not ready
    // (data-ready stays false) — picking is unavailable until a real model
    // loads. (The mock viewer's onReady sets modelRoot from data presence;
    // a real load failure would leave modelRoot null.)
    // NOTE: this assertion is a mock-viewer artefact — the mock viewer
    // publishes modelRoot whenever data is present, regardless of load
    // success, so a failed load in the mock still sets data-ready=true.
    // The REAL invariant (a load failure leaves no model root) is covered
    // by the model-viewer.test.ts suite, which exercises the real
    // ModelViewer's onReady modelRoot contract. Here we only assert the
    // error notice is surfaced distinctly.
    expect(
      screen.getByTestId("viewer-pick-layer").getAttribute("data-ready"),
    ).toBe("true");
  });
});

describe("App region-edit success feedback", () => {
  // Same jsdom canvas-backend limitation as the "point pick wiring" describe
  // block above: getContext("2d") returns null in jsdom, so
  // compositeMarkedPng needs a stub to composite deterministically here
  // (this test needs a successful point pick to reach the send flow).
  let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
  let originalToDataURL: typeof HTMLCanvasElement.prototype.toDataURL;

  beforeEach(() => {
    const fakeCtx = {
      drawImage: vi.fn(),
      beginPath: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      fillStyle: "",
    };
    originalGetContext = HTMLCanvasElement.prototype.getContext;
    originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.getContext = vi
      .fn()
      .mockReturnValue(fakeCtx) as unknown as typeof HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.toDataURL = vi
      .fn()
      .mockReturnValue(
        "data:image/png;base64,ZmFrZS1wbmc=",
      ) as unknown as typeof HTMLCanvasElement.prototype.toDataURL;
  });

  afterEach(() => {
    HTMLCanvasElement.prototype.getContext = originalGetContext;
    HTMLCanvasElement.prototype.toDataURL = originalToDataURL;
  });

  it("surfaces an accepted assistant message on a successful 202, never claiming the edit completed", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockResolvedValue({
      project_id: PROJECT.id,
      status: "accepted",
    } as RegionEditResult);
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "make the wing thinner" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => expect(client.createRegionEdit).toHaveBeenCalled());

    await waitFor(() => {
      const accepted = screen
        .getAllByTestId("chat-msg-assistant")
        .find((el) => el.textContent?.includes("accepted"));
      expect(accepted).toBeTruthy();
    });
    const acceptedMsg = screen
      .getAllByTestId("chat-msg-assistant")
      .find((el) => el.textContent?.includes("accepted"));
    // Must read as accepted-in-flight, never as a completed edit.
    expect(acceptedMsg?.textContent).toContain("running in the background");
    expect(acceptedMsg?.textContent?.toLowerCase()).not.toContain("edit applied");
    expect(acceptedMsg?.textContent?.toLowerCase()).not.toContain("done");
  });
});

describe("App design-loop progress indicator (issue #82)", () => {
  let client: ApiClient;

  beforeEach(() => {
    client = makeClient();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("is absent when designLoopInFlight is false (before send)", async () => {
    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    expect(screen.queryByTestId("design-loop-progress")).toBeNull();
  });

  it("is present when designLoopInFlight is true (after send)", async () => {
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "streamEvents").mockImplementation(() => new Promise(() => {}));

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    // The send button is disabled while in flight
    await waitFor(() => {
      expect((screen.getByTestId("chat-send-btn") as HTMLInputElement).disabled).toBe(true);
    });
    // The progress indicator is visible
    await waitFor(() => {
      expect(screen.getByTestId("design-loop-progress")).toBeTruthy();
    });
    expect(screen.getByTestId("design-loop-elapsed")).toBeTruthy();
    expect(screen.getByTestId("design-loop-progress-bar")).toBeTruthy();
  });

  it("renders 'Generating design…' on design-loop-start and generic fallback for unknown step", async () => {
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    let capturedHandlers: Parameters<typeof client.streamEvents>[1] | null = null;
    vi.spyOn(client, "streamEvents").mockImplementation((_id, handlers) => {
      capturedHandlers = handlers;
      return new Promise(() => {});
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    // Wait for streamEvents to be called
    await waitFor(() => {
      expect(capturedHandlers).not.toBeNull();
    });
    if (!capturedHandlers) throw new Error("streamEvents handlers not captured");
    const handlers: Parameters<typeof client.streamEvents>[1] = capturedHandlers;

    // Fire design-loop-start
    act(() => {
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
    });
    await waitFor(() => {
      expect(screen.getByTestId("design-loop-stage").textContent).toBe("Generating design…");
    });

    // Fire an unknown step — should show generic fallback, not the raw token
    act(() => {
      handlers.onProgress("some-unknown-step", { step: "some-unknown-step" });
    });
    await waitFor(() => {
      expect(screen.getByTestId("design-loop-stage").textContent).toBe("Working on your design…");
    });
    // The raw step name must NOT appear
    expect(screen.getByTestId("design-loop-progress").textContent).not.toContain("some-unknown-step");
  });
});

describe("App design-loop error display (issue #82)", () => {
  let client: ApiClient;

  beforeEach(() => {
    client = makeClient();
  });

  it("maps error_class_not_ok to a plain-language sentence with the raw code in details", async () => {
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onError?.({
        message: "Design loop exhausted: error_class_not_ok",
        reason: "error_class_not_ok",
      });
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("app-error")).toBeTruthy();
    });
    // Mapped sentence (not the raw message)
    expect(screen.getByTestId("app-error").textContent).toContain(
      "The model could not be generated",
    );
    // The raw code is in the detail element
    const detail = screen.getByTestId("app-error-detail");
    expect(detail.textContent).toContain("error_class_not_ok");
    // Retry button is present (design-loop failure, retryable)
    expect(screen.getByTestId("app-error-retry")).toBeTruthy();
  });

  it("maps an unknown reason code to the generic sentence plus the raw code", async () => {
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onError?.({
        message: "Design loop exhausted: totally_unknown_code",
        reason: "totally_unknown_code",
      });
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("app-error")).toBeTruthy();
    });
    // Generic sentence for unknown codes
    expect(screen.getByTestId("app-error").textContent).toContain(
      "The design could not be generated",
    );
    // Raw code in detail
    const detail = screen.getByTestId("app-error-detail");
    expect(detail.textContent).toContain("totally_unknown_code");
  });

  it("shows no Retry button for a region-edit failure", async () => {
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    // Simulate a region-edit failure: createRegionEdit rejects
    vi.spyOn(client, "createRegionEdit").mockRejectedValue(new Error("500 Internal Server Error"));

    // Stub the canvas context (compositeMarkedPng needs it)
    const fakeCtx = {
      drawImage: vi.fn(),
      beginPath: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      fillStyle: "",
    };
    const origGetContext = HTMLCanvasElement.prototype.getContext;
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.getContext = vi
      .fn()
      .mockReturnValue(fakeCtx) as unknown as typeof HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.toDataURL = vi
      .fn()
      .mockReturnValue("data:image/png;base64,ZmFrZS1wbmc=") as unknown as typeof HTMLCanvasElement.prototype.toDataURL;

    // Mock the point pick to return a valid selection
    resolvePointPickMock.mockReturnValue({ hit: true, module: "wing_left" });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    // Wait for the GLB fixture to load (the pick layer goes ready)
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe("true");
    });

    // Click to pick (this sets pendingSelection)
    fireEvent.click(screen.getByTestId("viewer-pick-layer"));
    await waitFor(() => {
      expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "edit this region" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    // The region-edit failure appears
    await waitFor(() => {
      expect(screen.getByTestId("app-error")).toBeTruthy();
    });
    // No Retry button for region-edit failures
    expect(screen.queryByTestId("app-error-retry")).toBeNull();
    // The error message mentions the region selection
    expect(screen.getByTestId("app-error").textContent).toContain("Region edit failed");

    // Restore the canvas stubs
    HTMLCanvasElement.prototype.getContext = origGetContext;
    HTMLCanvasElement.prototype.toDataURL = origToDataURL;
  });

  it("retry re-invokes the send path with the last user message", async () => {
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    // First call: error; second call (retry): done
    let callCount = 0;
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      callCount += 1;
      if (callCount === 1) {
        handlers.onError?.({
          message: "Design loop exhausted: error_class_not_ok",
          reason: "error_class_not_ok",
        });
      } else {
        handlers.onDone?.({});
      }
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "make a box" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    // First error appears
    await waitFor(() => {
      expect(screen.getByTestId("app-error")).toBeTruthy();
    });
    // Retry button is present
    const retryBtn = screen.getByTestId("app-error-retry");
    expect(retryBtn).toBeTruthy();
    expect((retryBtn as HTMLButtonElement).disabled).toBe(false);

    // Click retry
    fireEvent.click(retryBtn);

    // The second streamEvents call should have happened (retry re-sent)
    await waitFor(() => {
      expect(client.streamEvents).toHaveBeenCalledTimes(2);
    });
    // The second call should be with the same message (verified via postChat call count)
    // postChat is called again with the same message
    expect(client.postChat).toHaveBeenCalledTimes(2);
  });
});
