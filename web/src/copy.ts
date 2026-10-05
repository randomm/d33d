/**
 * The copy deck — every user-facing string in the SPA.
 *
 * Components never inline prose. A wording change is a one-file diff, and review
 * catches drift for free. Strings that embed a measurement are functions, so the
 * formatting of a number is decided once (`mm`) and not retyped per surface.
 *
 * House rules encoded here, not negotiable per surface:
 * - A value that has not been established renders `brief.unknownValue`, never a
 *   number, never "0", never an em-dash standing in for one.
 * - Failure copy names the measured number and the limit together, then offers
 *   actions, then the raw reason. It never leads with a code.
 * - Nothing implies d33d slices. Where Orca is the answer, say Orca.
 */

/** Narrow no-break space — keeps "34 mm" from breaking across a line. */
const NB = "\u202F";

/** One decimal, always, with the unit attached. Every displayed dimension. */
export const mm = (value: number): string => `${value.toFixed(1)}${NB}mm`;

/** Diameter, for bores and shafts. */
export const dia = (value: number): string => `Ø${value.toFixed(1)}${NB}mm`;

/** Seconds, for elapsed time. */
export const secs = (value: number): string => `${Math.round(value)}${NB}s`;

/** W/D/H axis → the adjective that belongs in a "how …?" slot ("wide",
 *  "deep", "tall"). The carried-axis sentence and the size-mismatch
 *  follow-up must read "how wide it should be", not "how width it should
 *  be" (issue #398); the noun form ("width") stays in the per-axis rows
 *  (see `SIZE_AXIS_WORDS` in lib/errorMapping). */
export const SIZE_AXIS_ADJECTIVES = {
  W: "wide",
  D: "deep",
  H: "tall",
} as const;

export const brief = {
  eyebrow: "What we're building",
  emptyBody:
    "Nothing yet. This fills in as you talk — and it only ever shows what has actually been pinned down.",

  /** The value cell for provenance "unknown". Never replace with a number. */
  unknownValue: "not established",
  /** The one-question the unknown-value control sends the assistant. */
  askEstablish: (label: string): string => `What is the ${label}?`,
  /** The value cell for a parameter being re-derived by the pass in flight. */
  remeasuring: "re-measuring…",
  /** The value cell on a FIRST pass: nothing has been measured yet, and saying
   *  so is the honest form. Never a number, never a guess from the request. */
  awaitingFirstMeasure: "measured when it lands",

  /** The user-facing axis label for an axis row (`kind: "axis"`) — the
   *  dimension protocol's own axes (W/D/H), not a prettified parameter
   *  name. Mirrors the backend's `AXIS_LABELS` in `d33d/design_state.py`,
   *  which the prompt uses for its own rendering. */
  axisLabel: { W: "Width", D: "Depth", H: "Height" } as Record<string, string>,

  legend: {
    stated: "you said",
    measured: "measured off the model",
    /** The model picked this value — nobody said it (issue #246). */
    assumed: "I assumed",
    unknown: "unknown",
    disagrees: "disagrees",
  },

  /** The expanded assumed row: nobody stated this value, the model picked
   *  it. Issue #248: when the model carried a ``reason`` for the value
   *  (a value the user did not give), the sentence gains the reason
   *  clause; the reason-less variant is #246's sentence, unchanged. */
  provenanceAssumed: (quoted: string): string =>
    `Nobody said this. I picked ${quoted}.`,

  /** Issue #248: the expanded assumed row when the model carried a
   *  reason for the value — the same sentence with the reason clause
   *  appended. The reason is the model's own words (the ``reason`` field
   *  of the ``parameters`` metadata), never a fabricated justification. */
  provenanceAssumedWithReason: (quoted: string, reason: string): string =>
    `Nobody said this. I picked ${quoted} because ${reason.replace(/[.\s]+$/, ".")}`,

  /** The chip's assumed-value count (issue #246): how many values the
   *  model assumed on its own — counted separately from unknowns, which
   *  are values nobody has established at all. "Assumed" is already
   *  plural-sounding, so the count is the only thing that varies:
   *  "4 assumed", "1 assumed". */
  collapsedAssumed: (count: number): string =>
    `${count} assumed`,

  /** Shown when a row is expanded and the value came from the user. */
  provenanceStated: (quoted: string, at: string): string =>
    `You typed “${quoted}” at ${at}. It is a named parameter in the model, so every change keeps it unless you say otherwise.`,

  /** Shown when a row is expanded and the value was read off the mesh. */
  provenanceMeasured: (version: string): string =>
    `Measured off ${version} after it passed validation. Nobody stated this — it is what the model came out as.`,

  /** Shown on a row where the measured value is outside tolerance of the stated one. */
  disagreement: (statedMm: number, measuredMm: number): string => {
    const delta = Math.abs(measuredMm - statedMm);
    const direction = measuredMm < statedMm ? "short" : "over";
    return `You asked for ${mm(statedMm)}. What came out measures ${mm(measuredMm)} — ${mm(delta)} ${direction}, which is outside tolerance. The measured number is the one shown, because it is the one that will print.`;
  },

  /** Shown on a `disagrees` row the MODEL caused (issue #264):
   *  `disagrees_source === "model"` — the value nobody stated, the model
   *  picked it, and the measurement contradicts it. Never "you asked
   *  for": the user gave no value here, so the model is named as the
   *  source. `label` is the parameter's user-facing label; the two
   *  numbers are the model's value and the measured one, in that order.
   *  Sibling of `disagreement` (the user-source variant, unchanged). */
  disagreementModel: (label: string, modelMm: number, measuredMm: number): string => {
    const delta = Math.abs(measuredMm - modelMm);
    const direction = measuredMm < modelMm ? "short" : "over";
    return `I set ${label} to ${mm(modelMm)}, but the part measures ${mm(measuredMm)} — ${mm(delta)} ${direction}. The measured number is the one that will print.`;
  },

  /** One row expanded, provenance in a sentence — the user asked for a
   *  value with no measurement to compare it against. */
  provenanceNoMeasurement: (quoted: string): string =>
    `You asked for ${quoted}. Nothing has measured it yet, so this is held as stated until a render comes back.`,

  rowActions: { change: "Change it", locate: "Show it on the model" },

  /** The collapsed chip: name, envelope, and how many facts are still missing. */
  collapsedUnknowns: (count: number): string =>
    `${count} unknown${count === 1 ? "" : "s"}`,

  openLabel: "Open the brief",
  collapseLabel: "Collapse the brief",

  /** The Brief does NOT change when a pass fails — a failed candidate was never
   *  accepted, so nothing it describes has moved. It gains this footer, which is
   *  also the link into the failure card. */
  failedFooter: (what: string): string =>
    `${what} didn't pass. Nothing above changed.`,

  /** The design-state refetch failed twice in a row (issue #237): the
   *  last-known block stays visible and this line says it may be stale.
   *  The marker clears on the next successful fetch. */
  refreshFailed:
    "Couldn't refresh — these values may be out of date.",

  /** Above 7 COLLAPSIBLE rows the settled, agreeing param rows fold into one
   *  honest count — the disclosure line for the hidden ones. Axis rows,
   *  disagrees rows, and unknowns are never grouped away; the unknowns are
   *  the thing to act on, so they surface out of the list. */
  unresolvedHeading: (count: number): string => `Unresolved · ${count}`,

  /** The Brief's storage-degradation states (issue #295): the project's
   *  saved design source or reference photo is missing on disk (the server
   *  reports both via the project's `storage` field — the SPA reads it,
   *  never recomputes). Both render in `var(--color-blocked)` ochre —
   *  never the region-marker colour (W17: the marker hex has exactly one
   *  home, lib/marker.ts). The banner is HONEST ABOUT WHAT
   *  SURVIVES: the persisted params/bbox still show as-is underneath, so
   *  the sentence never claims the design is lost, only that the SOURCE
   *  is. This is also the exact wire string the server's no-run chat reply
   *  and the restore/branch 409 carry (the design-contract tripwire pins
   *  the three-way agreement — the #260 way). */
  savedDesignMissing:
    "The saved design for this project is missing, so I can't change it. Start a new design, or describe it again and I'll make it fresh",
  /** The Brief's small marker for a reference photo that was deleted after
   *  being stored (the `storage.photo_present === false` state — a
   *  photo-LESS project is `null` and shows nothing). */
  referencePhotoMissing: "Reference photo missing",

  /** The collapsed-params disclosure line (issue #274): how many settled,
   *  agreeing param rows are hidden behind it. Singular-aware. */
  moreParameters: (count: number): string =>
    `${count} more parameter${count === 1 ? "" : "s"}`,

  /** The zone header for "The part you brought" (issue #338, operator
   *  decision 1). Rendered only when the design-state `part` block is
   *  present. */
  partBroughtHeader: "The part you brought",
  /** The zone header for "Your changes" (issue #338, operator decision 1).
   *  Rendered only when the zone has at least one row. */
  yourChangesHeader: "Your changes",
  /** The note under the W/D/H rows of "The part you brought" (issue #338,
   *  operator decision 1). {units} is the settled unit (mm / cm / inch).
   *  Rendered only for a settled part. */
  partBroughtNote: (units: string): string =>
    `Measured, in ${units} you confirmed. Its own features are fixed — I can add and cut, not resize.`,
  /** The part-zone note for an ASSUMED part (issue #350, operator decision
   *  3): the mm reading is assumed, not confirmed — the W/D/H rows carry
   *  assumed provenance, and this note says so. Rendered only for an
   *  assumed part (a settled part keeps `partBroughtNote`). */
  partBroughtNoteAssumed:
    "Read as millimetres — if it's in inches, tell me.",
  /** The part-zone note for a part whose units came FROM THE FILE
   *  (issue #352, operator decision 3 — the wire discriminator is
   *  `part.format === "3mf"`): a 3MF is always stored settled as mm by
   *  the import (d33d/part_import.py), so the units were never the
   *  user's choice. The spec's exact sentence — "you confirmed" is
   *  reserved for the user-settled case (`partBroughtNote`). */
  partBroughtNoteFromFile:
    "Measured, in the file's own millimetres. Its own features are fixed — I can add and cut, not resize.",
  /** The part-zone note for a part settled via the MEASUREMENT escape
   *  (issue #352, #350 follow-up comment): d33d/part_import.py settles
   *  `part_unit = "custom"` when the user measured one axis in mm (the
   *  scale was derived, not a unit choice), so `partBroughtNote("custom")`
   *  would read "in custom you confirmed" — a unit the user never chose.
   *  This note names the measurement instead. Rendered only for a settled
   *  part whose unit is "custom". */
  partBroughtNoteMeasured:
    "Scaled from the measurement you gave. Its own features are fixed — I can add and cut, not resize.",
  /** The W/D/H value cell when the part's units are unsettled (issue #338).
   *  Never a number, never 0. */
  waitingOnUnits: "waiting on units",
} as const;

