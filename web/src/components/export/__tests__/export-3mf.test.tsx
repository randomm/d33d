/**
 * Export3MF tests — download-trigger UI + API-client plumbing, plus the
 * designed ending (issue #126): the completion callback fires exactly once
 * and only on success, with the version id the mark must belong to, and the
 * download filename is the deck's slug contract.
 *
 * Scope note: there is no backend 3MF-generation endpoint yet (see the
 * component's doc comment). These tests exercise the client-side plumbing
 * against an injected ApiClient/fetch fake — they do not depend on (or
 * assert) real backend behaviour.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { Export3MF } from "../Export3MF";
import { ApiClient, ApiError } from "../../../lib/api";
import type { PartReportInfo } from "../../../lib/api";
import { copy } from "../../../copy";

/** An injected ApiClient whose downloadModel3MF returns a 3MF Blob. */
function makeClient(): ApiClient {
  const client = new ApiClient();
  client.downloadModel3MF = vi
    .fn()
    .mockResolvedValue(new Blob(["x"], { type: "model/3mf" })) as never;
  return client;
}

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

  it("issue #334 (D8): a settled part leaves export enabled", () => {
    const client = makeClient();
    const settled: PartReportInfo = {
      filename: "part.stl",
      format: "stl",
      unit: "mm",
      unit_status: "settled",
      scale: 1,
      report: null,
      options: null,
    };
    render(<Export3MF projectId={7} versionId={3} part={settled} client={client} />);
    expect(screen.getByTestId("export-3mf-button")).not.toBeDisabled();
  });

  it("issue #334 (D8): an unsettled part disables export (the 3MF cannot be built)", () => {
    const client = makeClient();
    const unsettled: PartReportInfo = {
      filename: "part.stl",
      format: "stl",
      unit: null,
      unit_status: "unsettled",
      scale: null,
      report: null,
      options: null,
    };
    render(<Export3MF projectId={7} versionId={3} part={unsettled} client={client} />);
    expect(screen.getByTestId("export-3mf-button")).toBeDisabled();
  });

  it("issue #350: an assumed part leaves export ENABLED (assumed = usable; only unsettled 409s)", () => {
    const client = makeClient();
    const assumed: PartReportInfo = {
      filename: "part.stl",
      format: "stl",
      unit: "mm",
      unit_status: "assumed",
      scale: 1,
      bbox_mm: [20, 20, 20],
      report: null,
      options: null,
    };
    render(<Export3MF projectId={7} versionId={3} part={assumed} client={client} />);
    expect(screen.getByTestId("export-3mf-button")).not.toBeDisabled();
  });

  it("issue #334 (D8): no part (null) leaves the existing version gating (enabled)", () => {
    const client = makeClient();
    render(<Export3MF projectId={7} versionId={3} part={null} client={client} />);
    expect(screen.getByTestId("export-3mf-button")).not.toBeDisabled();
  });

  it("issue #338: a project with a part shows the export note under the button, filename in mono", () => {
    const client = makeClient();
    const settled: PartReportInfo = {
      filename: "bracket.stl",
      format: "stl",
      unit: "mm",
      unit_status: "settled",
      scale: 1,
      report: null,
      options: null,
    };
    render(<Export3MF projectId={7} versionId={3} part={settled} client={client} />);
    const note = screen.getByTestId("export-3mf-note");
    expect(note.textContent).toContain(
      `contains geometry from ${"bracket.stl"}`,
    );
    // The filename renders in the mono face — inert text, its own span.
    const filenameEl = screen.getByTestId("export-3mf-note-filename");
    expect(filenameEl.textContent).toBe("bracket.stl");
    expect(filenameEl.classList).toContain("mono-face");
    // The note sits under the button, inside the export surface.
    expect(note.closest(".export-3mf")).toBeTruthy();
  });

  it("issue #338: no part (undefined) shows no export note", () => {
    const client = makeClient();
    render(<Export3MF projectId={7} versionId={3} client={client} />);
    expect(screen.queryByTestId("export-3mf-note")).toBeNull();
  });

  it("issue #338: a hostile filename renders inert as text in the export note (never interpreted)", () => {
    const client = makeClient();
    const hostile: PartReportInfo = {
      filename: "../../;rm -rf $HOME `id` <img src=x onerror=alert(1)>",
      format: "stl",
      unit: "mm",
      unit_status: "settled",
      scale: 1,
      report: null,
      options: null,
    };
    render(<Export3MF projectId={7} versionId={3} part={hostile} client={client} />);
    const note = screen.getByTestId("export-3mf-note");
    // The raw filename is inert text — no new element was created from it.
    expect(note.textContent).toContain(hostile.filename);
    expect(note.querySelector("script")).toBeNull();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a DISABLED export button when no version is targeted (first run)", () => {
    const client = new ApiClient();
    render(<Export3MF projectId={projectId} client={client} />);
    expect(screen.getByTestId("export-3mf")).toBeTruthy();
    const button = screen.getByTestId("export-3mf-button") as HTMLButtonElement;
    expect(button.textContent).toBe("Export 3MF");
    expect(button.disabled).toBe(true);
  });

  it("enables the export button when a versionId is present and no failure is pending", () => {
    const client = new ApiClient();
    render(<Export3MF projectId={projectId} versionId={4} client={client} />);
    const button = screen.getByTestId("export-3mf-button") as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });

  it("names the download by the deck's slug contract (exportFilename), not a default", async () => {
    const blob = new Blob(["x"], { type: "model/3mf" });
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockResolvedValue(blob);

    let capturedName = "";
    vi.spyOn(HTMLAnchorElement.prototype, "download", "set").mockImplementation(
      function (this: HTMLAnchorElement, value: string) {
        capturedName = value;
      },
    );

    render(
      <Export3MF
        projectId={projectId}
        projectName="Curtain rod bracket"
        versionId={4}
        versionName="v4"
        client={client}
      />,
    );
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(capturedName).toBe("curtain-rod-bracket-v4.3mf");
    });
  });

  it("uses the ordinal-based versionName the parent supplies (issue #387: v1, not v64)", async () => {
    // The parent (App) computes the filename from the timeline ordinal via
    // the shared versionOrdinals helper and passes it as `versionName`. The
    // component must use that name verbatim — never derive `v${id}` itself
    // when a name is provided. The divergence (id 64 at position 1 → "v1")
    // is pinned here: versionId={64}, versionName="v1" → the download name
    // carries "v1", not "v64".
    const blob = new Blob(["x"], { type: "model/3mf" });
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockResolvedValue(blob);

    let capturedName = "";
    vi.spyOn(HTMLAnchorElement.prototype, "download", "set").mockImplementation(
      function (this: HTMLAnchorElement, value: string) {
        capturedName = value;
      },
    );

    render(
      <Export3MF
        projectId={projectId}
        projectName="Curtain rod bracket"
        versionId={64}
        versionName="v1"
        client={client}
      />,
    );
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(capturedName).toBe("curtain-rod-bracket-v1.3mf");
    });
  });

  it("a stale versionId (nameless, not in the timeline) yields 'current' in the filename — never the raw id (issue #387)", async () => {
    // The parent computes the suffix from the shared versionOrdinals helper:
    // an id not in the loaded list yields NO number, so the parent passes no
    // versionName. The component must then fall back to "current" — it must
    // not invent `v${versionId}` on its own (the QA 2026-10-04 drift: raw id
    // in the filename while the strip shows the ordinal).
    const blob = new Blob(["x"], { type: "model/3mf" });
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockResolvedValue(blob);

    let capturedName = "";
    vi.spyOn(HTMLAnchorElement.prototype, "download", "set").mockImplementation(
      function (this: HTMLAnchorElement, value: string) {
        capturedName = value;
      },
    );

    render(
      <Export3MF
        projectId={projectId}
        projectName="Curtain rod bracket"
        versionId={64}
        client={client}
      />,
    );
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(capturedName).toBe("curtain-rod-bracket-current.3mf");
    });
  });

  it("downloads the 3MF blob and triggers a browser download on click", async () => {
    const blob = new Blob(["fake 3mf bytes"], { type: "model/3mf" });
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockResolvedValue(blob);

    render(
      <Export3MF
        projectId={projectId}
        projectName="widget"
        versionId={1}
        versionName="v1"
        client={client}
      />,
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

    render(<Export3MF projectId={projectId} versionId={3} client={client} />);
    const button = screen.getByTestId(
      "export-3mf-button",
    ) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    fireEvent.click(button);

    await waitFor(() => {
      expect(button.textContent).toBe("Exporting…");
    });
    expect(button.disabled).toBe(true);

    resolveDownload(new Blob(["x"]));
    await waitFor(() => {
      expect(button.textContent).toBe("Export 3MF");
    });
    expect(button.disabled).toBe(false);
  });

  it("surfaces a 404 as an error state AND returns the button to disabled (failed attempt is not retryable by re-click)", async () => {
    const client = new ApiClient();
    const spy = vi.spyOn(client, "downloadModel3MF").mockRejectedValue(
      new ApiError(404, "Not Found"),
    );

    render(<Export3MF projectId={projectId} versionId={4} client={client} />);
    const button = screen.getByTestId("export-3mf-button") as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByTestId("export-3mf-error")).toBeTruthy();
    });
    expect(screen.getByTestId("export-3mf-error").textContent).toContain(
      copy.export3mf.failed,
    );
    // The 404 body carries no error_class — the generic export sentence is
    // the primary text, never the raw "API 404: …" message.
    expect(screen.getByTestId("export-3mf-error").textContent).not.toContain(
      "API 404",
    );
    expect(createObjectURLSpy).not.toHaveBeenCalled();
    // The failed attempt must leave the button DISABLED — a re-click is not
    // a valid retry in the same target state.
    expect(button.disabled).toBe(true);
    // A second click does not re-issue the download.
    const callsBefore = spy.mock.calls.length;
    fireEvent.click(button);
    expect(spy.mock.calls.length).toBe(callsBefore);
  });

  it("calls onExported exactly once, with the version id, AFTER a successful download", async () => {
    const blob = new Blob(["x"], { type: "model/3mf" });
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockResolvedValue(blob);
    const onExported = vi.fn();

    render(
      <Export3MF
        projectId={projectId}
        versionId={7}
        versionName="v7"
        client={client}
        onExported={onExported}
      />,
    );
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(onExported).toHaveBeenCalledTimes(1);
    });
    expect(onExported).toHaveBeenCalledWith(7);
  });

  it("a new versionId arriving clears the failed-download flag and re-enables the button", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockRejectedValue(
      new ApiError(404, "Not Found"),
    );

    const { rerender } = render(
      <Export3MF projectId={projectId} versionId={4} client={client} />,
    );
    const button = screen.getByTestId("export-3mf-button") as HTMLButtonElement;
    fireEvent.click(button);

    await waitFor(() => {
      expect(button.disabled).toBe(true);
    });

    // A new version is targeted (the parent's listVersions refetch replaced
    // the list). The failed flag from the old target must clear.
    rerender(
      <Export3MF projectId={projectId} versionId={5} client={client} />,
    );
    await waitFor(() => {
      expect(button.disabled).toBe(false);
    });
  });

  it("a failed export calls onExported NEVER (no turn, no mark)", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockRejectedValue(
      new ApiError(404, "Not Found"),
    );
    const onExported = vi.fn();

    render(
      <Export3MF
        projectId={projectId}
        versionId={7}
        client={client}
        onExported={onExported}
      />,
    );
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(screen.getByTestId("export-3mf-error")).toBeTruthy();
    });
    expect(onExported).not.toHaveBeenCalled();
  });

  it("a 502 with error_class 'slice' renders the copy.ts slice sentence, not the raw API message", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockRejectedValue(
      new ApiError(
        502,
        "validation failed — no 3MF produced: Slice dry run failed: can not find setting file: qidi-q2-plus-2",
        "slice",
      ),
    );

    render(<Export3MF projectId={projectId} versionId={4} client={client} />);
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(screen.getByTestId("export-3mf-error")).toBeTruthy();
    });
    // The primary sentence is the copy.ts mapping — never the raw text.
    expect(screen.getByTestId("export-3mf-error").textContent).toContain(
      copy.failure.reasons.slice,
    );
    expect(screen.getByTestId("export-3mf-error").textContent).not.toContain(
      "API 502",
    );
    // The raw backend detail stays reachable in the collapsed disclosure.
    expect(
      screen.getByTestId("export-3mf-error-raw-code").textContent,
    ).toContain("can not find setting file");
  });

  it("a 409 with error_class 'conflict' renders the conflict sentence", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockRejectedValue(
      new ApiError(
        409,
        "validation in progress",
        "conflict",
      ),
    );

    render(<Export3MF projectId={projectId} versionId={4} client={client} />);
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(screen.getByTestId("export-3mf-error")).toBeTruthy();
    });
    expect(screen.getByTestId("export-3mf-error").textContent).toContain(
      copy.export3mf.conflict,
    );
    expect(screen.getByTestId("export-3mf-error").textContent).not.toContain(
      "API 409",
    );
  });

  it("a 502 with an unmapped error_class ('unknown') renders the generic sentence", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockRejectedValue(
      new ApiError(
        502,
        "validation failed — no 3MF produced: something odd",
        "unknown",
      ),
    );

    render(<Export3MF projectId={projectId} versionId={4} client={client} />);
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(screen.getByTestId("export-3mf-error")).toBeTruthy();
    });
    expect(screen.getByTestId("export-3mf-error").textContent).toContain(
      copy.export3mf.failed,
    );
    expect(screen.getByTestId("export-3mf-error").textContent).not.toContain(
      "API 502",
    );
  });

  it("a network failure (non-ApiError rejection) renders the generic sentence, not the stack", async () => {
    const client = new ApiClient();
    vi.spyOn(client, "downloadModel3MF").mockRejectedValue(
      new TypeError("Failed to fetch"),
    );

    render(<Export3MF projectId={projectId} versionId={4} client={client} />);
    fireEvent.click(screen.getByTestId("export-3mf-button"));

    await waitFor(() => {
      expect(screen.getByTestId("export-3mf-error")).toBeTruthy();
    });
    expect(screen.getByTestId("export-3mf-error").textContent).toContain(
      copy.export3mf.failed,
    );
    expect(screen.getByTestId("export-3mf-error").textContent).not.toContain(
      "Failed to fetch",
    );
  });

  it("uses a default same-origin ApiClient when none is injected", () => {
    // No `client` prop passed — the component must not throw at render time.
    render(<Export3MF projectId={projectId} />);
    expect(screen.getByTestId("export-3mf")).toBeTruthy();
  });

  it("the export failure's primary sentence uses the blocked colour token (not the marker)", () => {
    // jsdom does not load web/src/styles.css, so the blocked colour cannot
    // be computed from the stylesheet here. This source-level tripwire
    // (mirroring the design-contract test's failure-turn-bar assertion) pins
    // the rule: `.export-3mf-error-message` must reference var(--color-blocked)
    // and no rule in the export-error scope may reference #FF3300 — the
    // marker colour is reserved for the region marker and nothing else.
    const css = readFileSync(
      join(__dirname, "..", "..", "..", "styles.css"),
      "utf8",
    );
    expect(css).toMatch(
      /\.export-3mf-error-message[\s\S]*?var\(--color-blocked\)/,
    );
    const exportBlock = css.match(/\.export-3mf-error[\s\S]*?\n\}\n/);
    expect(exportBlock, "export-3mf-error rule block must exist in styles.css").toBeTruthy();
    expect(exportBlock?.[0]).not.toMatch(/#FF3300/i);
  });
});
