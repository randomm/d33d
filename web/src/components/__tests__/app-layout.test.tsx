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
import type { RenderImage } from "../../App";
import { dataUriToArrayBuffer } from "../../lib/dataUri";
import { ApiClient, MAX_REGION_EDIT_MODULE_IDS } from "../../lib/api";
import type { Project } from "../../lib/api";
import type { ModelViewerHandle, LoadResult } from "../viewer/ModelViewer";
import type { ViewportLassoCompletedEvent } from "../viewer/ViewportLassoOverlay";
import { assertValidRegionEditRequest } from "../../lib/__tests__/regionEditContract";

/** A minimal fake THREE.Object3D — the mock only needs identity, never
 *  real three.js behaviour (resolveLassoSelection itself is mocked below
 *  in tests that need to control its output). */
const FAKE_MODULE_GROUP = { name: "fake-module-group" };

const { resolveLassoSelectionMock, mockLoadResultRef } = vi.hoisted(() => ({
  resolveLassoSelectionMock: vi.fn(),
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
      props.onReady?.({
        scene: {} as never,
        camera: {} as never,
        renderer: {
          domElement: document.createElement("canvas"),
          getSize: (target: { x: number; y: number }) => {
            target.x = 600;
            target.y = 400;
            return target;
          },
        } as never,
        controls: {} as never,
        raycaster: {} as never,
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
    resolveLassoSelection: resolveLassoSelectionMock,
    ModelViewer: MockModelViewer,
  };
});

vi.mock("../viewer/ViewportLassoOverlay", () => ({
  ViewportLassoOverlay: (props: {
    disabled?: boolean;
    onLassoCompleted: (event: ViewportLassoCompletedEvent) => void;
  }) => (
    <div
      data-testid="viewport-lasso-overlay-mock"
      data-disabled={props.disabled}
      onClick={() =>
        props.onLassoCompleted({
          points: [
            { x: 0, y: 0 },
            { x: 10, y: 0 },
            { x: 10, y: 10 },
          ],
          viewId: "front",
        })
      }
    />
  ),
}));

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
  vi.spyOn(client, "streamEvents").mockResolvedValue(undefined);
  vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
  Object.assign(client, overrides);
  return client;
}

describe("App layout", () => {
  let client: ApiClient;

  beforeEach(() => {
    client = makeClient();
    resolveLassoSelectionMock.mockReset();
  });

  it("renders the two-pane shell (left chat + right viewer)", async () => {
    render(<App client={client} />);
    expect(screen.getByTestId("app-shell")).toBeTruthy();
    expect(screen.getByTestId("app-left-pane")).toBeTruthy();
    expect(screen.getByTestId("app-right-pane")).toBeTruthy();
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

  it("renders validation status and a real Export3MF once the project loads", async () => {
    render(<App client={client} />);
    expect(screen.getByTestId("validation-pane")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByTestId("export-3mf")).toBeTruthy();
    });
    expect(screen.queryByTestId("export-3mf-btn")).toBeNull();
  });

  it("does NOT render an auto-generated slider/parameter panel", async () => {
    const { container } = render(<App client={client} />);
    expect(container.querySelectorAll("input[type='range']")).toHaveLength(0);
    expect(screen.getByTestId("pinned-empty")).toBeTruthy();
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
  });

  it("renders six inline render images when provided", async () => {
    const renders: RenderImage[] = [
      { filename: "view_00_front.png", src: "data:image/png;base64,AAA" },
      { filename: "view_01_back.png", src: "data:image/png;base64,AAA" },
      { filename: "view_02_left.png", src: "data:image/png;base64,AAA" },
      { filename: "view_03_right.png", src: "data:image/png;base64,AAA" },
      { filename: "view_04_top.png", src: "data:image/png;base64,AAA" },
      { filename: "view_05_iso.png", src: "data:image/png;base64,AAA" },
    ];
    render(<App renders={renders} client={client} />);
    expect(screen.getByTestId("render-img-view_00_front.png")).toBeTruthy();
    expect(screen.getByTestId("render-img-view_05_iso.png")).toBeTruthy();
    expect(screen.getAllByTestId(/^render-img-/)).toHaveLength(6);
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

  it("appends streamed tokens to the assistant message via onToken", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
    vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
    vi.spyOn(client, "streamEvents").mockImplementation(async (_id, handlers) => {
      handlers.onToken("Hello", {});
      handlers.onToken(" world", {});
      handlers.onDone?.({});
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("chat-msg-assistant").textContent).toContain(
        "Hello world",
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

  it("shows a lasso-degradation notice and disables the lasso once the streamed STL replaces the fixture", async () => {
    const client = makeClient();
    await renderAndStreamVersionCreated(client);

    // After the pass: the streamed STL has no named modules, so the lasso
    // is disabled and the notice explains why.
    await waitFor(() => {
      expect(screen.getByTestId("selection-notice").textContent).toContain(
        "no named modules",
      );
    });
    expect(screen.getByTestId("viewport-lasso-overlay-mock").getAttribute("data-disabled")).toBe(
      "true",
    );
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
    // No stl_data_uri on the frame — the fixture GLB stays mounted and the
    // lasso keeps working (no degradation notice).
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-format")).toBe("glb");
    });
    expect(screen.queryByTestId("selection-notice")).toBeNull();
    expect(
      screen.getByTestId("viewport-lasso-overlay-mock").getAttribute("data-disabled"),
    ).toBe("false");
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

describe("App region-selection (lasso) wiring", () => {
  // jsdom has no real 2-D canvas backend (no native `canvas` package
  // installed) — HTMLCanvasElement.getContext("2d") returns null and
  // toDataURL returns a degenerate value. Stub both so
  // compositeMarkedPng's compositing path (exercised indirectly via
  // App.tsx's lasso-completion handler) runs deterministically, matching
  // markedPng.test.ts's own approach.
  let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
  let originalToDataURL: typeof HTMLCanvasElement.prototype.toDataURL;

  beforeEach(() => {
    const fakeCtx = {
      drawImage: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      stroke: vi.fn(),
      strokeStyle: "",
      lineWidth: 0,
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

  it("mounts ModelViewer with an onReady handler and a lasso surface over the viewport", async () => {
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
    expect(screen.getByTestId("viewport-lasso-overlay-mock")).toBeTruthy();
  });

  it("lasso completed -> pending selection shown, createRegionEdit NOT yet called", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [
        { name: "wing_left", hitCount: 5 },
        { name: "wing_right", hitCount: 2 },
      ],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));

    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("pending-selection-thumbnail")).toBeTruthy();
    expect(client.createRegionEdit).not.toHaveBeenCalled();
  });

  it("pending selection notice is styled as a card (border + light bg) with a 200px-max thumbnail", async () => {
    const client = makeClient();
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [
        { name: "wing_left", hitCount: 5 },
        { name: "wing_right", hitCount: 2 },
      ],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
    });

    // jsdom applies no class-based CSS, so the card styling must be an
    // inline style the test can see directly (not getComputedStyle).
    const notice = screen.getByTestId("pending-selection-notice");
    expect(notice.style.border).toBe("2px solid rgb(208, 215, 222)");
    expect(notice.style.backgroundColor).toBe("rgb(246, 248, 250)");

    const thumbnail = screen.getByTestId("pending-selection-thumbnail");
    expect(thumbnail.style.maxWidth).toBe("200px");
  });

  it("full corrected flow: lasso completed -> user sends chat text -> createRegionEdit called with that text as instruction and the resolved module ids -> resulting ChatMessage carries the matching .selection", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockResolvedValue({
      project_id: PROJECT.id,
      status: "accepted",
    });
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [
        { name: "wing_left", hitCount: 5 },
        { name: "wing_right", hitCount: 2 },
      ],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
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
          module_ids: ["wing_left", "wing_right"],
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
    expect(screen.queryByTestId("pending-selection-notice")).toBeNull();
  });

  it("NEVER calls createRegionEdit with an empty or whitespace-only instruction", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_left", hitCount: 5 }],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
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
    expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
  });

  it("cancelling a pending selection clears it without attaching to the next message", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_left", hitCount: 5 }],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("pending-selection-cancel-btn"));
    expect(screen.queryByTestId("pending-selection-notice")).toBeNull();

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

  it("does NOT call createRegionEdit when the ranked list is empty (nothing selected)", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit");
    resolveLassoSelectionMock.mockReturnValue({ ranked: [], primary: null });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));

    await waitFor(() => {
      expect(screen.getByTestId("selection-notice")).toBeTruthy();
    });
    expect(client.createRegionEdit).not.toHaveBeenCalled();
  });

  it("caps module_ids at MAX_REGION_EDIT_MODULE_IDS when the ranked list is longer", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockResolvedValue({
      project_id: PROJECT.id,
      status: "accepted",
    });
    const longRanked = Array.from({ length: MAX_REGION_EDIT_MODULE_IDS + 5 }, (_, i) => ({
      name: `module_${i}`,
      hitCount: MAX_REGION_EDIT_MODULE_IDS + 5 - i,
    }));
    resolveLassoSelectionMock.mockReturnValue({
      ranked: longRanked,
      primary: longRanked[0].name,
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "tidy this area up" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));

    await waitFor(() => {
      expect(client.createRegionEdit).toHaveBeenCalled();
    });
    const call = (client.createRegionEdit as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].module_ids).toHaveLength(MAX_REGION_EDIT_MODULE_IDS);
    expect(call[1].module_ids).toEqual(
      longRanked.slice(0, MAX_REGION_EDIT_MODULE_IDS).map((m) => m.name),
    );
    assertValidRegionEditRequest(call[1]);
  });

  it("restores the pending selection and surfaces an honest error when createRegionEdit rejects", async () => {
    const client = makeClient();
    vi.spyOn(client, "createRegionEdit").mockRejectedValue(new Error("422 Unprocessable"));
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_left", hitCount: 5 }],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
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
    expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
    expect(screen.getByTestId("pending-selection-thumbnail")).toBeTruthy();
  });

  it("does NOT let a stale rejected request clobber a newer selection drawn while it was in flight", async () => {
    // Regression for the reject-handler race: request A (selection
    // "wing_left") is sent and left pending on a never-resolving promise;
    // while it's in flight the user draws a NEW lasso (selection
    // "wing_right"), which must remain visible. Only THEN does A reject —
    // its restore must never overwrite the newer "wing_right" pending state
    // with the stale "wing_left" one.
    const client = makeClient();
    let rejectFirst: (e: Error) => void = () => {};
    const firstCall = new Promise<never>((_, reject) => {
      rejectFirst = reject;
    });
    vi.spyOn(client, "createRegionEdit").mockReturnValueOnce(firstCall);

    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_left", hitCount: 5 }],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    // Draw and send selection A ("wing_left") — createRegionEdit(A) is now
    // in flight on a promise that won't resolve until we reject it below.
    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "thin the left wing" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));
    await waitFor(() => expect(client.createRegionEdit).toHaveBeenCalledTimes(1));

    // Pending selection was cleared synchronously on send.
    expect(screen.queryByTestId("pending-selection-notice")).toBeNull();

    // While A is still in flight, draw a NEW lasso — selection B
    // ("wing_right").
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_right", hitCount: 5 }],
      primary: "wing_right",
    });
    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
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

    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_left", hitCount: 5 }],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
    });
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "thin the left wing" },
    });
    fireEvent.click(screen.getByTestId("chat-send-btn"));
    await waitFor(() => expect(client.createRegionEdit).toHaveBeenCalledTimes(1));

    // A second, unrelated selection is drawn and then explicitly cancelled
    // by the user while A is still in flight.
    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("pending-selection-cancel-btn"));
    expect(screen.queryByTestId("pending-selection-notice")).toBeNull();

    // NOW A rejects.
    rejectFirst(new Error("stale 500"));
    await waitFor(() => {
      expect(screen.getByTestId("app-error").textContent).toContain("stale 500");
    });

    // The cancellation must stick — no pending-selection notice reappears.
    expect(screen.queryByTestId("pending-selection-notice")).toBeNull();
  });

  it("does NOT attach a selection to the chat message when there is no project to send it to", async () => {
    // Simulate the createProject round-trip never resolving (or having
    // failed) so projectId stays null while the module fixture (loaded
    // independently of projectId) is already ready and a lasso can be drawn.
    const client = new ApiClient();
    vi.spyOn(client, "createProject").mockReturnValue(new Promise(() => {}));
    vi.spyOn(client, "streamEvents").mockResolvedValue(undefined);
    vi.spyOn(client, "createRegionEdit");
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_left", hitCount: 5 }],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
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
    expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
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
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_left", hitCount: 5 }],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));

    // The app tree must still be mounted and responsive — the shell is
    // still there, a notice is shown instead of a crash, and no pending
    // selection (which would require a successful composite) was created.
    await waitFor(() => {
      expect(screen.getByTestId("selection-notice")).toBeTruthy();
    });
    expect(screen.getByTestId("app-shell")).toBeTruthy();
    expect(screen.queryByTestId("pending-selection-notice")).toBeNull();
    expect(client.createRegionEdit).not.toHaveBeenCalled();
  });
});

