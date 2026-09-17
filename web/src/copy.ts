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

  legend: {
    stated: "you said",
    measured: "measured off the model",
    unknown: "unknown",
    disagrees: "disagrees",
  },

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

  /** Above ~7 rows the Brief groups by part. Unknowns are never grouped away —
   *  they are the thing to act on, so they surface out of the list. */
  unresolvedHeading: (count: number): string => `Unresolved · ${count}`,
  allParameters: (count: number): string => `All ${count} parameters`,
  groupCount: (count: number): string => `${count} groups`,
  groupSummary: (count: number, note: string): string => `${count} · ${note}`,
} as const;

export const passCard = {
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
    body: (actualMm: number, limitMm: number): string =>
      `It came out ${mm(actualMm)} across and the plate is ${mm(limitMm)}. Orca can't slice around this — the part itself has to get smaller, or come apart.`,
    overhang: (byMm: number): string => `${mm(byMm)} past the edge`,
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
    syntax_error: "The generated design had a syntax error, so nothing was built.",
    empty_model: "The design produced an empty model — there is nothing to print.",
    artifact_error: "The model file came out unreadable.",
    timeout: "The render ran out of time. A simpler shape will get through.",
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

  /** After TWO failures on the same goal, stop offering "Try again" and ask.
   *  Repeating an offer that has already failed twice is how you lose someone.
   *  This REPLACES the action set; it does not sit alongside it. */
  askInstead: (goal: string): string =>
    `Twice now I haven't got ${goal} right, and a third go the same way is unlikely to land. Tell me what I'm getting wrong and I'll start from that instead.`,

  rawDisclosure: "What the checker actually said",
  genericRetry:
    "Something went wrong on the way there. Try again — if it keeps happening, the detail below is the useful part.",
} as const;

export const region = {
  placeholder: "Change this part… e.g. “open the top so the rod drops in”",
  apply: "Apply",
  cancel: "Cancel selection",
  resolvedTo: "this point is on the",
  poseHint: "Turning the model clears the pin — it only means this from here.",
  cleared:
    "Pin cleared, the view changed. Turn to the angle you want, then click the model.",
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
} as const;

export const firstRun = {
  headline: "What do you need to print?",
  body: "Describe the part and the measurements you actually know. Everything here is in millimetres — say “3\u202Fcm” and it will be read as 30\u202Fmm.",
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
  /** Below the floor we say so plainly rather than degrading. */
  viewportTooSmall:
    "This needs a window at least 1024 × 640 to show the model and the conversation at once.",

  resetView: "Reset view",
  showDimensions: "Show dimensions",
  showPhoto: "Show the reference photo",
} as const;

export const copy = {
  brief,
  passCard,
  firstPass,
  progress,
  failure,
  region,
  history,
  firstRun,
  shell,
} as const;

export default copy;
