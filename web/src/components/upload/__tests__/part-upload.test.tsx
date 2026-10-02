import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { PartUpload } from "../PartUpload";
import { ApiClient, ApiError } from "../../../lib/api";
import copy from "../../../copy";

/** A File with the given name. */
function makeFile(name: string): File {
  return new File([new ArrayBuffer(8)], name, { type: "model/stl" });
}

function makeClient(overrides: { uploadPart?: ReturnType<typeof vi.fn> } = {}): ApiClient {
  return {
    uploadPart: vi.fn().mockResolvedValue({ id: 7, version_id: 1, part: null }),
    ...overrides,
  } as unknown as ApiClient;
}

describe("PartUpload (issue #334, D5)", () => {
  it("picks an STL and uploads to the given project id", async () => {
    const onUploaded = vi.fn();
    const client = makeClient();
    const file = makeFile("gear.stl");
    render(
      <PartUpload
        projectId={7}
        onUploaded={onUploaded}
        onError={vi.fn()}
        client={client}
      />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() =>
      expect((client.uploadPart as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith(7, file),
    );
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(7));
  });

  it("creates the project lazily through the latch (no project) before POSTing", async () => {
    const onEnsureProject = vi.fn().mockResolvedValue(99);
    const client = makeClient();
    const file = makeFile("part.stl");
    render(
      <PartUpload
        onEnsureProject={onEnsureProject}
        onUploaded={vi.fn()}
        onError={vi.fn()}
        client={client}
      />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() => expect(onEnsureProject).toHaveBeenCalled());
    await waitFor(() =>
      expect((client.uploadPart as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith(99, file),
    );
  });

  it("surfaces a 409 detail verbatim in the blocked style (never a status code)", async () => {
    const detail = "A part already exists: 'gear.stl'";
    const client = makeClient({
      uploadPart: vi.fn().mockRejectedValue(new ApiError(409, detail)),
    });
    const onError = vi.fn();
    const file = makeFile("dup.stl");
    render(
      <PartUpload projectId={7} onUploaded={vi.fn()} onError={onError} client={client} />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() => {
      const el = screen.getByTestId("part-upload-error");
      expect(el.textContent).toBe(detail);
    });
    await waitFor(() => expect(onError).toHaveBeenCalledWith(detail, detail));
  });

  it("surfaces a 400 detail verbatim", async () => {
    const detail = "Unsupported file type";
    const client = makeClient({
      uploadPart: vi.fn().mockRejectedValue(new ApiError(400, detail)),
    });
    const file = makeFile("bad.stl");
    render(
      <PartUpload projectId={7} onUploaded={vi.fn()} onError={vi.fn()} client={client} />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() => expect(screen.getByTestId("part-upload-error").textContent).toBe(detail));
  });

  it("surfaces a 413 detail verbatim", async () => {
    const detail = "File exceeds 50MB limit";
    const client = makeClient({
      uploadPart: vi.fn().mockRejectedValue(new ApiError(413, detail)),
    });
    const file = makeFile("big.stl");
    render(
      <PartUpload projectId={7} onUploaded={vi.fn()} onError={vi.fn()} client={client} />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() => expect(screen.getByTestId("part-upload-error").textContent).toBe(detail));
  });

  it("the drop-area label renders its own copy (partUpload.dropLine, not firstRun's)", () => {
    const client = makeClient();
    render(<PartUpload projectId={7} client={client} />);
    expect(screen.getByTestId("part-upload-label").textContent).toBe(
      copy.partUpload.dropLine,
    );
  });

  it("rejects a non-STL/3MF client-side (the unsupported copy, not a POST)", async () => {
    const client = makeClient();
    const file = makeFile("photo.png");
    render(
      <PartUpload projectId={7} onUploaded={vi.fn()} onError={vi.fn()} client={client} />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() =>
      expect(screen.getByTestId("part-upload-error").textContent).toBe(copy.partUpload.unsupported),
    );
    expect(client.uploadPart).not.toHaveBeenCalled();
  });

  it("surfaces a creation failure (latch reject) — never an upload failure", async () => {
    const onEnsureProject = vi
      .fn()
      .mockRejectedValue(new Error("no permission"));
    const client = makeClient();
    const file = makeFile("part.stl");
    const onError = vi.fn();
    render(
      <PartUpload
        onEnsureProject={onEnsureProject}
        onUploaded={vi.fn()}
        onError={onError}
        client={client}
      />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() => {
      const msg = copy.shell.projectCreationFailed("no permission");
      expect(onError).toHaveBeenCalledWith(msg, msg);
    });
    expect(client.uploadPart).not.toHaveBeenCalled();
  });

  it("accepts a 3MF extension", async () => {
    const client = makeClient();
    const file = makeFile("part.3mf");
    render(
      <PartUpload projectId={7} onUploaded={vi.fn()} onError={vi.fn()} client={client} />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() =>
      expect((client.uploadPart as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith(7, file),
    );
  });
});