export const passCard = {
  /** The pass card's summary line for a pass that produced and validated a
   *  design. An honest generic fallback: no dimensions, no fabricated
   *  description — nothing the done frame cannot establish (issue #218). */
  summary:
    "Your design is ready — it was built and checked against the print limits.",

  viewLabels: ["Front", "Back", "Left", "Right", "Top", "Iso"] as const,

  /** When fewer than six views arrived. Never pretend all six are there. */
  partialViews: (arrived: number, total: number): string =>
    `${arrived} of ${total} views came back — the rest failed to render.`,

  heldSuffix: "held",
  sourceDisclosure: (lines: number): string => `OpenSCAD, ${lines} lines`,
  sourceChanged: (lines: number): string =>
    `${lines} line${lines === 1 ? "" : "s"} changed`,
  sourceFootnote:
    "Named parameters, because that is what makes the next change a change and not a rewrite. Copy it into OpenSCAD if you want — nothing here needs you to.",

  checksPassed: (count: number): string => `${count} checks`,
  closeDisclosure: "Close the source",
  viewEnlargedCaption:
    "Rendered straight from the model, not a photo of the viewport. This is the picture the system itself looked at.",
  viewActions: {
    next: "Next view",
    /** The enlarged view's single action. The frame carries a PNG (no
     *  geometry), so nothing moves to the stage — the button closes the
     *  enlargement and the streamed model stays beside the photo as it
     *  already is. Relabelled from "Beside the photo", which promised a
     *  swap the data cannot make. */
    closeView: "Close this view",
  },
} as const;

/** The first pass of a new project — the one moment the "previous model stays on
 *  screen" rule cannot apply, because there is no previous model. The renders
 *  themselves fill the canvas as they land: before a mesh exists they are the only
 *  real picture of the object, so nothing shown is a stand-in. */
export const firstPass = {
  explain:
    "Nothing has been built here before, so there is no earlier version to look at. Each view appears on the canvas as it finishes.",
  heroCaption: "a real render, not a preview — the turnable model arrives last",
} as const;

export const progress = {
  /** The dimmed previous model is captioned, so nobody thinks it is the new one. */
  stillShowing: (current: string, pending: string): string =>
    `Still showing ${current} — ${pending} replaces it when it is ready`,

  /** Stage labels. Extend design_loop_events.py rather than adding labels here. */
  stages: {
    write: "Wrote the model",
    writeActive: "Writing the model",
    build: "Built the solid",
    buildActive: "Building the solid",
    render: "Rendering six views",
    critique: "Compare against your photo",
    validate: "Check it will print",
  },

  viewsProgress: (done: number, total: number): string =>
    `${done} of ${total} — each view is a separate render, they come in one at a time.`,

  /** The first line of the design-loop stage: an attempt is announced as
   *  it starts, never discovered (the repair attempt is announced, not
   *  hidden — W11 rule 4). The first pass is the baseline; from the second
   *  on the number is what makes the counter reset legible. */
  attemptLine: (attempt: number, max: number): string =>
    attempt <= 1 ? "Attempt 1 of up to 3" : `Attempt ${attempt} of up to ${max}`,

  /** The default pass time expectation. Shown only when the project has
   *  no imported part (PassProgress keys off the `importedPart` prop):
   *  a bare from-scratch pass is the one that usually lands in 15–30 s.
   *  Issue #332: the import path is slower (the part seeds the render
   *  volume and its bbox is the gate's ground truth), so on that path no
   *  copy may promise a time window — `expectationImport` carries no
   *  number at all, the honest statement for a path whose duration is
   *  not established. */
  expectation: "Usually 15–30\u202Fs. You will see it change.",
  /** The import path's expectation: no 15–30 s promise — the duration
   *  is not established, and this line never invents one. */
  expectationImport: "It takes a little longer when I'm working around your import. You will see it change.",
  stop: "Stop",

  /** An auto-repair round is announced, never hidden.
   *  Attempts are counted openly rather than named in ordinals — "Attempt 3 of 3"
   *  scales and stays grammatical where "Third try" does not, and a maker would
   *  rather know how many are left than be soothed. */
  repairAttempt: (attempt: number, max: number, whatChanged: string): string => {
    const left = max - attempt;
    const tail =
      left <= 0
        ? "This is the last one \u2014 if it is still wrong I will stop and ask you."
        : `${left} ${left === 1 ? "try" : "tries"} left before I stop and ask you.`;
    return `Attempt ${attempt} of ${max}. ${whatChanged} ${tail}`;
  },

  /** The composer disables with its reason visible, never silently. */
  composerBusy: (pending: string): string =>
    `Working on ${pending} — one change at a time`,
} as const;

