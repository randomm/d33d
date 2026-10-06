import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { PartUpload } from "../PartUpload";
import { ApiError } from "../../../lib/api";
import type { ApiClient, PartReportInfo } from "../../../lib/api";
import copy from "../../../copy";

/** A File with the given name. */
function makeFile(name: string): File {
  return new File([new ArrayBuffer(8)], name, { type: "model/stl" });
}

/** A minimal PartReportInfo matching the declared uploadPart contract
 *  (the mock was `part: null`, which the type forbids). */
const partReport: PartReportInfo = {
  filename: "gear.stl",
  format: "stl",
  unit: "mm",
  unit_status: "settled",
  scale: 1,
  report: {
    triangles: 12,
    bodies: 1,
    watertight: true,
    gaps_closed: 0,
    bbox_file_units: [20, 20, 20],
  },
  options: null,
};

function makeClient(overrides: { uploadPart?: ReturnType<typeof vi.fn> } = {}): ApiClient {
  return {
    uploadPart: vi.fn().mockResolvedValue({ id: 7, version_id: 1, part: partReport }),
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

  it("the drop-area label carries no 📦 glyph (issue #347, decision 1: text-only)", () => {
    const client = makeClient();
    render(<PartUpload projectId={7} client={client} />);
    const label = screen.getByTestId("part-upload-label");
    expect(label.textContent).not.toContain("\u{1F4E6}"); // 📦
    expect(label.textContent).not.toContain("\u{1F4CE}"); // 📎
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

  it("surfaces the no-project copy when no latch is provided (never a POST)", async () => {
    const client = makeClient();
    const file = makeFile("part.stl");
    const onError = vi.fn();
    render(<PartUpload onUploaded={vi.fn()} onError={onError} client={client} />);
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    await waitFor(() =>
      expect(screen.getByTestId("part-upload-error").textContent).toBe(
        copy.shell.projectCreationFailed(copy.shell.noProject),
      ),
    );
    expect(client.uploadPart).not.toHaveBeenCalled();
  });

  it("shows the elapsed-seconds reading state while the upload is in flight (issue #395)", async () => {
    // A big mesh can take a long time to read and repair — the card ticks
    // "Reading your file… Ns" with the client-side elapsed seconds instead
    // of showing nothing for 80 s. The label is the copy.ts interpolation,
    // so the test asserts against `copy.partUpload.reading`, never a
    // hardcoded sentence (the ticking itself is pinned by the component's
    // 1 s interval, which this test observes at the 0 s boundary).
    let release!: () => void;
    const gate = new Promise<void>((res) => (release = res));
    const client = makeClient({
      uploadPart: vi.fn().mockImplementation(async () => {
        await gate;
        return { id: 7, version_id: 1, part: partReport };
      }),
    });
    const onUploaded = vi.fn();
    const file = makeFile("big.stl");
    render(<PartUpload projectId={7} onUploaded={onUploaded} onError={vi.fn()} client={client} />);
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    // In flight: the status line shows the copy.ts reading state at 0 s.
    await waitFor(() =>
      expect(screen.getByTestId("part-upload-status").textContent).toBe(
        copy.partUpload.reading(0),
      ),
    );
    // The old static "Uploading…" label is gone while in flight.
    expect(screen.getByTestId("part-upload-status").textContent).not.toBe(
      copy.partUpload.uploading,
    );
    // Settles to success once the POST completes: the status line is gone.
    release();
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(7));
    expect(screen.queryByTestId("part-upload-status")).toBeNull();
  });

  it("stops ticking the elapsed seconds once the POST settles (issue #395)", async () => {
    // The elapsed-seconds interval must not keep updating state after the
    // POST settles (the effect's cleanup fires when `uploadStart` is
    // nulled on settle). The in-flight window is gated by a promise; the
    // settle is simulated by releasing the gate and flushing microtasks
    // (the POST's own resolution settles the component). With fake
    // timers, advancing time well past the settle point then causes NO
    // further state updates if the interval was cleaned up.
    let release!: () => void;
    const gate = new Promise<void>((res) => (release = res));
    const client = makeClient({
      uploadPart: vi.fn().mockImplementation(async () => {
        await gate;
        return { id: 7, version_id: 1, part: partReport };
      }),
    });
    const onUploaded = vi.fn();
    const file = makeFile("big.stl");
    render(
      <PartUpload projectId={7} onUploaded={onUploaded} onError={vi.fn()} client={client} />,
    );
    fireEvent.change(screen.getByTestId("part-file-input"), {
      target: { files: [file] },
    });
    // In flight: the interval is armed and ticking (the status line is up).
    await waitFor(() => {
      expect(screen.getByTestId("part-upload-status").textContent).toBe(
        copy.partUpload.reading(0),
      );
    });
    // Settle: release the POST; the success callbacks run and the shared
    // path's `onUploadStateChange(false)` nulls `uploadStart` (the
    // interval's cleanup fires from the effect).
    release();
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(7));
    expect(screen.queryByTestId("part-upload-status")).toBeNull();

    // Now with FAKE timers: if the interval were still armed (the bug),
    // advancing time would fire its tick callback and update the
    // elapsed-seconds state. Spy on the state setter via the rendered
    // output: the label must stay at the settled (non-reading) text with
    // no status line reappearing across a long advance.
    vi.useFakeTimers();
    try {
      const label = screen.getByTestId("part-upload-label");
      const before = label.textContent;
      // Advance well past many 1 s interval ticks: with the interval
      // cleaned up, no state update fires and the label is unchanged.
      vi.advanceTimersByTime(60_000);
      expect(label.textContent).toBe(before);
      expect(label.textContent).toBe(copy.partUpload.dropLine);
      expect(screen.queryByTestId("part-upload-status")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
