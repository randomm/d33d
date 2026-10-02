import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ImportReport } from "../ImportReport";
import { ApiClient, ApiError, type PartReportInfo } from "../../../lib/api";
import copy from "../../../copy";

function makeClient(overrides: {
  setPartUnit?: ReturnType<typeof vi.fn>;
  setPartAxisMeasurement?: ReturnType<typeof vi.fn>;
} = {}): ApiClient {
  return {
    setPartUnit: vi.fn().mockResolvedValue({}),
    setPartAxisMeasurement: vi.fn().mockResolvedValue({}),
    ...overrides,
  } as unknown as ApiClient;
}

const unsettledPart: PartReportInfo = {
  filename: "gear.stl",
  format: "stl",
  unit: null,
  unit_status: "unsettled",
  scale: null,
  report: {
    triangles: 1234,
    bodies: 2,
    watertight: false,
    gaps_closed: 1,
    bbox_file_units: [100, 100, 100],
  },
  options: [
    { unit: "mm", scale: 1, extents_mm: [100, 100, 100], fits_envelope: true, at_least_5mm: true },
    { unit: "cm", scale: 10, extents_mm: [1000, 1000, 1000], fits_envelope: false, at_least_5mm: true },
    { unit: "inch", scale: 25.4, extents_mm: [2540, 2540, 2540], fits_envelope: false, at_least_5mm: true },
  ],
};

describe("ImportReport — the import report (issue #334, D6)", () => {
  it("shows the filename, triangle/body counts (en-US grouped), and the watertight flag", () => {
    const client = makeClient();
    render(<ImportReport part={unsettledPart} projectId={7} client={client} onSettled={vi.fn()} showPlate={false} />);
    expect(screen.getByTestId("import-report-title").textContent).toBe(
      copy.partReport.iRead("gear.stl"),
    );
    const metrics = screen.getByTestId("import-report-metrics").textContent;
    expect(metrics).toContain("1,234");
    expect(metrics).toContain("2 bodies");
    expect(metrics).toContain("not watertight");
  });

  it("renders the watertight-positive form when watertight is true", () => {
    const client = makeClient();
    const part: PartReportInfo = { ...unsettledPart, report: { ...unsettledPart.report!, watertight: true } };
    render(<ImportReport part={part} projectId={7} client={client} onSettled={vi.fn()} showPlate={false} />);
    expect(screen.getByTestId("import-report-metrics").textContent).toContain("watertight");
  });

  it("an unsettled part shows the waiting line, the options in given order, and no W-D-H number", () => {
    const client = makeClient();
    render(<ImportReport part={unsettledPart} projectId={7} client={client} onSettled={vi.fn()} showPlate={false} />);
    expect(screen.getByTestId("import-report-waiting").textContent).toBe(copy.partReport.waitingOnUnits);
    // Options in the given order.
    expect(screen.getByTestId("import-report-option-mm")).toBeTruthy();
    expect(screen.getByTestId("import-report-option-cm")).toBeTruthy();
    expect(screen.getByTestId("import-report-option-inch")).toBeTruthy();
    // No W-D-H line for an unsettled part.
    expect(screen.queryByTestId("import-report-wdh")).toBeNull();
  });

  it("a settled part shows the measured W×D×H (and no options)", () => {
    const client = makeClient();
    const settled: PartReportInfo = {
      filename: "gear.stl",
      format: "stl",
      unit: "mm",
      unit_status: "settled",
      scale: 1,
      report: { triangles: 100, bodies: 1, watertight: true, gaps_closed: 0, bbox_file_units: [50, 30, 20] },
      options: null,
    };
    render(<ImportReport part={settled} projectId={7} client={client} onSettled={vi.fn()} showPlate={true} />);
    const wdh = screen.getByTestId("import-report-wdh").textContent;
    expect(wdh).toContain("50.0");
    expect(wdh).toContain("30.0");
    expect(wdh).toContain("20.0");
    expect(wdh).toContain("mm");
    // No options / waiting state for a settled part.
    expect(screen.queryByTestId("import-report-options")).toBeNull();
    expect(screen.queryByTestId("import-report-waiting")).toBeNull();
  });

  it("an assumed part shows the read-as-mm line + the change-units affordance", () => {
    const client = makeClient();
    const assumed: PartReportInfo = {
      filename: "gear.stl",
      format: "stl",
      unit: "mm",
      unit_status: "assumed",
      scale: 1,
      report: { triangles: 100, bodies: 1, watertight: true, gaps_closed: 0, bbox_file_units: [50, 30, 20] },
      options: null,
    };
    render(<ImportReport part={assumed} projectId={7} client={client} onSettled={vi.fn()} showPlate={true} />);
    expect(screen.getByTestId("import-report-assumed")).toBeTruthy();
    expect(screen.getByTestId("import-report-change-units")).toBeTruthy();
  });

  it("the unsettled caption shows when the plate is hidden (unit_status !== settled)", () => {
    const client = makeClient();
    render(<ImportReport part={unsettledPart} projectId={7} client={client} onSettled={vi.fn()} showPlate={false} />);
    expect(screen.getByTestId("import-report-unsettled-caption").textContent).toBe(
      copy.partReport.unsettledCaption,
    );
  });

  it("no caption when the plate is shown (settled)", () => {
    const client = makeClient();
    const settled: PartReportInfo = {
      filename: "gear.stl",
      format: "stl",
      unit: "mm",
      unit_status: "settled",
      scale: 1,
      report: { triangles: 100, bodies: 1, watertight: true, gaps_closed: 0, bbox_file_units: [50, 30, 20] },
      options: null,
    };
    render(<ImportReport part={settled} projectId={7} client={client} onSettled={vi.fn()} showPlate={true} />);
    expect(screen.queryByTestId("import-report-unsettled-caption")).toBeNull();
  });
});