export const failure = {
  /** Always say what survived. */
  survived: (version: string): string =>
    `${version} is unchanged and still exportable.`,
  /** The survived line when the export button is actually disabled
   *  (issue #352, operator decision 4: the part's units are unsettled or
   *  a design pass is in flight — the 409 gate the export button renders
   *  the same way). Names the survivor, never claims exportability.
   *  Rendered from FailureTurn only when `exportable` is false. */
  survivedNoExport: (version: string): string =>
    `${version} is unchanged.`,

  envelope: {
    headline: "It won't fit on the bed.",
    /** Part 1 + part 2 in one sentence: the measured value and the limit
     *  together, then the Orca hand-off — the limit is the API's number, so
     *  the sentence is only ever printed once both are established. */
    body: (actualMm: number, limitMm: number): string =>
      `It came out ${mm(actualMm)} across and the plate is ${mm(limitMm)}. Orca can't slice around this — the part itself has to get smaller, or come apart.`,
    overhang: (byMm: number): string => `${mm(byMm)} past the edge`,
    /** The part-2 axis row when the envelope failure carries a measurement:
     *  the measured value beside the limit, the failing axis in the blocked
     *  colour. `label` is the axis letter from the API's x/y/z. */
    axisRow: (label: string, actualMm: number, limitMm: number): string =>
      `${label}: ${mm(actualMm)} / ${mm(limitMm)}`,
    /** The axis row for a measured axis that FITS — the same component
     *  renders at every severity (W12's design-team answer 3). */
    axisRowFits: (label: string, actualMm: number, limitMm: number): string =>
      `${label}: ${mm(actualMm)} / ${mm(limitMm)} — fits`,
    /** The axis row when the measurement for that axis is not established —
     *  the house rule: a phrase, never an invented number. */
    axisNotMeasured: (label: string): string => `${label}: not measured yet`,
    actions: {
      split: "Split it into two parts that bolt together",
      scale: "Scale the whole thing down to fit",
      biggerPrinter: "My printer is bigger than that",
    },
  },

  exhausted: {
    headline: "I tried three times and it still isn't right.",
    body: (label: string, statedMm: number, measuredMm: number): string =>
      `The closest one is ${mm(Math.abs(statedMm - measuredMm))} ${measuredMm < statedMm ? "short of" : "over"} the ${mm(statedMm)} you asked for on ${label.toLowerCase()}. I kept it so you can look at it, but I haven't made it the current version.`,
    actions: { retry: "Try again", keep: "Keep it anyway" },
  },

  /** The carried-axis variant of the bbox-gate failure (issue #261 fix
   *  batch): the failing axis was CARRIED from an earlier statement, not
   *  cued this turn — the held value is named (formatted by `mm`) and the
   *  user is told how to override it. A dedicated sibling of `reasons`
   *  (NOT inside it): `reasons` is the closed reason-code → string map
   *  that `exportErrorCopy` spreads and `displayDesignLoopError` looks up
   *  by reason code; this is a parameterized sentence selected by
   *  `carried_axes` presence, not a reason code. Rendered ONLY when the
   *  frame's `carried_axes` carries an axis the gate enforced. */
  bboxCarried: (label: string, heldMm: number): string => {
    // The "how …" slot takes the per-axis adjective ("wide"/"deep"/"tall"),
    // not the noun ("width"/"depth"/"height") — "how deep", never "how depth"
    // (issue #398). The label is the noun; map it back to the adjective.
    const adjective =
      label === "width" ? "wide" : label === "depth" ? "deep" : "tall";
    return `I kept the ${label.toLowerCase()} you asked for (${mm(heldMm)}). If you meant to change it, say how ${adjective} it should be.`;
  },

  /** Part 2 line for an `axis_params_mismatch` failure (issue #276): the
   *  mismatching parameter's declared value and the measured extent on its
   *  axis, both mm-formatted (rendered in mono by the failure turn). One
   *  line per mismatching parameter — the headline sentence (without
   *  numbers) lives in `reasons`. */
  axisMismatchLine: (label: string, modelMm: number, measuredMm: number): string =>
    `${label}: ${mm(modelMm)} → ${mm(measuredMm)}`,

  /** The model pre-flight helper (issue #303): the helper sentence when the
   *  terminal `model_unconfigured` frame named the missing env var (the
   *  frame's `env_var` field) — the name renders in the mono face, the fix
   *  is the operator's. A sibling of `reasons`, not a reason-code entry: it
   *  is a second sentence the failure turn renders after the headline. */
  modelUnconfiguredHelper: (envVar: string): string =>
    `Set ${envVar} where the server runs, then restart it.`,
  /** The model pre-flight helper (issue #303): the helper sentence when the
   *  pre-flight could not resolve the model itself (no env var to name —
   *  the alias/role is missing). Distinct from the env-var helper: the fix
   *  is the settings file, not a shell variable. Also a sibling of `reasons`. */
  modelUnresolved: "Check the model settings.",
  /** The renderer image pre-flight disclosure (issue #346): the fault
   *  line naming the verified reason (image missing vs label mismatch).
   *  A sibling of `reasons`, not a reason-code entry: the failure turn
   *  renders it in the mono face under the headline. The rebuild command
   *  renders separately in `<code>`; this is the reason line only. */
  rendererImageMissing:
    "image missing: the render-worker image is not in the Docker daemon",
  rendererImageLabelMismatch: (actual: string | undefined, expected: string | undefined): string =>
    `label mismatch: image label ${actual ?? "(unlabeled)"} does not match expected ${expected ?? "(unknown)"}`,
  /** The label prefix for the rebuild command in the collapsed disclosure
   *  (issue #346). */
  rebuildLabel: "rebuild",

  /** One sentence per closed-set reason. errorMapping.ts keeps the mapping; this
   *  holds the words. The map must stay total — every GATE_REASON_BITS value,
   *  every render ErrorClass value, and the loop-level pre-flight reasons
   *  (`renderer_unavailable`, issue #277; `model_unconfigured`, issue #303;
   *  `renderer_image_stale`, issue #346)
   *  have an entry (the `model_unconfigured` entry is the headline; the
   *  sibling helpers `modelUnconfiguredHelper` / `modelUnresolved` render
   *  after it). */
  reasons: {
    /** Loop-level pre-flight (issue #303): the configured LLM model could
     *  not be used, so nothing was designed. Terminal — retrying changes
     *  nothing until the operator sets the env var (or fixes the model
     *  settings), which is why the failure turn offers no retry. The
     *  helper sentences (sibling keys `modelUnconfiguredHelper` /
     *  `modelUnresolved`, not reason codes) name the env var or the
     *  settings; the failure turn renders them after this headline. */
    model_unconfigured:
      "The model isn't configured, so nothing was designed.",
    bbox_out_of_tolerance:
      "It came out a different size from the one you asked for.",
    stated_dims_not_named_parameters:
      "The dimensions you gave didn't end up as parameters, so the next change would not be able to hold them.",
    views_blank_or_missing: "It built, but the preview images came out blank.",
    error_class_not_ok: "The design step produced nothing usable.",
    axis_params_mismatch:
      "The part came out a different size from its own measurements.",
    ok: "The design step produced nothing usable.",
    syntax_error: "The generated design had a syntax error, so nothing was built.",
    unknown_variable:
      "The generated design referenced a size that was never set, so nothing was built.",
    empty_model: "The design produced an empty model — there is nothing to print.",
    artifact_error: "The model file came out unreadable.",
    timeout: "The render ran out of time. A simpler shape will get through.",
    design_loop_timed_out:
      "The design loop stopped responding — it ran past its time limit.",
    oom: "The model was too heavy to render. A simpler shape will get through.",
    container_error:
      "The render environment failed. That is temporary — try again.",
    renderer_unavailable:
      "The renderer isn't running, so nothing was designed. Start Docker and try again.",
    /** Loop-level pre-flight (issue #346): the render-worker image is
     *  missing or its build-hash label no longer matches the tree, so no
     *  render can run. Terminal — retrying changes nothing until the
     *  operator rebuilds the image (the "What the checker actually said"
     *  disclosure carries the real reason and the rebuild command in the
     *  mono face), which is why the failure turn offers no retry. */
    renderer_image_stale: "The renderer needs rebuilding.",
    load_error: "The finished model could not be loaded back for checking.",
    watertight:
      "The model has holes in its surface, so a slicer can't tell inside from outside.",
    winding: "The model's surfaces face inconsistently, which confuses slicers.",
    dimension: "The finished size is outside tolerance of the size you stated.",
    volume: "The model has no volume, or an implausible amount of geometry.",
    slice: "A test slice of the model failed.",
    envelope: "It doesn't fit inside the build volume.",
    export_error: "The 3MF could not be written.",
  },

  /** One badge over the canvas ties the card and the drawing together: they are
   *  the same event, and this says which version you still actually have. */
  attemptBadge: (kept: string): string =>
    `This is the attempt that failed. It was not saved — ${kept} is still yours.`,
  restoreKept: (kept: string): string => `Put ${kept} back on screen`,
  /** The card points at the canvas so the two halves read as one thing. */
  seeItOnThePlate: "It's on the plate to your left, with the overhang picked out.",

  /** The size-mismatch card (issue #367): rendered when a
   *  `bbox_out_of_tolerance` frame does NOT carry the gate-7 envelope
   *  string (the stated-size gate, not the build-plate gate). One mono
   *  row per axis — "asked → made". The axis word is the noun form
   *  ("width", "depth", "height"). */
  sizeMismatch: {
    /** The per-axis row: asked → made, both mm-formatted. */
    row: (axisWord: string, askedMm: number, madeMm: number): string =>
      `${axisWord}: ${mm(askedMm)} → ${mm(madeMm)}`,
    /** The per-axis row when only the made value is established
     *  (import projects, or an axis the user never stated). */
    madeOnly: (axisWord: string, madeMm: number): string =>
      `${axisWord}: ${mm(madeMm)}`,
    /** The follow-up question (operator decision 3): the HEADING of the
     *  two-button lip question on the size card, the first axis in W/D/H
     *  order that has both an asked and a made value beyond the gate
     *  tolerance. `axisAdjective` is the per-axis adjective ("wide",
     *  "deep", "tall") — the "how …" slot must read "how wide", never
     *  the noun "how width" (issue #398). */
    whichMeasurement: (askedMm: number, axisAdjective: string): string =>
      `Is ${mm(askedMm)} how ${axisAdjective} the part itself is, or how the whole thing is, lip included?`,
    /** The two lip-question button LABELS (operator decision 4): the short
     *  names the operator chose for the two-button split. The buttons
     *  prefill the composer with `lipPartItself` / `lipOverallIncludingLip`;
     *  the labels are what is printed on the buttons themselves. */
    lipButtonPartItself: "The part itself",
    lipButtonOverallIncludingLip: "Overall, including the lip",
    /** The two lip-question prefills (operator decision 4): each prefills
     *  the composer with the answer it represents, so the next design
     *  pass holds the right number. `axisNoun` is "width"/"depth"/"height"
     *  (the row's noun); the overall variant names the lip explicitly so
     *  the two answers cannot be confused. */
    lipPartItself: (askedMm: number, axisNoun: string): string =>
      `${mm(askedMm)} is the part's own ${axisNoun}, not the overall size.`,
    lipOverallIncludingLip: (askedMm: number, axisNoun: string): string =>
      `${mm(askedMm)} is the overall ${axisNoun}, including the lip.`,
  },

  /** Part 3 — the retry action for a failure with no dedicated action
   *  set: a concrete, sendable line. */
  retryAction: "Try again",
  rawDisclosure: "What the checker actually said",
  genericRetry:
    "Something went wrong on the way there. Try again — if it keeps happening, the detail below is the useful part.",
} as const;

