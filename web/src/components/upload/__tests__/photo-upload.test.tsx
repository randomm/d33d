/**
 * PhotoUpload tests — file validation, upload flow, error handling.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { PhotoUpload } from "../PhotoUpload";

// Mock fetch for upload tests
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

// jsdom's Image doesn't decode real pixels from a fake blob URL, so it never
// fires onload with meaningful naturalWidth/naturalHeight. Stub the global
// Image constructor to fire onload synchronously-ish with fixed test
// dimensions, matching the "read natural dimensions client-side" contract
// PhotoUpload relies on.
const TEST_IMAGE_WIDTH = 1200;
const TEST_IMAGE_HEIGHT = 900;

class FakeImage {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  naturalWidth = TEST_IMAGE_WIDTH;
  naturalHeight = TEST_IMAGE_HEIGHT;
  private _src = "";
  set src(value: string) {
    this._src = value;
    // Fire onload asynchronously, mirroring real <img> loading behavior.
    queueMicrotask(() => this.onload?.());
  }
  get src() {
    return this._src;
  }
}

vi.stubGlobal("Image", FakeImage);
if (!URL.revokeObjectURL) {
  URL.revokeObjectURL = vi.fn();
} else {
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
}

const projectId = 1;
const onUploaded = vi.fn();
const onError = vi.fn();

function makeFile(type: string, size: number, name = "test.png"): File {
  const content = new ArrayBuffer(size);
  return new File([content], name, { type });
}

describe("PhotoUpload", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the upload label and file input", () => {
    render(<PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />);
    expect(screen.getByTestId("photo-upload")).toBeTruthy();
    expect(screen.getByTestId("photo-upload-label")).toBeTruthy();
    expect(screen.getByTestId("photo-file-input")).toBeTruthy();
  });

  it("rejects unsupported file type (gif)", async () => {
    const file = makeFile("image/gif", 1000, "anim.gif");
    render(
      <PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />,
    );
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => {
      expect(onError).toHaveBeenCalledWith(
        expect.stringContaining("Unsupported file type"),
      );
    });
  });

  it("rejects oversized file (> 20 MB)", async () => {
    const file = makeFile("image/png", 21 * 1024 * 1024, "big.png");
    render(
      <PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />,
    );
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => {
      expect(onError).toHaveBeenCalledWith(
        expect.stringContaining("exceeds 20 MB"),
      );
    });
  });

  it("uploads a valid PNG file", async () => {
    const file = makeFile("image/png", 1024, "test.png");
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ source_photo_path: "/repo/photos/test.png" }),
    });
    render(
      <PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />,
    );
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
    }, { timeout: 3000 });
    await waitFor(() => {
      expect(onUploaded).toHaveBeenCalledWith(
        "/repo/photos/test.png",
        TEST_IMAGE_WIDTH,
        TEST_IMAGE_HEIGHT,
      );
    }, { timeout: 3000 });
  });

  it("shows error state on HTTP failure", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 413,
      json: async () => ({ detail: "file too large" }),
    });
    const file = makeFile("image/png", 1024, "test.png");
    render(
      <PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />,
    );
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => {
      expect(onError).toHaveBeenCalledWith("file too large");
    });
  });

  it("treats a 200 response with a missing source_photo_path as an error, not undefined propagation", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ unexpected: "shape" }),
    });
    const file = makeFile("image/png", 1024, "test.png");
    render(
      <PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />,
    );
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => {
      expect(onError).toHaveBeenCalledWith(
        expect.stringContaining("source_photo_path"),
      );
    });
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it("reports the uploaded image's real natural dimensions, not a hardcoded size", async () => {
    const file = makeFile("image/png", 1024, "test.png");
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ source_photo_path: "/repo/photos/test.png" }),
    });
    render(
      <PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />,
    );
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(onUploaded).toHaveBeenCalled();
    });

    const [, width, height] = onUploaded.mock.calls[0];
    expect(width).toBe(TEST_IMAGE_WIDTH);
    expect(height).toBe(TEST_IMAGE_HEIGHT);
    expect(width).not.toBe(800);
    expect(height).not.toBe(600);
  });

  it("treats a 200 response where source_photo_path is not a string as an error", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ source_photo_path: 12345 }),
    });
    const file = makeFile("image/png", 1024, "test.png");
    render(
      <PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />,
    );
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => {
      expect(onError).toHaveBeenCalled();
    });
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it("reports upload success (onUploaded, not onError) when the server upload succeeds but client-side dimension-read fails", async () => {
    // Simulate a FakeImage that fires onerror instead of onload for this
    // one test — the decode failure must not be conflated with an upload
    // failure: the server already has the photo stored.
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

    const file = makeFile("image/png", 1024, "test.png");
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ source_photo_path: "/repo/photos/test.png" }),
    });
    render(
      <PhotoUpload projectId={projectId} onUploaded={onUploaded} onError={onError} />,
    );
    const input = screen.getByTestId("photo-file-input");
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(onUploaded).toHaveBeenCalledWith("/repo/photos/test.png", 0, 0);
    });
    // The upload itself succeeded — no error must be reported, and the
    // component's visible state must reflect success, not failure.
    expect(onError).not.toHaveBeenCalled();
    expect(screen.queryByTestId("upload-error")).toBeNull();

    // Restore the shared FakeImage stub for subsequent tests in this file.
    vi.stubGlobal("Image", FakeImage);
  });
});
