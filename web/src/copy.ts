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

  /** Above ~7 rows the Brief groups by part. Unknowns are never grouped away —
   *  they are the thing to act on, so they surface out of the list. */
  unresolvedHeading: (count: number): string => `Unresolved · ${count}`,
  allParameters: (count: number): string => `All ${count} parameters`,
  groupCount: (count: number): string => `${count} groups`,
  groupSummary: (count: number, note: string): string => `${count} · ${note}`,
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

  expectation: "Usually 15–30\u202Fs. You will see it change.",
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

  /** One sentence per closed-set reason. errorMapping.ts keeps the mapping; this
   *  holds the words. The map must stay total — every GATE_REASON_BITS value and
   *  every render ErrorClass value has an entry. */
  reasons: {
    bbox_out_of_tolerance:
      "It came out a different size from the one you asked for.",
    stated_dims_not_named_parameters:
      "The dimensions you gave didn't end up as parameters, so the next change would not be able to hold them.",
    views_blank_or_missing: "It built, but the preview images came out blank.",
    error_class_not_ok: "The design step produced nothing usable.",
    ok: "The design step produced nothing usable.",
    syntax_error: "The generated design had a syntax error, so nothing was built.",
    empty_model: "The design produced an empty model — there is nothing to print.",
    artifact_error: "The model file came out unreadable.",
    timeout: "The render ran out of time. A simpler shape will get through.",
    design_loop_timed_out:
      "The design loop stopped responding — it ran past its time limit.",
    oom: "The model was too heavy to render. A simpler shape will get through.",
    container_error:
      "The render environment failed. That is temporary — try again.",
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
  /** The riser graph's legend entries. The graph itself is the version graph
   *  the timeline returns: parent edges (the main line) and restored_from
   *  edges (rise-backs). */
  riserParent: "built from the previous version",
  riserRestored: "restored from an earlier version",
} as const;

export const firstRun = {
  headline: "What do you need to print?",
  body: "Describe the part and the measurements you actually know. Everything here is in millimetres — give dimensions in mm: “30 mm”, not “3 cm”.",
  placeholder: "A bracket to hold a 34 mm curtain rod 45 mm off the wall…",
  start: "Start",
  startersLabel: "Or start from one of these",
  starters: [
    "A bracket for a 34\u202Fmm curtain rod, two M4 screws",
    "A spacer to lift a shelf 12\u202Fmm",
    "A knob for a 6\u202Fmm D‑shaft",
    "A channel to hide four cables along a desk edge",
  ],
  photoHint:
    "Got the thing it has to fit? Drop a photo in and say how wide something in it is — that is what turns a picture into millimetres.",
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
  /** The deterministic offer template (deck mirror of the server's
   *  `offer_sentence` template — the server is the writer of the wire
   *  string; see the module docstring above for the full wiring). */
  offer: (value: string, label: string): string =>
    `I assumed ${value} for ${label}. Want it different?`,

  /** The short acknowledgement after the user accepts the offer (no design
   *  run, no new version — the value is recorded as confirmed). */
  acknowledged: (label: string, value: string): string =>
    `Got it — ${label} stays ${value}.`,
} as const;

export const shell = {
  addPhoto: "Add a reference photo",
  composerPlaceholder:
    "Ask for a change, or click the model to point at a part",
  send: "Send",
  export: "Export 3MF",
  exportVersion: (version: string): string => `Export ${version}`,
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
  /** The generic fallback: an unlisted/`unknown` class, no `error_class`
   *  (the 404 bodies), or a network failure with no class at all. */
  failed: "The 3MF couldn't be exported.",
} as const;

export const copy = {
  brief,
  passCard,
  firstPass,
  progress,
  failure,
  confirmOffer,
  region,
  history,
  firstRun,
  shell,
  export3mf,
} as const;

export default copy;