export const region = {
  placeholder: "Change this part… e.g. “open the top so the rod drops in”",
  apply: "Apply",
  cancel: "Cancel selection",
  resolvedTo: "this point is on the",
  /** The module chip's inline hint — names the module's local name (mono) and
   *  the human phrasing. The full sentence is `resolvedTo` + " " + `name`. */
  poseHint: "Turning the model clears the pin — it only means this from here.",
  cleared:
    "Pin cleared, the view changed. Turn to the angle you want, then click the model.",
  clearedHint:
    "The view turned, so the pin is gone. Re-pick once the angle is right.",
  missedGeometry: "Click on the model to point at a part.",
  notLoaded: "Model not loaded yet — click again once it appears.",
  pending: "A point is selected. Finish in the bar on the model, or press Esc.",
  /** The chip shown when the pick landed on an imported part (issue #338):
   *  the point is on the part the user brought, not on a generated module.
   *  The mm hit point that follows is in the mono face. */
  onImportedPart: "on the part you brought",
} as const;

export const history = {
  eyebrow: "History",
  building: "building",
  onScreen: "on screen",
  current: "current",
  allVersions: "All versions",
  branchedFrom: (version: string): string => `branched from ${version}`,
  restore: (version: string): string => `Go back to ${version}`,
  branch: (version: string): string => `Branch from ${version}`,
  sharedCamera:
    "Both models turn together — one camera, so a difference you see is a real difference.",
  neverOverwritten: "nothing is ever overwritten",
  /** Everything before the four visible slots collapses into one honest count. */
  earlierCount: (count: number): string => `+${count}`,
  earlierLabel: "earlier",
  /** A version with siblings carries a fork mark and their count; the branch
   *  itself lives in the sheet. The strip is the lineage you are ON. */
  variantCount: (count: number): string => `${count} variants`,
  position: (current: number, total: number): string => `v${current} of ${total}`,
  /** Makers lose track of which version they actually printed. This mark is the
   *  useful half of the completion moment. */
  exported: "exported",
  exportedAt: (when: string): string => `exported ${when}`,
  /** The diff table's change column — one closed vocabulary, never recomputed. */
  change: {
    added: "added",
    removed: "removed",
    /** A value that is not present on that side of the table (added/removed). */
    notPresent: "—",
  },
  /** The pinned variants' mark: which one was kept, dimmed — the version's
   *  own message is the why (no separate pin_reason field exists). */
  pinnedMark: (version: string): string => `${version} · pinned`,
  /** The prefix of the imported-part version label (issue #338,
   *  decision 8): "v1 — Imported {filename}" — the version slot +
   *  dash + the word, before the mono filename span. */
  importedLabelPrefix: "v1 — Imported ",
  /** The riser graph's legend entries. The graph itself is the version graph
   *  the timeline returns: parent edges (the main line) and restored_from
   *  edges (rise-backs). */
  riserParent: "built from the previous version",
  riserRestored: "restored from an earlier version",
} as const;