describe("ImportReport — settlement (issue #334, D8)", () => {
  it("picking an option calls setPartUnit with that unit, then refetches (onSettled)", async () => {
    const onSettled = vi.fn();
    const client = makeClient();
    render(<ImportReport part={unsettledPart} projectId={7} client={client} onSettled={onSettled} showPlate={false} />);
    fireEvent.click(screen.getByTestId("import-report-option-mm"));
    await waitFor(() =>
      expect((client.setPartUnit as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith(7, "mm"),
    );
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
  });

  it("the escape input: an explicit axis + mm POSTs setPartAxisMeasurement", async () => {
    const onSettled = vi.fn();
    const client = makeClient();
    render(<ImportReport part={unsettledPart} projectId={7} client={client} onSettled={onSettled} showPlate={false} />);
    fireEvent.click(screen.getByTestId("import-report-axis-D"));
    fireEvent.change(screen.getByTestId("import-report-escape-mm"), { target: { value: "42" } });
    fireEvent.click(screen.getByTestId("import-report-settle-btn"));
    await waitFor(() =>
      expect((client.setPartAxisMeasurement as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith(7, "D", 42),
    );
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
  });

  it("the escape input is not POSTed for a non-positive / non-finite value (button disabled)", () => {
    const client = makeClient();
    render(<ImportReport part={unsettledPart} projectId={7} client={client} onSettled={vi.fn()} showPlate={false} />);
    fireEvent.change(screen.getByTestId("import-report-escape-mm"), { target: { value: "0" } });
    expect(screen.getByTestId("import-report-settle-btn")).toBeDisabled();
    fireEvent.change(screen.getByTestId("import-report-escape-mm"), { target: { value: "abc" } });
    expect(screen.getByTestId("import-report-settle-btn")).toBeDisabled();
  });

  it("a failed settle shows its detail verbatim and changes nothing (no refetch)", async () => {
    const onSettled = vi.fn();
    const client = makeClient({
      setPartUnit: vi.fn().mockRejectedValue(new ApiError(409, "The unit is already locked")),
    });
    render(<ImportReport part={unsettledPart} projectId={7} client={client} onSettled={onSettled} showPlate={false} />);
    fireEvent.click(screen.getByTestId("import-report-option-mm"));
    await waitFor(() => {
      const el = screen.getByTestId("import-report-settle-error");
      expect(el.textContent).toContain("The unit is already locked");
    });
    expect(onSettled).not.toHaveBeenCalled();
  });
});