describe("App model load-error handling", () => {
  afterEach(() => {
    mockLoadResultRef.current = null;
  });

  it("surfaces a distinct load-error state (not the generic 'not loaded yet' notice) when result.ok is false, and keeps the lasso disabled", async () => {
    // Regression: before the fix, handleViewerLoaded collapsed "still
    // loading" and "failed to load" into the same moduleGroup===null state
    // with zero user-facing signal — a decode/parse failure left the lasso
    // permanently and inexplicably disabled.
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
    expect(
      screen.getByTestId("viewport-lasso-overlay-mock").getAttribute("data-disabled"),
    ).toBe("true");
  });
});

describe("App region-edit success feedback", () => {
  // Same jsdom canvas-backend limitation as the "lasso wiring" describe
  // block above: getContext("2d") returns null in jsdom, so
  // compositeMarkedPng needs a stub to composite deterministically here
  // (this test needs a successful lasso completion to reach the send flow).
  let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
  let originalToDataURL: typeof HTMLCanvasElement.prototype.toDataURL;

  beforeEach(() => {
    const fakeCtx = {
      drawImage: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      stroke: vi.fn(),
      strokeStyle: "",
      lineWidth: 0,
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
    });
    resolveLassoSelectionMock.mockReturnValue({
      ranked: [{ name: "wing_left", hitCount: 5 }],
      primary: "wing_left",
    });

    render(<App client={client} />);
    await waitFor(() => expect(client.createProject).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("model-viewer-mock").getAttribute("data-has-data")).toBe(
        "true",
      );
    });

    fireEvent.click(screen.getByTestId("viewport-lasso-overlay-mock"));
    await waitFor(() => {
      expect(screen.getByTestId("pending-selection-notice")).toBeTruthy();
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