export const firstRun = {
  headline: "What do you need to print?",
  body: "Describe it from scratch, or start from a file you already have and tell me what to change.",
  describeLabel: "Describe it",
  fileLabel: "Start from a file",
  placeholder: "A bracket for a 34 mm curtain rod, two M4 screws…",
  fileCardCaption:
    "The file becomes the part. I can add to it and cut from it — drill, slot, extend, split it for the bed. I can't resize what's already in it.",
  fileDropLine: "Drop an STL or 3MF here",
  fileChooseLine: "or choose a file",
  photoBtn: "Add a photo",
  photoLine:
    "A photo is different: it's a reference to design against, not the part itself.",
  start: "Start",
  startersLabel: "Or start from one of these",
  starters: [
    "A bracket for a 34\u202Fmm curtain rod, two M4 screws",
    "A spacer to lift a shelf 12\u202Fmm",
    "A knob for a 6\u202Fmm D‑shaft",
    "A channel to hide four cables along a desk edge",
  ],
  plateCaption: (x: number, y: number, z: number): string =>
    `${x} × ${y} × ${z}\u202Fmm`,
  plateNote: "the volume every design is checked against — slicing stays in Orca",
  /** The plate's caption qualifier for an unconfirmed envelope — the numbers
   *  are the API's best report, not yet checked against the machine. The
   *  deck's honest-degradation pattern: a qualifier, never a hidden plate. */
  plateCaptionUnverified: " — not yet confirmed against your machine",
} as const;

/**
 * Assumed-value confirmation (issue #250). After a passing design pass the
 * assistant offers to confirm ONE assumed value — the one that most affects
 * fit — as a single plain sentence in the conversation, never a form.
 *
 * WIRING (deck vs wire): the OFFER sentence that reaches the user is built
 * server-side (`d33d.confirm_offer.offer_sentence` — either the model's own
 * `confirm_sentence` when it passes the server's number guard, or the
 * server's deterministic template) and rides the done frame's
 * `confirm_sentence` field; the SPA renders that field VERBATIM as a plain
 * assistant message (App.tsx never calls `confirmOffer.offer` in
 * production — the offer path is server-templated). `confirmOffer.offer`
 * below exists in the deck so the deterministic offer template has a
 * single, testable home on the SPA side and so the design-contract test
 * can pin that the deck's and the server's templates match in substance
 * ("I assumed {value} for {label}. Want it different?") — it is a
 * mirror of the server template, not a second writer of the wire string.
 * The ACKNOWLEDGEMENT, by contrast, IS rendered from the deck in
 * production: App.tsx builds it from the done frame's `confirm_ack_label`
 * / `confirm_ack_value` via `confirmOffer.acknowledged` (the server also
 * carries the same sentence in the frame's `message` field as the wire
 * of record; the deck render and the wire are one string — the ack is
 * deterministic, the model never writes it).
 *
 * `value` arrives pre-formatted, and `label` is the parameter's user-facing
 * label with the raw identifier as the mono fallback.
 */
export const confirmOffer = {
  /** Tier 3 — the deterministic offer template (deck mirror of the
   *  server's `offer_sentence` template — the server is the writer of the
   *  wire string; see the module docstring above for the full wiring).
   *  `offer` is an alias kept for the #250 wiring. */
  offer: (value: string, label: string): string =>
    `I assumed ${value} for ${label}. Want it different?`,

  /** Tier 1 (issue #261): the user's message carried a relative or global
   *  cue ("taller", "bigger", "half the size") — the axis was released this
   *  turn, the new value lands assumed, and this is the first thing offered.
   *  `cue` is the first matching lexicon token, verbatim, lowercased; `value`
   *  is pre-formatted (mm() for millimetre params). */
  offerReleasedAxis: (cue: string, label: string, value: string): string =>
    `You asked for ${cue} — I made ${label} ${value}. Right?`,

  /** Tier 2 (issue #261): the user quoted an explicit mm number the lexicon
   *  did not map to an axis ("a 20 mm wide thing, lift it 12 mm" — the 12).
   *  The machine used that number for this parameter; confirm it. `value`
   *  is the mm()-formatted string of the number as the user quoted it. */
  offerUserNumber: (value: string, label: string): string =>
    `You said ${value} — I used it for ${label}. Right?`,

  /** The short acknowledgement after the user accepts the offer (no design
   *  run, no new version — the value is recorded as confirmed). */
  acknowledged: (label: string, value: string): string =>
    `Got it — ${label} stays ${value}.`,
} as const;

/**
 * The question pre-route's no-run replies (issue #260). When a
 * chat question the stage-1 filter accepts cannot be answered — the
 * stage-2 call times out, errors, returns a malformed reply, trips the
 * number guard, or says the design state does not establish the value —
 * the chat replies with one of these fixed plain messages: no
 * design run, no version. The strings are written in copy.ts and the
 * backend emits the same strings verbatim (pinned by the design-
 * contract test), so the wire and the deck are one sentence.
 */
export const answerRoute = {
  /** The stage-2 call failed (timeout, exception, malformed reply, or
   *  the number guard rejected the reply) — the honest "I could not
   *  check just now" reply. Never routes to the design loop. */
  couldNotAnswer:
    "I couldn't answer that just now — nothing was changed.",
  /** The stage-2 call succeeded and said the design state does not
   *  establish what the question asks — the honest "the design as it
   *  stands doesn't establish that" reply. Never routes to the design
   *  loop either. */
  notEstablished:
    "The design as it stands doesn't establish that — nothing was changed.",
  /** The unanswerable reply that names the missing fact (issue #278).
   *  `missing` is the short noun phrase naming the unknown fact (the
   *  backend validates it and falls back to `notEstablished` when it is
   *  invalid or absent). The backend builds the wire string from its own
   *  `UNANSWERABLE_MISSING_TEMPLATE` (the server is the writer of the
   *  wire string — the SPA renders the done frame's `answer` verbatim);
   *  the deck carries the template so the design-contract test can pin
   *  that the deck's and the server's templates match. */
  unanswerableMissing: (missing: string): string =>
    `I don't know ${missing}. Tell me and I'll check — nothing was changed.`,
  /** The no-version reply (issue #349): a question arrives on a project
   *  that has no version yet (the imported v1 counts as a version, so
   *  this fires only for projects with nothing at all). The question
   *  never starts the design loop and never creates a pending version;
   *  the backend emits this string verbatim on the done frame's `answer`
   *  field. The deck carries the same fixed string so wording drift
   *  between deck and server fails here, the #260 way. */
  nothingBuiltYet:
    "Nothing is built yet — tell me what to make first.",
  /** The bare-affirmation no-offer reply (issue #349): a clean
   *  affirmation ("yes", "ok", "sure") arrives when no offer is pending
   *  — neither a #250 param offer nor a fill-recut offer. The bare yes
   *  never starts the design loop; the backend emits this string verbatim
   *  on the done frame's `answer` field. The deck carries the same fixed
   *  string so wording drift between deck and server fails here, the #260
   *  way. */
  nothingWaitingForYes:
    "There's nothing waiting for a yes right now — what would you like to change?",
} as const;

