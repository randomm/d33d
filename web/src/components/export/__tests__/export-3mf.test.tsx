/**
 * Export3MF tests — download-trigger UI + API-client plumbing.
 *
 * Scope note: there is no backend 3MF-generation endpoint yet (see the
 * component's doc comment). These tests exercise the client-side plumbing
 * against an injected ApiClient/fetch fake — they do not depend on (or
 * assert) real backend behaviour.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { Export3MF } from "../Export3MF";
import { ApiClient, ApiError } from "../../../lib/api";

const projectId = 42;

describe("Export3MF", () => {
  let createObjectURLSpy: ReturnType<typeof vi.fn>;
  let revokeObjectURLSpy: ReturnType<typeof vi.fn>;
  let clickSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // jsdom does not implement URL.createObjectURL/revokeObjectURL — stub
    // them as plain functions (not spyOn, which requires the property to
    // already exist on the object).
    URL.createObjectURL = vi.fn().mockReturnValue("blob:mock-url");
    URL.revokeObjectURL = vi.fn();
    createObjectURLSpy = vi.mocked(URL.createObjectURL);
    revokeObjectURLSpy = vi.mocked(URL.revokeObjectURL);
    clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the export button in idle state", () => {
    const client = new ApiClient();
    render(<Export3MF projectId={projectId} client={client} />);
    expect(screen.getByTestId("export-3mf")).toBeTruthy();
    const button = screen.getByTestId("export-3mf-button");
    expect(button.textContent).toBe("Export 3MF");
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });

  it("downloads the 3MF blob and triggers a browser download on click", async () => {
    const blob = new Blob(["fake 3mf bytes"], { type: "model/3mf" });
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockResolvedValue(blob);

    render(
      <Export3MF projectId={projectId} client={client} fileName="widget.3mf" />,
    );
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(client.downloadModel3MF).toHaveBeenCalledWith(projectId);
    });
    await waitFor(() => {
      expect(createObjectURLSpy).toHaveBeenCalledWith(blob);
    });
    expect(clickSpy).toHaveBeenCalled();
    expect(revokeObjectURLSpy).toHaveBeenCalledWith("blob:mock-url");
  });

  it("shows a disabled 'Exporting…' state while the download is in flight", async () => {
    const client = new ApiClient();
    let resolveDownload: (blob: Blob) => void = () => {};
    vi.spyOn(client, "downloadModel3MF").mockReturnValue(
      new Promise((resolve) => {
        resolveDownload = resolve;
      }),
    );

    render(<Export3MF projectId={projectId} client={client} />);
    const button = screen.getByTestId(
      "export-3mf-button",
    ) as HTMLButtonElement;
    fireEvent.click(button);

    await waitFor(() => {
      expect(button.textContent).toBe("Exporting…");
    });
    expect(button.disabled).toBe(true);

    resolveDownload(new Blob(["x"]));
    await waitFor(() => {
      expect(button.textContent).toBe("Export 3MF");
    });
  });

  it("surfaces a 404 (no backend route yet) as an error state, not a crash", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockRejectedValue(
      new ApiError(404, "Not Found"),
    );

    render(<Export3MF projectId={projectId} client={client} />);
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(screen.getByTestId("export-3mf-error")).toBeTruthy();
    });
    expect(screen.getByTestId("export-3mf-error").textContent).toContain(
      "404",
    );
    expect(createObjectURLSpy).not.toHaveBeenCalled();
  });

  it("defaults the download filename to model-{projectId}.3mf", async () => {
    const blob = new Blob(["x"], { type: "model/3mf" });
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockResolvedValue(blob);

    let capturedName = "";
    vi.spyOn(HTMLAnchorElement.prototype, "download", "set").mockImplementation(
      function (this: HTMLAnchorElement, value: string) {
        capturedName = value;
      },
    );

    render(<Export3MF projectId={projectId} client={client} />);
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(capturedName).toBe(`model-${projectId}.3mf`);
    });
  });

  it("uses a default same-origin ApiClient when none is injected", () => {
    // No `client` prop passed — the component must not throw at render time.
    render(<Export3MF projectId={projectId} />);
    expect(screen.getByTestId("export-3mf")).toBeTruthy();
  });
});
