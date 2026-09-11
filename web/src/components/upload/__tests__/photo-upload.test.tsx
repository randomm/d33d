/**
 * PhotoUpload tests — file validation, upload flow, error handling.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { PhotoUpload } from "../PhotoUpload";

// Mock fetch for upload tests
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

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
      expect(onUploaded).toHaveBeenCalledWith("/repo/photos/test.png");
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
});