/**
 * Deterministic axis-size answer copy (issue #263).
 *
 * The new stage between stage 1 and stage 2 in `route_chat_message`
 * answers "how tall is it?" / "what's the depth?" / "how big is it?"
 * deterministically from the design state — no LLM call. The backend
 * builds the wire string using its own `mm_formatted` (which replicates
 * `mm()` here) and emits it verbatim on the done frame's `answer` field.
 *
 * The deck carries the templates so the design-contract test can pin
 * the exact strings and catch a wording drift between deck and server,
 * the #260 / #250 way.
 *
 * `axis` is the adjective the question used: "tall" (H), "wide" (W),
 * or "deep" (D). `value`, `stated`, and `measured` arrive pre-formatted
 * via `mm()`. `w`, `d`, and `h` in the dimension-list sentence are
 * each pre-formatted or the literal "-" (dash) when not established.
 */
export const deterministicAnswer = {
  /** Stated + measured agree: "It's 12.0 mm tall — you said that, and I
   *  measured it." */
  statedAndMeasured: (value: string, axis: string): string =>
    `It's ${value} ${axis} — you said that, and I measured it.`,

  /** Measured only: "It measures 12.0 mm tall." */
  measuredOnly: (value: string, axis: string): string =>
    `It measures ${value} ${axis}.`,

  /** Stated only (no measurement yet):
   *  "You said 12.0 mm tall. Nothing has measured it yet." */
  statedOnly: (value: string, axis: string): string =>
    `You said ${value} ${axis}. Nothing has measured it yet.`,

  /** Disagrees: "You said 12.0 mm; what came out measures 43.8 mm." */
  disagrees: (stated: string, measured: string): string =>
    `You said ${stated}; what came out measures ${measured}.`,

  /** Not established: "The height isn't established yet." (width/depth)
   *  `axisNoun` is the noun form: "height", "width", "depth". */
  notEstablished: (axisNoun: string): string =>
    `The ${axisNoun} isn't established yet.`,

  /** Dimension list: "It measures 60.0 mm × 45.0 mm × 12.0 mm." Each
   *  axis uses its best value (pre-formatted) or "-" when not established. */
  dimensionList: (w: string, d: string, h: string): string =>
    `It measures ${w} × ${d} × ${h}.`,

  /** Comparison met (issue #313, issue #320): "Yes — it measures 40.0 mm
   *  deep, 10.0 mm more than 30 mm." `measured`, `delta`, and `target`
   *  arrive pre-formatted via `mm()` — the delta is the absolute
   *  shortfall or surplus, and the target is the number the user named.
   *  `relation` is the TRUE number relation between measured and target:
   *  "more than" when measured > target, "less than" when measured <
   *  target — it follows the sign of measured − target, independent of
   *  the Yes/No the direction decides (issue #320 retired "short of"). */
  comparisonYes: (
    measured: string,
    axis: string,
    delta: string,
    target: string,
    relation: string,
  ): string =>
    `Yes — it measures ${measured} ${axis}, ${delta} ${relation} ${target}.`,

  /** Comparison not met (issue #313, issue #320): "No — it measures 45.0
   *  mm deep, 5.0 mm less than 50 mm." `relation` carries the same
   *  sign-true relation as `comparisonYes` — a No answer can say
   *  "more than" (fit direction) and a Yes answer can say "less than"
   *  ("shorter than 50 mm" at 12 mm). */
  comparisonNo: (
    measured: string,
    axis: string,
    delta: string,
    target: string,
    relation: string,
  ): string =>
    `No — it measures ${measured} ${axis}, ${delta} ${relation} ${target}.`,

  /** Within tolerance (issue #313): "About the same — it measures 30.2 mm
   *  deep." Used for both the borderline comparison and the named-axis fit
   *  case ("will it fit in a 45 mm deep gap?"). */
  comparisonAboutTheSame: (measured: string, axis: string): string =>
    `About the same — it measures ${measured} ${axis}.`,

  /** Missing-fact comparison (issue #313): "How tall is the shelf?
   *  The part is 102.0 mm tall." The question half names the other
   *  object (the backend validates it); the answer half states only the
   *  part's own measured value — never a value the design state does not
   *  establish. `axisNoun` is "tall" / "wide" / "deep" (the adjective,
   *  matching the other axis templates). */
  comparisonMissingFact: (
    axisAdjective: string,
    object: string,
    measured: string,
  ): string =>
    `How ${axisAdjective} is ${object}? The part is ${measured} ${axisAdjective}.`,

  /** The size-unknown reply (issue #352, operator decision 1): a
   *  dimension question on a part whose units are unsettled — the
   *  backend's `UNSETTLED_SIZE_REPLY` carries the same sentence (the
   *  design-contract test pins the two-way agreement, the #260 way).
   *  No number is ever emitted for an unsettled part: the v1 import's
   *  file-unit bbox is not an mm measurement until the units are
   *  settled. The route's unsettled pre-route already pre-empts every
   *  message on an unsettled part; this sentence is the deterministic
   *  stage's own guard (defence in depth). */
  sizeUnknownWhileUnsettled:
    "I can't give you a size until the part's units are settled — pick mm, cm, or inch (or give one measured axis) and the dimensions will be real.",
} as const;

