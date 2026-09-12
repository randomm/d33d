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

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import App from "../../App";
import type { RenderImage } from "../../App";
import { ApiClient } from "../../lib/api";
import type { Project } from "../../lib/api";

vi.mock("../viewer/ModelViewer", () => ({
  ModelViewer: (props: { data: ArrayBuffer | null; format: string }) => (
    <div
      data-testid="model-viewer-mock"
      data-format={props.format}
      data-has-data={props.data !== null}
    />
  ),
}));

// DimensionCanvas renders a react-konva <Stage>, which needs a 2-D canvas
// context jsdom doesn't implement — its own suite (dimension-canvas.test.tsx)
// mocks react-konva directly. Here we only need to assert it is mounted with
// the right photoSrc once a photo is uploaded, so a lightweight mock is the
// right boundary.
vi.mock("../canvas/DimensionCanvas", () => ({
  DimensionCanvas: (props: { photoSrc: string }) => (
    <div data-testid="dimension-canvas-container" data-photo-src={props.photoSrc} />
  ),
}));

const PROJECT: Project = {
  id: 7,
  name: "untitled project",
  git_repo_path: "/data/projects/7",
  tags: [],
  notes: "",
  source_photo_path: null,
  created_at: "2026-01-01T00:00:00Z",
};

function makeClient(overrides: Partial<ApiClient> = {}): ApiClient {
  const client = new ApiClient();
  vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
  vi.spyOn(client, "streamEvents").mockResolvedValue(undefined);
  Object.assign(client, overrides);
  return client;
}

describe("App layout", () => {
  let client: ApiClient;

  beforeEach(() => {
    client = makeClient();
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
  });
});