export const shell = {
  addPhoto: "Add a reference photo",
  /** The photo surface's no-project state (issue #282): the photo is
   *  chosen before any project exists — creation is in flight or failed. */
  noProject: "No project selected",
  /** The shared project-creation failure card (issue #282): send and
   *  photo paths surface the SAME copy — the failure is about the
   *  project, never about the upload itself. */
  projectCreationFailed: (reason: string): string =>
    `Failed to create project: ${reason}`,
  composerPlaceholder:
    "Ask for a change, or click the model to point at a part",
  send: "Send",
  export: "Export 3MF",
  exportVersion: (version: string): string => `Export ${version}`,
  /** The static lead-in for the export note (issue #338). The component
   *  appends the filename in its own mono `<span>` (never interpolated into
   *  a sentence string) so a hostile filename stays inert text. */
  exportContainsGeometryPrefix: "contains geometry from ",
  /** Only rendered once validation has actually passed. Never a default. */
  validated: (checks: number): string =>
    `Watertight · in millimetres · ${checks} checks passed`,
  /** The end: the file is named, and what to do with it is said once. d33d stops
   *  at geometry; Orca takes it from here. */
  exportDone: (filename: string): string =>
    `${filename}. Open it in Orca — it's already in millimetres and oriented flat.`,
  exportFilename: (project: string, version: string): string =>
    `${project.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}-${version}.3mf`,

  /** Chat collapsed to its rail keeps the last summary legible. */
  conversationCollapsed: (messages: number): string => `${messages} messages`,
  openConversation: "Open the conversation",
  collapseConversation: "Collapse the conversation",
  hideAllPanels: "Hide every panel",
  /** Below the adaptive threshold the conversation docks to the bottom
   *  of the window so it can no longer sit over the build plate (issue
   *  #194): the caption is a status line in the docked bar's header. */
  conversationDocked:
    "Conversation docked to the bottom — the build plate stays clear.",
  /** Below the floor we say so plainly rather than degrading. */
  viewportTooSmall:
    "This needs a window at least 1024 × 640 to show the model and the conversation at once.",
  /** The viewer's "nothing yet" state (issue #107): shown when no model has
   *  been streamed. Distinct from the in-progress state (the design-loop
   *  progress surface) and from a failure (the selection notice) — this is
   *  simply "empty so far", not an error and not work in flight. */
  viewerEmpty: "Your model will appear here once a design is generated.",

  resetView: "Reset view",
  showDimensions: "Show dimensions",
  showPhoto: "Show the reference photo",
} as const;

/**
 * 3MF export failure copy (issue #233). The export failure surface maps
 * the download's `error_class` to a sentence — validation-gate classes
 * reuse `failure.reasons`; these two cover the classes that have no entry
 * there (no new keys go into that map — it belongs to the design loop).
 */
export const export3mf = {
  /** `error_class: "conflict"` (the 409: a design pass is still in
   *  flight — the version being created is not yet exportable). */
  conflict:
    "A design is still being made — export it once that finishes.",

  /** `error_class: "units_unsettled"` (the 409: an imported part's units
   *  are not yet settled — the 3MF cannot be exported until the user
   *  settles the unit). Issue #325. */
  unitsUnsettled:
    "The part's units aren't settled yet — settle them to export a 3MF.",

  /** The generic fallback: an unlisted/`unknown` class, no `error_class`
   *  (the 404 bodies), or a network failure with no class at all. */
  failed: "The 3MF couldn't be exported.",
} as const;

/**
 * Fill-and-recut boundary copy (issue #332, sub-issue 3). When a project
 * has an assumed/settled part and the user asks to RESIZE or MOVE one of
 * the part's own features (a closed-set noun that is NOT a param of the
 * design's own current version), the chat replies with ONE of these fixed
 * sentences — the noun and any number arrive from the USER'S OWN words
 * (substituted, never invented). The backend builds the wire string from
 * its own templates (`d33d.fill_recut`'s `FRILL_*_REPLY` constants) and
 * the SPA renders the done frame's `message` verbatim; the deck carries
 * the templates so the parity test can pin that the deck's and the
 * server's sentences match in substance (the same two-way pin the
 * `confirmOffer` and `deterministicAnswer` decks carry).
 *
 * `noun` is the closed-set feature noun (hole, slot, boss, …).
 * `dim` / `distance` arrive mono-formatted (the user's own number, the
 * `:g` spelling — `38` or `38.5`). `direction` is the user's own
 * where-word (left, right, up, …).
 */
export const fillRecut = {
  /** The point-at-the-spot move reply (a move request with no distance).
   *  `noun` is the user's own closed-set feature noun. */
  move: (noun: string): string =>
    `That ${noun} came with your file, so I can't move it directly — the file has no parameters for me to change. What I can do: fill it, then cut a new one where you want it. Point at the spot, or tell me where.`,

  /** The move-with-distance reply (a move request carrying the user's
   *  own distance + direction — the number and direction are kept, never
   *  dropped). `distance` is mono-formatted; `direction` is the user's
   *  own where-word. */
  moveWithDistance: (noun: string, distance: string, direction: string): string =>
    `That ${noun} came with your file, so I can't move it directly — the file has no parameters for me to change. What I can do: fill it, then cut a new one ${distance} mm ${direction} of where it is now. It'll look the same, and you'll see it as a change in the history.`,

  /** The hole/bore diameter resize reply (the UX spec's sentence —
   *  `dim` is the user's own number, mono-formatted). */
  holeDiameter: (noun: string, dim: string): string =>
    `That ${noun} came with your file, so I can't resize it directly — the file has no parameters for me to change. What I can do: fill it, then cut a Ø${dim} mm one on the same axis. It'll look the same, and you'll see it as a change in the history.`,

  /** The other-noun resize reply (`dim` is the user's own number,
   *  mono-formatted). */
  nounDimension: (noun: string, dim: string): string =>
    `That ${noun} came with your file, so I can't resize it directly — the file has no parameters for me to change. What I can do: fill it, then cut a new ${noun} at ${dim} mm in the same place. It'll look the same, and you'll see it as a change in the history.`,

  /** The no-dimension ask (the user named a feature but no size). */
  noDimension: (noun: string): string =>
    `How big should the ${noun} be? It came with your file, so I'll fill it and cut a new one at that size.`,

  /** The quiet decline acknowledgement (a clean "no" on the pending
   *  fill-recut offer). */
  declined: "Understood — leaving the part as it is.",

  /** The no-normal degradation reply (issue #338, decision 6): a region
   *  edit that triggers the boundary WITHOUT a face normal gets NO
   *  axis-dependent offer — the reply says the feature came with the
   *  file, then this sentence. No offer is stored, no buttons, no loop.
   *  The backend's FRILL_NO_NORMAL_REPLY carries the same sentence
   *  (the parity pin in tests/test_projects.py). */
  noNormal:
    "That feature came with your file, so I can't resize it directly — the file has no parameters for me to change. I can't tell that feature's axis from where you pointed — pin a flat face on it and I'll offer to fill it and recut it on the same axis.",

  /** The no-hole honest reply (issue #351): the user asked to resize a
   *  hole/bore/counterbore on a part whose stored hole_count is 0.
   *  No offer is stored, no buttons, no loop. The noun is the user's own
   *  closed-set feature noun (substituted, never invented). The backend's
   *  FRILL_NO_HOLE_REPLY carries the same template (the parity pin in
   *  tests/test_projects.py). */
  noHole: (noun: string): string =>
    `I don't see a ${noun} on the part you brought — want me to drill one?`,

  /** The fill-and-recut offer's acceptance button (issue #338, decision
   *  7): sends the acceptance through the existing chat offer path, which
   *  runs the loop. */
  offerYes: "Yes, do that",
  /** The fill-and-recut offer's decline button (issue #338, decision 7):
   *  clears the pending offer, no loop. */
  offerNo: "Leave it",
} as const;

/**
 * The unsettled-part chat reply (issue #332, sub-issue 3). When the
 * project has a part whose units are NOT assumed/settled, the chat
 * replies with this fixed sentence before any design loop runs — the
 * units are not settled; the loop must wait for the user to settle
 * them. The backend's `UNSETTLED_PART_REPLY` carries the same sentence
 * (the parity test pins the two-way agreement, the #299 way).
 */
export const partUnitsUnsettled =
  "The part's units aren't settled yet, so I can't work on it. Settle the units first — pick mm, cm, or inch, or give one measured axis — and then I can add and cut on it.";

/**
 * Part upload rejection copy (issue #325). When the backend rejects a
 * part upload, it returns a 422 with `detail` carrying one of these exact
 * sentences; the SPA surfaces `detail` verbatim (the #299 way). The
 * design-contract tripwire pins the two-way agreement.
 */
export const partUpload = {
  /** The chat-pane drop-area label (the part upload surface, distinct from
   *  the first-run drop area's `firstRun.fileDropLine`). */
  dropLine: "Drop an STL or 3MF here",
  /** The reference-photo drop-area label (issue #347, decision 1): the
   *  clip glyph was removed — text-only, in the deck per the user-string
   *  contract (it was previously inlined in PhotoUpload.tsx). */
  photoAttachLine: "Attach reference photo",
  /** The 400 `detail` body: an unsupported content type / extension. */
  unsupported:
    "That file type isn't supported. Upload an STL or 3MF mesh.",
  /** The 422 `detail` body: an unparseable / empty / non-finite / over-cap
   *  mesh (or a 3MF zip-bomb). The SPA shows it verbatim. */
  unparseable:
    "That file isn't a readable mesh. Check it opens in another 3D tool and try again.",
  /** The 500 `detail` body: the part could not be saved (commit / settle
   *  failure). FIXED sentence — the backend's exception text (paths, git
   *  output) never reaches the client; it stays in the server log only. */
  commitFailed:
    "The part couldn't be saved. Nothing was changed.",
  /** The upload-in-flight status line (the label swaps to it while the
   *  part upload is in flight). */
  uploading: "Uploading…",
} as const;

/**
 * Screen 2 copy (issue #334, sub-issue 4): the part read report and
 * the unit-settlement UI. All strings pinned by design-contract.test.ts.
 */
export const partReport = {
  /** "I read {filename}" — the report header. */
  iRead: (filename: string): string => `I read ${filename}`,
  /** The W/D/H row when units are unsettled. Never a number. */
  waitingOnUnits: "waiting on units",
  /** The assumed-mm line: "I read it as millimetres: {W} × {D} × {H} mm. If it's in inches, tell me." */
  assumedLine: (w: string, d: string, h: string): string =>
    `I read it as millimetres: ${w} × ${d} × ${h}. If it's in inches, tell me.`,
  /** The one-tap change link (issue #350: it opens the unit choice +
   *  measurement escape — the same surface the unsettled card uses). */
  changeUnits: "Change the units",
  /** The one-line note shown when the server sent no unit options and the
   *  user opens the choice anyway (issue #350): only the measurement
   *  escape is available — never a bare escape with no explanation. */
  assumedNoOptionsLine: "There's no unit to pick from here.",
  /** The unit option labels. */
  unitLabels: {
    inch: "Inches",
    cm: "Centimetres",
    mm: "Millimetres — it really is that small",
  } as Record<string, string>,
  /** The escape line. */
  escapeLine:
    "Or tell me one real measurement — “the base is 60 mm wide” — and I'll scale from that.",
  /** The escape input placeholder. */
  escapePlaceholder: "e.g. the base is 60 mm wide",
  /** The unsettled viewport caption. */
  unsettledCaption: "The shape is known. Its size isn't, until the units are.",
  /** The axis labels for the escape input. */
  axisLabels: { W: "Width", D: "Depth", H: "Height" } as Record<string, string>,
  /** The settle button. */
  settle: "Settle",
  /** The 409 part_exists detail (re-import attempt). */
  partExists: "This project already has a part.",
  /** The watertight-with-gaps form: "watertight, after closing {n} small gap(s)". */
  watertightGaps: (n: number): string =>
    `watertight, after closing ${n} small gap${n === 1 ? "" : "s"}`,
  /** Issue #375: the dropped-body line, shown only when the report carries
   *  `bodies_before` (repair dropped a body): "N bodies → M after repair". */
  bodiesAfterRepair: (before: number, after: number): string =>
    `${before} bod${before === 1 ? "y" : "ies"} → ${after} bod${
      after === 1 ? "y" : "ies"
    } after repair`,
} as const;

/**
 * Missing-storage copy (issue #295). Two distinct sentences:
 * - `photoMissing`: the fixed copy.ts string the backend emits on the
 *   SSE `notice` frame (a non-terminal frame before the terminal
 *   done/error frame) when the project's stored reference photo was lost
 *   out-of-band (path set, file gone — never a photo-LESS project). The
 *   SPA renders it as a plain assistant message in the transcript.
 * - `sourceMissing`: the sentence the restore / branch 409 with detail
 *   code `source_missing` maps to — the SPA maps the 409 body's
 *   `{ code: "source_missing" }` to this text (never the raw detail
 *   message), the same deck-home pattern as the #260 no-run replies.
 *
 * The chat pre-route's saved-design-missing reply is the backend's own
 * wire string (emitted verbatim on the done frame — the SPA never
 * substitutes its own copy for it), so only the 409 mapping and the
 * notice live here.
 */
export const missingStorage = {
  /** The SSE `notice` frame's fixed string (issue #295): the backend
   *  emits this verbatim when the project's stored reference photo is
   *  lost out-of-band (path set, file gone). The SPA renders it as a
   *  plain assistant message in the transcript, before the pass/failure
   *  turn. The design-contract tripwire pins the two-way agreement with
   *  the backend's `PHOTO_MISSING_NOTICE` (d33d/projects.py).
   *  Never a fabricated value, never a digit. */
  photoMissing:
    "Your reference photo for this project is missing, so I'm designing " +
    "from your words alone",

  /** The 409 `source_missing` 409 detail code (restore / branch-from): the
   *  target version's recorded design source — or the project's git repo —
   *  is absent from disk. Same honest statement the chat pre-route makes.
   *  Never the raw backend message, never a fabricated value. */
  sourceMissing:
    "The saved design for this project is missing, so I can't change it. Start a new design, or describe it again and I'll make it fresh",

  /** The restore-specific 409 `source_missing` sentence (issue #295): the
   *  restore action cannot recreate the missing source, so the sentence says
   *  what the user can still keep (the versions list + its measurements).
   *  Distinct from `sourceMissing` (chat + Brief) by design — the two
   *  surfaces make two different honest statements. */
  restoreSourceMissing:
    "The saved design for that version is missing, so I can't restore it. The versions list and its measurements are still here",
} as const;

/**
 * Reference-photo upload rejection copy (issue #299). When the backend
 * rejects an upload because the bytes are not a decodable image, it
 * returns a 422 with `detail` carrying this exact sentence; the SPA
 * surfaces `detail` verbatim (throwFor/PhotoUpload render it as-is, the
 * #295 way), and the design-contract tripwire pins the two-way agreement
 * so a wording drift between the deck and the wire fails the test.
 */
export const photoUpload = {
  /** The 422 `detail` body the upload route returns for bytes that are
   *  not a readable PNG or JPEG. The SPA shows it verbatim — never
   *  paraphrased, never a status code. */
  undecodable:
    "That file isn't a readable PNG or JPEG image. Try exporting it again.",
} as const;

export const copy = {
  brief,
  passCard,
  firstPass,
  progress,
  failure,
  confirmOffer,
  answerRoute,
  deterministicAnswer,
  region,
  history,
  firstRun,
  shell,
  export3mf,
  missingStorage,
  photoUpload,
  partUpload,
  partReport,
  fillRecut,
  partUnitsUnsettled,
} as const;

export default copy;
