/**
 * App shell — the full-viewport stage (issue #119).
 *
 * The 3D canvas fills the window; every panel floats above it and costs
 * no layout height. The stage is a single positioned container
 * (`.app-stage`, position:relative, 100vw × 100vh, overflow:hidden) with
 * the ModelViewer absolute inset:0, sized from its ResizeObserver — the
 * handle's `renderer.getSize()` is THE single source of the viewport's
 * CSS-pixel size (the pick layer derives its box from the same element).
 *
 * Six presentational surfaces are extracted to their own components
 * (issue #116): Brief (components/brief/Brief.tsx), PassCard
 * (components/chat/PassCard.tsx), Composer (components/chat/Composer.tsx),
 * PassProgress (components/progress/PassProgress.tsx), FailureCard
 * (components/failure/FailureCard.tsx) and Filmstrip
 * (components/versions/Filmstrip.tsx). App keeps all state and API wiring;
 * the components are driven by props. PassCard is a presentational shell
 * (no markup to extract — its content arrives with W10); its import here
 * is the design-contract assertion that App holds none of its markup.
 *
 * Four layers, one ever modal (design contract, issue #119): canvas 0;
 * brief/header/filmstrip/controls 10; conversation 20; pin+bar 30.
 * A failure is CONTENT INSIDE THE CONVERSATION, not a layer.
 *
 * No auto-generated slider panel. (The opt-in pinned-parameter strip was
 * superseded by the Brief — issue #123 deleted it; the Brief is the
 * always-visible answer to what we are building.)
 *
 * Project lifecycle (task-e wiring): this is a single-operator tool, not
 * a multi-project dashboard yet, so the shell creates one project on
 * mount ("get or create default project" would need a list/select UI
 * that's out of scope here) and holds its id in state. Every component
 * that needs a projectId (PhotoUpload, Export3MF, the chat stream) waits
 * for that id before doing anything real.
 */

import { useState, useCallback, useEffect, useRef } from "react";
import { Vector2 } from "three";
import { dataUriToArrayBuffer } from "./lib/dataUri";
import {
  ChatPanel,
  type ChatMessage,
  type ChatMessageSelection,
} from "./components/chat/ChatPanel";
import { PhotoUpload } from "./components/upload/PhotoUpload";
import { ModelViewer, type ModelViewerHandle, type LoadResult } from "./components/viewer/ModelViewer";
import { PickLayer } from "./components/viewer/PickLayer";
import { resolvePointPick } from "./components/viewer/ModelViewer";
import { DimensionCanvas } from "./components/canvas/DimensionCanvas";
import { Export3MF } from "./components/export/Export3MF";
import { compositeMarkedPng, stripDataUrlPrefix } from "./lib/markedPng";
import { MARKER_COLOR } from "./lib/marker";
import { displayDesignLoopError, type DisplayError } from "./lib/errorMapping";
import {
  ApiClient,
  type RegionEditViewId,
  type VersionTimelineEntry,
  type VersionCompare,
  type DesignStateEntry,
  type Envelope,
} from "./lib/api";
import copy from "./copy";
import type { RenderImage } from "./lib/renderImage";
import { Brief } from "./components/brief/Brief";
import { PassCard } from "./components/chat/PassCard";
import { Composer } from "./components/chat/Composer";
import { PassProgress } from "./components/progress/PassProgress";
import {
  INITIAL_VIEW_PROGRESS,
  reduceViewProgress,
  type ViewProgressState,
} from "./lib/viewProgress";
import { FailureCard } from "./components/failure/FailureCard";
import { Filmstrip } from "./components/versions/Filmstrip";
import { FirstRun } from "./components/firstrun/FirstRun";
import { PlateBackdrop } from "./components/firstrun/PlateBackdrop";
// Composer is rendered via ChatPanel (its form lives there) — App holds
// none of its markup. The design-contract assertion (W8) requires App to
// import all six surface components, so both imports below are kept (the
// void statement keeps the linter honest about them).
void [PassCard, Composer];

// The version surfaces (issue #8): the horizontal filmstrip (W13) is the
// at-a-glance strip; the history sheet (W16, HistorySheet) is the
// expanded overlay the strip's expand mark opens — the home of the
// compare, restore and pin actions. The transitional VersionTail rail
// (issue #117 adversarial fix) is gone: every one of those actions is
// reachable from the sheet.
import { HistorySheet } from "./components/versions/HistorySheet";

// Overlay geometry (issue #119). The overlays are siblings above the
// canvas, each inset OVERLAY_INSET_PX from the stage edge. z-index is a
// CLOSED set of four values (the design contract): canvas 0, panels 10,
// conversation 20, pin+bar 30 — no other z-index may exist.
const OVERLAY_INSET_PX = 24;
const Z_INDEX = { canvas: 0, panels: 10, conversation: 20, pinAndBar: 30 } as const;

// Responsive rules (issue #119). The floor is measured against the
// WINDOW (window.innerWidth/innerHeight — NOT the stage's own box): below
// it the app says so plainly instead of degrading. The Brief renders as a
// chip below 1200px wide OR 820px tall (height is the real constraint on
// a laptop).
const FLOOR_WIDTH_PX = 1024;
const FLOOR_HEIGHT_PX = 640;
const BRIEF_CHIP_MAX_WIDTH_PX = 1200;
const BRIEF_CHIP_MAX_HEIGHT_PX = 820;

// RenderImage now lives in lib/renderImage.ts (issue #116 — the shared
// shape App and the pass-card surface both need); re-exported here for
// existing consumers (ChatPanel, app-layout.test.tsx import from App).
export type { RenderImage };

/**
 * A point selection that has been picked (raycast hit + optional resolved
 * module id) and composited into a marked PNG, but has NOT yet been sent
 * as a region edit — the user must still supply the free-text instruction
 * via the inline bar (see `handlePointSelected` / `handleSendMessage`).
 * Carries everything `RegionEditRequest` needs except `instruction`.
 */
interface PendingRegionSelection {
  /** The composited red-marked view PNG as a data URL (for the chat
   *  thumbnail) — `stripDataUrlPrefix`'d again when building the request. */
  thumbnail: string;
  viewId: RegionEditViewId;
  moduleIds: string[];
  point: { x: number; y: number };
}

interface AppProps {
  /** Injectable API client (test seam). Defaults to a same-origin ApiClient. */
  client?: ApiClient;
}
export default function App({ client }: AppProps) {
  const apiClient = useRef(client ?? new ApiClient()).current;

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  // The build envelope (GET /api/config/envelope) — the first-run plate
  // backdrop's caption and the plate's drawn dimensions both come from this
  // fetch, never from a literal in the SPA (issue #128, W14). Null until the
  // fetch resolves; the plate renders only once it has.
  const [envelope, setEnvelope] = useState<Envelope | null>(null);
  const [photoSrc, setPhotoSrc] = useState<string | null>(null);
  const [photoDimensions, setPhotoDimensions] = useState<{ width: number; height: number } | null>(
    null,
  );
  const [streamError, setStreamError] = useState<DisplayError | null>(null);
  // The last user message text, for the Retry control (issue #82).
  const lastUserMessageRef = useRef<string>("");
  const [designLoopInFlight, setDesignLoopInFlight] = useState(false);
  // The current design-loop progress step (issue #82): updated by the
  // onProgress handler so the stage indicator can show a human label.
  // Reset to null on completion/error. Unknown/empty values render the
  // generic label, never the raw token.
  const [designLoopStep, setDesignLoopStep] = useState<string | null>(null);
  // Elapsed seconds since the design loop started (issue #82). Updated by
  // a 1s interval while in flight; reset on completion/error. Cleaned up
  // on unmount AND completion to avoid leaked intervals.
  const [designLoopElapsed, setDesignLoopElapsed] = useState(0);
  // The per-view progress state (issue #118): reduced from the real
  // render-view-start / render-view-done SSE frames — the honest "N of 6"
  // counter. Only those frames advance it (no timer, no interpolation);
  // a new design-loop iteration resets it (the attempt is announced via
  // the state, never a silent "view 9 of 6"). Reset to idle on completion
  // and error so the counter never survives the pass that fed it.
  const [viewProgress, setViewProgress] = useState<ViewProgressState>(
    INITIAL_VIEW_PROGRESS,
  );
  // The start timestamp (ms) of the current design loop, used to compute
  // elapsed seconds. Held in a ref (not state) so the interval can read
  // the current value without re-running the effect.
  const designLoopStartRef = useRef<number | null>(null);

  // Version timeline (issue #8) — the side rail. Loaded when the project
  // resolves (opening a project RESUMES the chat at its latest version
  // with the timeline as a side rail — the project home is the
  // conversation and the design together).
  const [versions, setVersions] = useState<VersionTimelineEntry[]>([]);
  const [compareIds, setCompareIds] = useState<[number, number] | null>(null);
  // The history sheet (W16): null while closed, the version id it was
  // opened from while open. The sheet is an OVERLAY over the canvas — not
  // a route, not a page (there is no router in the app); the filmstrip's
  // expand mark opens it and its close button closes it.
  const [sheetOpenFor, setSheetOpenFor] = useState<number | null>(null);
  // The project's display name — the export filename's slug source
  // (copy.shell.exportFilename reads it; the App creates the project on
  // mount and knows it, so the filename is real, never a guess).
  const [projectName, setProjectName] = useState("untitled project");

  // The design-state block (issue #120 / #123) — the Brief's data. Fetched
  // once the project exists and re-fetched on every version-created frame
  // (the single trigger — a new version means the block may have changed).
  // A failure leaves the last good block in place (never a crash, never a
  // fake value).
  const [designState, setDesignState] = useState<DesignStateEntry[]>([]);

  const refetchDesignState = useCallback(() => {
    if (projectId === null) return;
    apiClient
      .getDesignState(projectId)
      .then(setDesignState)
      .catch(() => {
        // A failed fetch leaves the last good block in place — the Brief
        // describes what it last knew, never a made-up value.
      });
  }, [projectId, apiClient]);

  useEffect(() => {
    refetchDesignState();
  }, [refetchDesignState]);

  // Responsive shell state (issue #119). The floor and the Brief chip
  // threshold are measured against the WINDOW (window.innerWidth/Height —
  // NOT the stage's own box): below the floor the app says so plainly
  // (copy.shell.viewportTooSmall) instead of degrading; the Brief renders
  // as a chip below 1200px wide OR 820px tall.
  const [windowSize, setWindowSize] = useState(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
  }));
  // The conversation collapses to a RAIL by choice at any size, keeping
  // the last summary legible (copy.shell.conversationCollapsed).
  const [conversationCollapsed, setConversationCollapsed] = useState(false);
  // Backslash hides EVERY panel (copy.shell.hideAllPanels) — bound at the
  // window level so it fires regardless of focus, but IGNORED while an
  // input/textarea/contenteditable has focus (typing a backslash into the
  // composer would blank the interface).
  const [panelsHidden, setPanelsHidden] = useState(false);

  useEffect(() => {
    const onResize = () =>
      setWindowSize({ width: window.innerWidth, height: window.innerHeight });
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // The backslash key: hide every panel (issue #119). window-level so
  // focus does not matter — but IGNORED while an input, textarea or
  // contenteditable has focus, and while a panel is hidden, backslash
  // RESTORES them (one key, two states, no modal stack).
  useEffect(() => {
    const isTypingTarget = (t: EventTarget | null): boolean => {
      if (!(t instanceof HTMLElement)) return false;
      const tag = t.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || t.isContentEditable;
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "\\") return;
      if (isTypingTarget(e.target)) return; // typing wins over the shortcut
      setPanelsHidden((prev) => !prev);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const viewportTooSmall =
    windowSize.width < FLOOR_WIDTH_PX || windowSize.height < FLOOR_HEIGHT_PX;
  // The Brief is a CHIP below 1200px wide OR 820px tall (height is the
  // real constraint on a laptop) — a chip, not a full panel.
  const briefIsChip =
    windowSize.width < BRIEF_CHIP_MAX_WIDTH_PX ||
    windowSize.height < BRIEF_CHIP_MAX_HEIGHT_PX;

  // Region-selection (point pick) wiring (issue #98).
  const viewerHandleRef = useRef<ModelViewerHandle | null>(null);
  // Stream-driven model (issue #69): once a design-loop pass streams its
  // best render's STL through the version-created progress frame
  // (`stl_data_uri`), the viewer mounts the decoded STL ArrayBuffer
  // (format "stl"). Stays null until the first stream-driven pass —
  // the pre-pass state is the EMPTY viewer (issue #107: no fixture model
  // is ever shown; a fresh project starts from a clean slate).
  //
  // ONE-WAY LATCH: once set, it is never reset to null. A later pass whose
  // frame omits stl_data_uri does not restore a previous model — the
  // streamed model stays mounted for the session. This is intentional
  // (a streamed pass is a terminal state for the viewer's source); a
  // future ticket that needs to unmount the model must add an explicit
  // reset path here, not an implicit one.
  const [streamModelData, setStreamModelData] = useState<ArrayBuffer | null>(null);
  const [selectionNotice, setSelectionNotice] = useState<string | null>(null);
  // Camera-pose signature captured at onReady time (position + the
  // OrbitControls target). The pick layer is view-dependent: a marker is
  // composited onto ONE specific rendered view, so when the pose drifts
  // past POSE_EPS_MM (incidental damping jitter) the pending selection is
  // cleared with a visible cue — re-compositing from a new view would
  // silently change the image being sent.
  const lastPoseRef = useRef<string | null>(null);
  // Free-text instruction typed into the inline bar while a point selection
  // is pending. Kept in local state (not the chat input) so the bar's own
  // Apply/Enter can submit it through the same handleSendMessage path the
  // chat panel uses. Reset on every pick/cancel/submit so a stale draft
  // never leaks into a later selection's instruction.
  const [regionBarText, setRegionBarText] = useState("");
  // The viewer's mounted data (issue #69 / #107), derived ONCE from the
  // single source of truth (streamModelData): a streamed STL, or an
  // EXPLICIT null when nothing has been streamed — the viewer's empty
  // state, never a stand-in model. The format is "stl" in both branches
  // (the only model that ever reaches the viewer is a streamed STL; the
  // GLB loader remains available to ModelViewer and its tests, but no
  // production source feeds it anymore).
  const viewerSource = streamModelData
    ? { data: streamModelData, format: "stl" as const }
    : { data: null, format: "stl" as const };
  // A point selection that has been picked (marked PNG) but not yet sent
  // — the instruction is the user's own free text, which the server
  // requires non-empty (`RegionEditRequest.instruction`,
  // `Field(min_length=1)`). The selection is attached to the NEXT chat
  // message the user sends (or the inline bar's submit), not fired
  // immediately from the click.
  const [pendingSelection, setPendingSelection] = useState<PendingRegionSelection | null>(
    null,
  );
  // Whether the pick layer is live: a model has actually been loaded into
  // the scene (onReady re-fires with a non-null modelRoot at exactly that
  // moment — the e2e `data-ready` wait flips on this). Null before the
  // first load, after a failed load, or when no model is mounted.
  const [pickLayerReady, setPickLayerReady] = useState(false);
  // The visible red dot in the DOM, at the picked CSS-pixel location. The
  // pending selection IS the marker's state: one click → one marker,
  // cleared by cancel/Escape/submit/orbit-cleared.
  const pickMarker = pendingSelection ? pendingSelection.point : null;
  // Bumped on every draw/cancel/send-clear of pendingSelection (see the
  // three setPendingSelection call sites below). A createRegionEdit
  // rejection captures the generation at send time and only restores the
  // selection if nothing has touched pendingSelection since — otherwise a
  // slow/failed request for an OLD selection could silently clobber a NEWER
  // one the user drew afterward, or resurrect one they explicitly cancelled.
  const pendingSelectionGenerationRef = useRef(0);

  // The region bar's anchoring (issue #129): the bar is anchored to the pin,
  // not to the viewport's bottom edge. `viewportSize` is the bar's sizing
  // source — driven by the stage element (the same element ModelViewer sizes
  // from via its ResizeObserver), so there is no second size constant.
  const stageRef = useRef<HTMLDivElement | null>(null);
  const [viewportSize, setViewportSize] = useState<{ width: number; height: number }>(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
  }));
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const measure = () => setViewportSize({ width: el.clientWidth, height: el.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  // The pin's live "orbiting" (orbit gesture in flight) and "cleared" (pose
  // crossed POSE_EPS_MM, the pin is being torn down) states. Both are
  // cleared on the next pick / cancel / submit (the new pin starts clean).
  const [orbitingPin, setOrbitingPin] = useState(false);
  const [orbitClearedPin, setOrbitClearedPin] = useState(false);

  // The camera-pose signature (position + OrbitControls target, in mm)
  // for the view-dependent selection guard. The threshold exists because
  // OrbitControls damping drifts the pose a fraction of a mm even on a
  // stationary viewport — the selection must survive that jitter, but a
  // real orbit (any mm-scale move) invalidates the composited view.
  const POSE_EPS_MM = 0.5;
  const poseSignature = useCallback((handle: ModelViewerHandle): string => {
    const c = handle.camera.position;
    const t = handle.controls.target;
    return `${c.x},${c.y},${c.z},${t.x},${t.y},${t.z}`;
  }, []);

  // Clear a pending point selection (with a visible cue) when the camera
  // pose has moved beyond POSE_EPS_MM since the selection was made — the
  // marker is composited onto one specific rendered view, and re-sending
  // it after an orbit would mislead the vision model. Incidental jitter
  // (damping drift, sub-eps moves) does NOT clear it.
  const handleCameraMoved = useCallback(() => {
    if (pendingSelection === null) return;
    const handle = viewerHandleRef.current;
    if (!handle) return;
    const sig = poseSignature(handle);
    const prev = lastPoseRef.current ?? sig;
    if (sig === prev) return;
    // Compare numerically — the string embeds the full floats; parse and
    // distance-check so POSE_EPS_MM is honoured. (The string identity
    // check above is the fast path: unchanged pose, no work.)
    const [px, py, pz, tx, ty, tz] = sig.split(",").map(Number);
    const [ox, oy, oz, otX, otY, otZ] = prev.split(",").map(Number);
    const moved =
      Math.abs(px - ox) + Math.abs(py - oy) + Math.abs(pz - oz) +
      Math.abs(tx - otX) + Math.abs(ty - otY) + Math.abs(tz - otZ);
    if (moved <= POSE_EPS_MM) {
      lastPoseRef.current = sig; // absorb the jitter, keep the selection
      return;
    }
    lastPoseRef.current = sig;
    pendingSelectionGenerationRef.current += 1;
    setPendingSelection(null);
    setRegionBarText("");
    // The pin is cleared — the bar's inline hint takes over (the demoted
    // version of the old "notice card" surface; W15: it is expected
    // behaviour, not an error, so it lives in the bar's hint slot).
    setOrbitClearedPin(true);
    setOrbitingPin(false);
    setSelectionNotice(copy.region.cleared);
  }, [pendingSelection, poseSignature]);

  // ModelViewer's onReady fires on mount and again whenever the loaded
  // model root changes — replace (never merge) the captured handle on
  // every call so a remount or model swap never leaves a stale raycaster
  // (or a stale modelRoot) behind. The modelRoot is also the pick layer's
  // liveness signal: picking is live exactly when a model is in the scene.
  const handleViewerReady = useCallback(
    (handle: ModelViewerHandle) => {
      viewerHandleRef.current = handle;
      lastPoseRef.current = poseSignature(handle);
      setPickLayerReady(handle.modelRoot !== null);
      handleCameraMoved();
    },
    [poseSignature, handleCameraMoved],
  );

  // Gesture-start seam (issue #129): OrbitControls fires `start` at the
  // first pose move of a gesture, BEFORE POSE_EPS_MM is crossed. The pin
  // desaturates at that moment ("about to go", not "gone"); the bar dims
  // alongside. Clearing on the same gesture's `end` is not wired (that
  // seam does not exist yet); instead the cleared state takes over when
  // the threshold IS crossed (the pin is genuinely gone) or when the
  // user cancels/submits a new pick.
  const handleOrbitStart = useCallback(() => {
    if (pendingSelection === null) return;
    setOrbitingPin(true);
  }, [pendingSelection]);
  const handleViewerLoaded = useCallback(
    (result: LoadResult) => {
      if (result.ok && result.mesh) {
        // Picking works on every loaded model — named modules (the GLB
        // fixture) yield a module-id bonus via resolvePointPick; unnamed
        // streamed STL is still fully selectable, grounded by the marked
        // PNG alone. Nothing here disables the pick on format.
        setSelectionNotice(null);
        return;
      }
      // A load failure (decode/parse error) is distinct from "still
      // loading" — both leave no model root (the pick layer is disabled
      // via `ready`), but only a failure should tell the user *why*
      // picking is unavailable instead of leaving them to wonder if it
      // will ever appear.
      setSelectionNotice(
        result.error
          ? `Model failed to load — picking unavailable: ${result.error}`
          : "Model failed to load — picking unavailable.",
      );
    },
    [],
  );

  // Decode the streamed design-loop STL: the version-created progress frame
  // carries `stl_data_uri` (the best iteration's STL as a base64 data URI)
  // plus a `views` map of data URIs. Mount the stream-derived STL in the
  // viewer (the pre-pass state is the empty viewer — issue #107, no
  // fixture). A streamed pass renders a single unnamed mesh, so picks on it
  // resolve no module ids — selection still works, grounded by the marked
  // PNG alone (issue #98). (SCAD ownership is untouched — onToken stays the
  // sole SCAD carrier.) The streamed source is a one-way latch (see
  // streamModelData): a later frame that omits stl_data_uri does not
  // restore a previous model.
  const handleStreamViewerData = useCallback(
    (data: Record<string, unknown>) => {
      const stlDataUri = typeof data.stl_data_uri === "string" ? data.stl_data_uri : null;
      if (stlDataUri === null) return;
      try {
        setStreamModelData(dataUriToArrayBuffer(stlDataUri));
      } catch (e) {
        // A corrupt data URI must not break the stream turn — keep the
        // exception visible in the console and surface the same notice
        // channel used for load failures (never a crash). The pick is
        // unchanged (no new model mounted), so the notice names the decode
        // failure only.
        console.error("failed to decode streamed STL data URI", e);
        setSelectionNotice(
          "Could not decode the streamed model — the displayed model is unchanged.",
        );
      }
    },
    [],
  );

  // A single click on the viewport (issue #98): raycast the CSS-pixel
  // point through the live camera. A geometry HIT (named or unnamed)
  // places the marker and opens the inline instruction bar; a miss (a
  // click on empty background) does NOT select — a marker on empty
  // background grounds nothing and would mislead the vision model, so a
  // brief notice is surfaced instead.
  const handlePointSelected = useCallback(
    (event: { point: { x: number; y: number } }) => {
      const handle = viewerHandleRef.current;
      if (!handle) {
        setSelectionNotice(
          "Model not loaded yet — click the model again once it appears.",
        );
        return;
      }
      const modelRoot = handle.modelRoot;
      if (!modelRoot) {
        // No model loaded yet — distinct from "the click missed geometry".
        setSelectionNotice(
          "Model not loaded yet — click the model again once it appears.",
        );
        return;
      }

      const canvas = handle.renderer.domElement;
      // CSS-pixel viewport size, NOT canvas.width/canvas.height (the
      // WebGL drawing-buffer size, which renderer.setPixelRatio scales by
      // devicePixelRatio). PickLayer's points come from
      // getBoundingClientRect() — always CSS pixels — so the NDC conversion
      // in resolvePointPick must divide by the same CSS-pixel dimensions or
      // every raycast mis-registers on any DPR!==1 display.
      const cssSize = handle.renderer.getSize(new Vector2());

      // resolvePointPick and compositeMarkedPng can throw on unexpected
      // failures (notably compositeMarkedPng's `canvas.getContext("2d")`
      // returning null on context loss, exhausted canvas contexts, or
      // headless quirks). There is no ErrorBoundary in this app, so an
      // uncaught throw here would unmount the whole React tree — losing
      // chat history and project state — instead of degrading like the
      // existing "nothing selected" path. Catch and route through the
      // same selection-notice channel.
      try {
        const pick = resolvePointPick(
          event.point,
          cssSize.x,
          cssSize.y,
          handle.camera,
          handle.raycaster,
          modelRoot,
        );

        if (!pick.hit) {
          // Click missed all geometry — never a marker, never a bar. A
          // marker on empty background would actively mislead the vision
          // model.
          setSelectionNotice("Click on the model to point at a part.");
          return;
        }

        const moduleIds = pick.module ? [pick.module] : [];
        // compositeMarkedPng's point argument is in the same CSS-pixel
        // space as event.point (PickLayer records via
        // getBoundingClientRect()), so it needs the same CSS-pixel cssSize
        // used for the raycast above to scale into the canvas's
        // drawing-buffer pixel space.
        const markedPngBase64 = compositeMarkedPng(canvas, event.point, cssSize.x, cssSize.y);

        setSelectionNotice(null);
        setRegionBarText("");
        // A new pick resets the orbit state — the previous pin's "cleared"
        // or "orbiting" flag must not leak into this one (the new pin is a
        // fresh state: full colour, no dim, no hint).
        setOrbitClearedPin(false);
        setOrbitingPin(false);

        // The server requires a non-empty free-text `instruction`
        // (`RegionEditRequest.instruction`, `Field(min_length=1)`) that only
        // the user can supply. Stash the picked selection and the point/view
        // needed to build the request, and surface the inline bar for the
        // instruction. The pending selection is attached to whichever chat
        // message the user sends next (see handleSendMessage), or via the
        // bar's own Apply/Enter path.
        pendingSelectionGenerationRef.current += 1;
        setPendingSelection({
          thumbnail: `data:image/png;base64,${markedPngBase64}`,
          viewId: "front",
          moduleIds,
          point: event.point,
        });
      } catch (e) {
        const detail = e instanceof Error ? e.message : "unknown error";
        setSelectionNotice(`Selection failed — could not process the click: ${detail}`);
      }
    },
    [],
  );

  const handleCancelPendingSelection = useCallback(() => {
    pendingSelectionGenerationRef.current += 1;
    setPendingSelection(null);
    setRegionBarText("");
    setOrbitClearedPin(false);
    setOrbitingPin(false);
  }, []);

  // Issue #125: the pass card's enlarged-view action. The frame carries
  // PNG thumbnails (no geometry), so there is nothing to swap in — the
  // button's honest job is closing the enlargement (the card's own
  // state; the streamed model is already beside the photo in the stage
  // the whole time). This hook exists so the seam is wired and a ticket that
  // carries view geometry on the frame can do a real swap here.
  const handleBesidePhoto = useCallback(() => {}, []);

  // Create the (single, default) project on mount. Once it resolves,
  // load the version timeline (the side rail) — the project resumes at
  // its latest version.
  useEffect(() => {
    let cancelled = false;
    apiClient
      .createProject({ name: "untitled project" })
      .then((project) => {
        if (!cancelled) {
          setProjectId(project.id);
          setProjectName(project.name);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setStreamError({
            message: `Failed to create project: ${
              e instanceof Error ? e.message : "unknown error"
            }`,
            detail: e instanceof Error ? e.message : undefined,
            retryable: false,
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load the version timeline once the project exists (the resume state).
  useEffect(() => {
    let cancelled = false;
    if (projectId === null) return;
    apiClient
      .listVersions(projectId)
      .then((vs) => {
        if (!cancelled) setVersions(vs);
      })
      .catch(() => {
        // A fresh project has an empty timeline — no error to surface
        // (the timeline component renders its own empty state).
        if (!cancelled) setVersions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, apiClient]);

  // The build envelope (issue #128) — fetched once on mount, independent of
  // the project id (it is machine config, not project state). A failure
  // leaves the plate unrendered (no caption, no drawing) — a confident value
  // the SPA has not established must never be invented.
  useEffect(() => {
    let cancelled = false;
    apiClient
      .getEnvelope()
      .then((env) => {
        if (!cancelled) setEnvelope(env);
      })
      .catch(() => {
        // Envelope fetch failed — the plate stays hidden rather than
        // displaying a number the SPA has not established.
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Timeline callbacks (issue #8): restore (non-destructive forward
  // version), pin (the gallery), compare-select (the two-viewport compare).
  const handleVersionRestore = useCallback(
    async (versionId: number) => {
      if (projectId === null) return;
      try {
        await apiClient.restoreVersion(projectId, versionId);
        // Refresh the timeline (a new forward version was created).
        const vs = await apiClient.listVersions(projectId);
        setVersions(vs);
      } catch (e) {
        setStreamError({
          message: `Restore failed: ${
            e instanceof Error ? e.message : "unknown error"
          }`,
          detail: e instanceof Error ? e.message : undefined,
          retryable: false,
        });
      }
    },
    [projectId, apiClient],
  );

  const handleVersionPin = useCallback(
    async (versionId: number, pinned: boolean) => {
      if (projectId === null) return;
      try {
        await apiClient.updateVersion(projectId, versionId, { pinned });
        setVersions((prev) =>
          prev.map((v) => (v.id === versionId ? { ...v, pinned: pinned } : v)),
        );
      } catch (e) {
        setStreamError({
          message: `Pin failed: ${e instanceof Error ? e.message : "unknown error"}`,
          detail: e instanceof Error ? e.message : undefined,
          retryable: false,
        });
      }
    },
    [projectId, apiClient],
  );

  // Compare-select: pick two versions to compare (the prioritized surface).
  // Each click drops the older of the two selections and keeps the newest,
  // so a 3-click sequence rotates A→B→C→B→A→…
  const handleCompareSelect = useCallback((versionId: number) => {
    setCompareIds((prev) => {
      if (!prev) return [versionId, versionId];
      // Drop the older selection; keep the newest pair.
      return [prev[1], versionId];
    });
  }, []);

  // The compare view (two viewports + the param diff table) — fetched once
  // two distinct versions have been selected (the prioritized surface).
  const [compareResult, setCompareResult] = useState<VersionCompare | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (projectId === null || compareIds === null) {
      setCompareResult(null);
      setCompareError(null);
      return;
    }
    // Skip the fetch when both ids are the same (the first click sets
    // [id, id] as a pending selection without a network call).
    if (compareIds[0] === compareIds[1]) {
      setCompareResult(null);
      setCompareError(null);
      return;
    }
    setCompareError(null);
    apiClient
      .compareVersions(projectId, compareIds[0], compareIds[1])
      .then((res) => {
        if (!cancelled) setCompareResult(res);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setCompareResult(null);
          setCompareError(
            `Compare failed: ${e instanceof Error ? e.message : "unknown error"}`,
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, apiClient, compareIds]);

  const handleSendMessage = useCallback(
    (text: string) => {
      // Never call createRegionEdit with an empty or whitespace-only
      // instruction — the server rejects it (`Field(min_length=1)`), and
      // an all-whitespace string would pass a naive truthiness check but
      // still be meaningless as an edit instruction.
      const trimmed = text.trim();

      // Remember the last plain chat message for the Retry control (issue #82).
      lastUserMessageRef.current = text;

      // Bail before constructing/appending anything if there's no project to
      // send to — a message (and any attached selection) must never render
      // as sent when the region-edit request that would justify it can
      // never fire. Checked ahead of selectionToAttach/userMsg construction
      // so a pending selection is never displayed as "submitted" while
      // still sitting untouched in state.
      if (projectId === null) {
        setStreamError({
          message: "No project selected",
          detail: undefined,
          retryable: false,
        });
        return;
      }

      const selectionToAttach = trimmed.length > 0 ? pendingSelection : null;

      const userMsg: ChatMessage = {
        id: `msg-${Date.now()}`,
        role: "user",
        content: text,
        ...(selectionToAttach
          ? {
              selection: {
                thumbnail: selectionToAttach.thumbnail,
                viewId: selectionToAttach.viewId,
                moduleIds: selectionToAttach.moduleIds,
              } satisfies ChatMessageSelection,
            }
          : {}),
      };
      setMessages((prev) => [...prev, userMsg]);

      // Start the design-loop timer (issue #82): the elapsed-seconds counter
      // starts when the request is sent. The 1s interval runs while
      // designLoopInFlight is true (the flag is set just below), so the
      // timer starts on send and is torn down on completion/error.
      designLoopStartRef.current = Date.now();
      setDesignLoopElapsed(0);

      if (selectionToAttach) {
        // Clear immediately so a slow createRegionEdit response can't race a
        // second send into re-attaching the same pending selection. Snapshot
        // the generation counter first so the reject handler below can tell
        // whether the user drew a new selection or clicked cancel while this
        // request was in flight.
        const sentGeneration = pendingSelectionGenerationRef.current;
        pendingSelectionGenerationRef.current += 1;
        setPendingSelection(null);
        // W12: a region-edit failure is a turn in the conversation, like
        // any other stream failure — append it to messages, not a card.
        const turnId = `msg-${Date.now()}-failure`;
        void apiClient
          .createRegionEdit(projectId, {
            module_ids: selectionToAttach.moduleIds,
            view_id: selectionToAttach.viewId,
            marked_png_base64: stripDataUrlPrefix(selectionToAttach.thumbnail),
            point: selectionToAttach.point,
            instruction: trimmed,
          })
          .then(() => {
            // 202 Accepted means the request was validated and the design
            // loop was queued in the background — NOT that any
            // regeneration happened (`RegionEditResult` is exactly
            // `{project_id, status: "accepted"}`, mirroring /chat; the
            // version arrives only via the SSE stream, see api.ts's
            // createRegionEdit doc comment). Without this, a successful
            // request produced zero feedback, indistinguishable from a
            // silent failure or a request still in flight. Word it so it
            // can never read as a completed edit.
            setMessages((prev) => [
              ...prev,
              {
                id: `msg-${Date.now()}-region-edit-accepted`,
                role: "assistant",
                content:
                  "Region edit request accepted — the design loop is running in the background; a new version will appear in the timeline when it passes.",
              },
            ]);
          })
          .catch((e) => {
            // The request failed (network error, or a 4xx/5xx from the
            // server) after the pending selection was already cleared —
            // restore it so the user doesn't have to re-pick, and
            // say plainly that THIS is what failed (the chat message
            // above already shows the selection thumbnail as sent, so a
            // generic error would leave that looking correct).
            //
            // Only restore if the generation counter is UNCHANGED since this
            // request started (i.e. the send's own +1 is still the latest
            // bump) — this request's selectionToAttach is a value closed over
            // at send time. If the user drew a new selection or clicked cancel
            // while this request was in flight, the counter has moved on and
            // this stale value must NOT win: an unconditional (or a merely
            // current-is-null) overwrite here would silently clobber a newer
            // selection, or resurrect one the user explicitly cancelled, with
            // no way for the user to tell the difference.
            const detail = e instanceof Error ? e.message : "unknown error";
            if (pendingSelectionGenerationRef.current === sentGeneration + 1) {
              setPendingSelection(selectionToAttach);
            }
            // The region-edit failure is a turn in the conversation (W12):
            // the message says what failed (with the selection restored if
            // the generation is unchanged), and the action set is the
            // generic one (no envelope-specific actions for a request that
            // never reached the design loop).
            const msg =
              pendingSelectionGenerationRef.current === sentGeneration + 1
                ? `Region edit failed — selection restored, please resend: ${detail}`
                : `Region edit failed: ${detail}`;
            setMessages((prev) => {
              const withoutPrevious = prev.filter((m) => m.failure === undefined);
              return [
                ...withoutPrevious,
                {
                  id: turnId,
                  role: "assistant" as const,
                  content: "",
                  failure: { message: msg, detail, retryable: false },
                },
              ];
            });
          });
      }

      const assistantId = `msg-${Date.now()}-assistant`;
      setMessages((prev) => [
        ...prev,
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);

      // Set the in-flight flag (disables the send button) BEFORE the
      // postChat call — the design loop runs in a background task and the
      // flag must be set synchronously to prevent a second send from
      // racing the first.
      setDesignLoopInFlight(true);

      // Collect the last 10 user messages for chat_history (the SPA
      // in-memory state — the transcripts table is NOT populated by this
      // ticket; that is a future seam).
      const chatHistory = messages
        .filter((m) => m.role === "user")
        .slice(-10)
        .map((m) => m.content);

      void apiClient
        .postChat(projectId, {
          message: trimmed,
          chat_history: chatHistory,
        })
        .then(() => {
          // The event source is registered synchronously before the 202
          // response — the SSE stream will find it. Open the stream now.
          return apiClient.streamEvents(projectId, {
            onToken: (text, _data) => {
              // Issue #125 (W10): the token frame carries the generated
              // source. It is the pass card's DISCLOSURE content, not a
              // chat message — appending it to the message text is the
              // "a hundred lines of OpenSCAD in the transcript" defect
              // this item removes. The streaming state machine is
              // untouched: onDone/onError still clear `streaming`.
              if (text.length > 0) {
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantId ? { ...m, source: (m.source ?? "") + text } : m,
                  ),
                );
              }
            },
            onProgress: (step, data) => {
              handleStreamViewerData(data);
              // Issue #82: the step name was previously discarded ("_step").
              // The stage indicator uses it to show a human-readable label.
              // design-loop-pass fires once per iteration (up to 3×); setting
              // an identical value is a no-op state update (React bails out),
              // so repeated steps do not reset the elapsed timer or flicker.
              if (typeof step === "string") setDesignLoopStep(step);
              // Issue #118: the per-view frames (render-view-start /
              // render-view-done, each carrying `view` + `iteration`) drive
              // the honest counter. Only those steps reach the reducer
              // (any other step is ignored), and only those frames can
              // advance the count — a hung render freezes the counter at
              // its last real value (stream death clears the whole surface
              // via the terminal error frame).
              if (step === "render-view-start" || step === "render-view-done") {
                const view = typeof data.view === "string" ? data.view : "";
                const iteration =
                  typeof data.iteration === "number" ? data.iteration : 0;
                if (view !== "") {
                  setViewProgress((prev) =>
                    reduceViewProgress(prev, step, view, iteration),
                  );
                }
              }
              // Issue #125 (W10): the version-created frame carries the
              // pass's `views` map (data URIs, one per view). Attach them
              // to the in-flight assistant message — it is the turn that
              // produced the version, and ChatPanel renders that turn as
              // the PassCard.
              if (step === "version-created") {
                const viewsRaw = data.views;
                if (viewsRaw && typeof viewsRaw === "object") {
                  const views = Object.entries(viewsRaw).map(([filename, src]) => ({
                    filename,
                    src: typeof src === "string" ? src : "",
                  }));
                  const versionId =
                    typeof data.version_id === "number" ? data.version_id : null;
                  setMessages((prev) =>
                    prev.map((m) =>
                      m.id === assistantId
                        ? { ...m, versionId, views: views.length > 0 ? views : [] }
                        : m,
                    ),
                  );
                }
              }
              // Issue #114: the version-created frame is the single trigger
              // for refetching the timeline — not every progress frame
              // (that would hammer the endpoint).
              if (step === "version-created") {
                void apiClient.listVersions(projectId).then(setVersions);
                // Issue #123: a new version means the design-state block the
                // Brief renders may have changed — refetch it on the same
                // single trigger.
                refetchDesignState();
              }
            },
            onDone: (data) => {
              setMessages((prev) =>
                prev.map((m) => {
                  if (m.id !== assistantId) return m;
                  // The done frame's `message` is the loop's result prose
                  // ("Design loop passed validation") — the pass card's
                  // summary line. The message's content was seeded "" on
                  // send, so a REAL done message is always the summary:
                  // an error/infra frame travels the error path (onError),
                  // and the only thing that could land in content first is
                  // the postChat rejection's "Error: …" text, which this
                  // guard refuses to overwrite. Nothing else (token text
                  // arrives on `source` only) can reach content, so no
                  // guard is needed to keep source out of the summary.
                  const msg = typeof data.message === "string" ? data.message : "";
                  const isReal = msg.length > 0 && !msg.startsWith("Error:");
                  return {
                    ...m,
                    streaming: false,
                    ...(isReal && m.content === "" ? { content: msg } : {}),
                  };
                }),
              );
            },
            onError: (data) => {
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m)),
              );
              // The structured `reason` (when present) is mapped to plain
              // language; a missing reason is an infra failure (or a legacy
              // frame) — generic copy, never a gate mapping (issue #82).
              // The envelope limits (from GET /api/config/envelope, already
              // fetched for the plate) enable the failure turn's measured
              // number for the envelope gate — never a literal (W12).
              const failure = displayDesignLoopError(
                data,
                envelope === null ? undefined : [envelope.x, envelope.y, envelope.z],
              );
              // W12: a failure is a TURN in the conversation — it is
              // appended to `messages` (rendered by the ChatPanel), not a
              // card beside it. A new failure replaces the previous turn, so
              // the conversation never stacks failure turns.
              const turnId = `msg-${Date.now()}-failure`;
              setMessages((prev) => {
                const withoutPrevious = prev.filter((m) => m.failure === undefined);
                return [
                  ...withoutPrevious,
                  { id: turnId, role: "assistant" as const, content: "", failure },
                ];
              });
            },
          });
        })
        .catch((e) => {
          // postChat failed (404, 409, 422, network error) — the design
          // loop did not start. Show the error and re-enable the send
          // button.
          const detail = e instanceof Error ? e.message : "unknown error";
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId ? { ...m, streaming: false, content: `Error: ${detail}` } : m,
            ),
          );
          // W12: a stream failure is a TURN in the conversation — append
          // it to `messages` (ChatPanel renders it as a FailureTurn), not
          // a card beside the panel.
          const turnId = `msg-${Date.now()}-failure`;
          setMessages((prev) => {
            const withoutPrevious = prev.filter((m) => m.failure === undefined);
            return [
              ...withoutPrevious,
              { id: turnId, role: "assistant" as const, content: "", failure: {
                  message: "The request could not be sent. The design did not start — you can retry.",
                  detail,
                  retryable: true,
                } },
            ];
          });
        })
        .finally(() => {
          // Release the in-flight flag on ALL exit paths (pass, exhausted,
          // exception, network error). The server also clears its flag in
          // a finally, but the client-side flag is what disables the send
          // button (issue #82: also tear down the elapsed-seconds timer —
          // no leaked interval, no stage indicator after the run).
          setDesignLoopInFlight(false);
          designLoopStartRef.current = null;
          setDesignLoopStep(null);
          setViewProgress(INITIAL_VIEW_PROGRESS);
        });
    },
    [projectId, apiClient, pendingSelection, messages, envelope, handleStreamViewerData, refetchDesignState],
  );

  // The inline bar's submit path — routes the typed instruction through the
  // SAME handleSendMessage the chat panel uses, so the pending selection is
  // attached and createRegionEdit fires exactly once, with the same
  // empty/whitespace guard, generation race-guard, and error handling as a
  // chat send (no duplicated request logic). handleSendMessage trims the
  // text; a whitespace-only draft therefore also cannot fire a request, and
  // an empty draft never even reaches it (guard below).
  // The export's designed ending (issue #126). Fires ONLY on a successful
  // download (Export3MF calls it after the bytes are in the browser):
  //   1. the conversation gains its final assistant turn — the file is
  //      named and the handover to Orca is said once
  //      (copy.shell.exportDone, copy.shell.exportFilename);
  //   2. the version actually exported is marked in the filmstrip —
  //      server-side (POST …/versions/{id}/export), so the mark survives
  //      a page reload; a failed or cancelled download never reaches here,
  //      so it can neither append the turn nor set the mark.
  // The completion turn is CLIENT session state — a reload re-fetches the
  // versions and the mark, but not the transcript; the mark, not the turn,
  // is the half that must outlive the session (a maker three days later
  // asks which version they printed).
  const handleExported = useCallback(
    async (versionId: number | undefined) => {
      if (projectId === null || versionId === undefined) return;
      const version = versions.find((v) => v.id === versionId);
      const versionLabel = version?.name ?? `v${versionId}`;
      const filename = copy.shell.exportFilename(projectName, versionLabel);
      setMessages((prev) => [
        ...prev,
        {
          id: `msg-${Date.now()}-export-done`,
          role: "assistant",
          content: copy.shell.exportDone(filename),
        },
      ]);
      try {
        await apiClient.recordExport(projectId, versionId);
        // The mark is now server state — pick it up in the timeline so the
        // filmstrip shows it in THIS session, not only after a reload.
        const vs = await apiClient.listVersions(projectId);
        setVersions(vs);
      } catch {
        // The download already happened — the 3MF is in the browser and the
        // completion turn is appended. Only the mark is missing; say so
        // plainly rather than pretend the export itself failed.
        setStreamError({
          message: "The export was downloaded, but the exported mark could not be saved.",
          detail: undefined,
          retryable: false,
        });
      }
    },
    [projectId, projectName, versions, apiClient],
  );

  // Elapsed-seconds timer for the design-loop stage indicator (issue
  // #82): runs only while a plain chat design loop is in flight, so the
  // indicator's counter is accurate and no interval leaks on completion
  // or unmount. `designLoopInFlight` is false for region-edit sends, so
  // the indicator (and this interval) never run for those.
  useEffect(() => {
    if (!designLoopInFlight) return;
    const tick = () => {
      const start = designLoopStartRef.current;
      if (start !== null) {
        setDesignLoopElapsed(Math.max(0, Math.floor((Date.now() - start) / 1000)));
      }
    };
    tick(); // seed immediately — no 1s dead time before the first display
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [designLoopInFlight]);

  const handleRegionBarSubmit = useCallback(() => {
    const text = regionBarText;
    if (text.trim().length === 0) return;
    setRegionBarText("");
    handleSendMessage(text);
  }, [regionBarText, handleSendMessage]);

  const handlePhotoUploaded = useCallback((photoPath: string, width: number, height: number) => {
    // Photo upload success — the photo path is now stored server-side.
    // The chat panel picks up the new photo context on next interaction.
    // Also feed the DimensionCanvas so the operator can draw a scale
    // anchor against the uploaded reference photo, using its real pixel
    // dimensions (not a hardcoded guess) for coordinate mapping.
    setPhotoSrc(photoPath);
    setPhotoDimensions({ width, height });
  }, []);


  // Below the floor the app says so plainly rather than degrading (issue
  // #119): copy.shell.viewportTooSmall replaces the stage's content.
  if (viewportTooSmall) {
    return (
      <div
        className="app-stage"
        data-testid="app-stage"
        style={{
          position: "relative",
          width: "100vw",
          height: "100vh",
          overflow: "hidden",
        }}
      >
        <div
          data-testid="viewport-too-small"
          role="alert"
          style={{
            position: "absolute",
            top: "50%",
            left: "50%",
            transform: "translate(-50%, -50%)",
            maxWidth: "80%",
            textAlign: "center",
            color: "var(--color-fg)",
            fontFamily: "var(--font-ui)",
          }}
        >
          {copy.shell.viewportTooSmall}
        </div>
      </div>
    );
  }

  return (
    <div
      className="app-stage"
      data-testid="app-stage"
      ref={stageRef}
      style={{
        // 100vw/100vh per the ticket's own text (`.app-stage` — width:100vw;
        // height:100vh; overflow:hidden). The overflow:hidden means the
        // scrollbar gutter that 100vw includes on some platforms never
        // causes a visible overflow — the stage simply clips to the window.
        position: "relative",
        width: "100vw",
        height: "100vh",
        margin: 0,
        overflow: "hidden",
      }}
    >
      {/* Layer 0 — the canvas fills the stage; the pick layer shares its
          exact box (both absolute inset:0). The viewer's size comes only
          from its ResizeObserver; the handle's renderer.getSize() is the
          single source of the CSS-pixel viewport size (issue #119).
          The pre-pass state is the EMPTY viewer (issue #107): no model
          until a stream-driven pass delivers its STL. */}
      <div
        className="viewer-pane"
        data-testid="viewer-pane"
        style={{
          position: "absolute",
          inset: 0,
          zIndex: Z_INDEX.canvas,
        }}
      >
        {/* The viewer's pre-pass state is the named-module GLB fixture.
         * After a stream-driven design-loop pass (issue #69) it is
         * REPLACED by the design loop's rendered STL (streamModelData,
         * from the version-created frame's stl_data_uri) so the browser
         * displays the model the loop actually produced. */}
        <ModelViewer
          data={viewerSource.data}
          format={viewerSource.format}
          onReady={handleViewerReady}
          onLoaded={handleViewerLoaded}
          onOrbitStart={handleOrbitStart}
        />
        {/* Single-click pick layer (issue #98) — fills the stage, the SAME
            element box the viewer's ResizeObserver reads (issue #119). */}
        <PickLayer
          ready={pickLayerReady}
          marker={pickMarker}
          dimmed={orbitingPin || orbitClearedPin}
          onPointSelected={handlePointSelected}
        />
      </div>

      {/* Layer 10 — the first-run screen (issue #128, W14). Shown while the
          project has no versions and no message has been sent — the
          moment the user has no idea what to type. The build plate is
          drawn to scale behind it; the screen goes away once the
          conversation starts. Hidden with the other panels on backslash. */}
      {versions.length === 0 && messages.length === 0 && !panelsHidden && (
        <FirstRun
          onSend={handleSendMessage}
          onPhotoSelect={() => {
            // The photo button routes to the left pane's file input — the
            // same photo path as everywhere else. A label[htmlFor] click
            // natively triggers the file picker (a hidden input's .click()
            // does not fire change without a file chosen, and the input is
            // display:none).
            const label = document.querySelector<HTMLLabelElement>(
              'label[htmlfor="photo-file-input"]',
            );
            if (label) label.click();
          }}
          inFlight={designLoopInFlight}
        />
      )}

      {/* Layer 10 — the build-plate backdrop (issue #128, W14). Drawn to
          scale from the envelope the API reports; behind the first-run
          column, very low contrast — the constraint as a room, not a
          warning. The keep-out notch is NOT drawn: the envelope route
          does not expose it, and this surface must not infer it. */}
      {envelope !== null && !panelsHidden && (
        <PlateBackdrop x={envelope.x} y={envelope.y} z={envelope.z} verified={envelope.verified} />
      )}

      {/* Layer 20 — the conversation (chat + upload + errors). Floats over
          the canvas; costs no layout height. Collapses to a RAIL by
          choice (conversationCollapsed) — the last summary stays legible.
          Hidden by backslash (panelsHidden). */}
      {!panelsHidden && (
        <div
          className="app-left"
          data-testid="app-left-pane"
          style={{
            position: "absolute",
            top: OVERLAY_INSET_PX,
            left: OVERLAY_INSET_PX,
            width: conversationCollapsed ? 240 : 420,
            height: `calc(100% - ${OVERLAY_INSET_PX * 2}px)`,
            zIndex: Z_INDEX.conversation,
            display: "flex",
            flexDirection: "column",
            gap: 12,
            minWidth: 0,
            overflowY: "auto",
            boxSizing: "border-box",
          }}
        >
          {/* The conversation rail: collapsed keeps the last summary
              legible (copy.shell.conversationCollapsed). */}
          {conversationCollapsed ? (
            <button
              type="button"
              data-testid="conversation-rail"
              onClick={() => setConversationCollapsed(false)}
              style={{
                padding: "8px 12px",
                border: "1px solid var(--color-hairline)",
                borderRadius: 8,
                background: "color-mix(in srgb, var(--color-panel) 92%, transparent)",
                color: "var(--color-fg)",
                cursor: "pointer",
                textAlign: "left",
              }}
            >
              {copy.shell.conversationCollapsed(messages.length)} · {copy.shell.openConversation}
            </button>
          ) : (
            <>
              <div data-testid="conversation-header" style={{ display: "flex", justifyContent: "flex-end" }}>
                <button
                  type="button"
                  data-testid="conversation-collapse-btn"
                  onClick={() => setConversationCollapsed(true)}
                  aria-label={copy.shell.collapseConversation}
                  style={{
                    border: "1px solid var(--color-hairline)",
                    borderRadius: 8,
                    background: "color-mix(in srgb, var(--color-panel) 92%, transparent)",
                    color: "var(--color-fg)",
                    cursor: "pointer",
                    padding: "4px 8px",
                  }}
                >
                  {copy.shell.collapseConversation}
                </button>
              </div>
              <div style={{ flex: "1 1 auto", minHeight: 0, display: "flex", flexDirection: "column" }}>
                <ChatPanel
                  messages={messages}
                  onSend={handleSendMessage}
                  inFlight={designLoopInFlight}
                  onBesidePhoto={handleBesidePhoto}
                  envelope={envelope}
                  keptVersion={
                    versions.length > 0 ? versions[versions.length - 1].name : null
                  }
                />
                {designLoopInFlight && (
                  <PassProgress
                    step={designLoopStep}
                    elapsed={designLoopElapsed}
                    viewProgress={viewProgress}
                  />
                )}
                {streamError && <FailureCard error={streamError} />}
              </div>
            </>
          )}
          <PhotoUpload
            projectId={projectId ?? undefined}
            onUploaded={handlePhotoUploaded}
            onError={(msg) =>
              setStreamError({
                message: msg,
                detail: undefined,
                retryable: false,
              })
            }
          />
          {photoSrc && photoDimensions && photoDimensions.width > 0 && photoDimensions.height > 0 && (
            <DimensionCanvas
              photoSrc={photoSrc}
              photoWidth={photoDimensions.width}
              photoHeight={photoDimensions.height}
            />
          )}
          {photoSrc && photoDimensions && (photoDimensions.width === 0 || photoDimensions.height === 0) && (
            <div className="dimension-canvas-unavailable" data-testid="dimension-canvas-unavailable" role="status">
              Photo uploaded, but its dimensions could not be read — dimension drawing is unavailable for this photo.
            </div>
          )}
        </div>
      )}

      {/* Layer 10 — the Brief chip / full panel. The Brief renders as a
          CHIP below 1200px wide OR 820px tall; a full panel above. */}
      {!panelsHidden && (
        <Brief
          isChip={briefIsChip}
          inset={OVERLAY_INSET_PX}
          conversationCollapsed={conversationCollapsed}
          entries={designState}
          hasLivePin={pendingSelection !== null}
          highlightModuleId={pendingSelection?.moduleIds[0] ?? null}
          onAsk={(label) => handleSendMessage(copy.brief.askEstablish(label))}
          onChange={(label) => handleSendMessage(`${copy.brief.rowActions.change}: ${label}`)}
        />
      )}

      {/* Layer 10 — the version filmstrip (bottom-left, horizontal four-slot
          strip, W13). A failure is CONTENT INSIDE THE CONVERSATION, never a
          layer of its own. The strip is absent (not empty) until a version
          exists or a pass is in flight. The pass-in-flight flag drives the
          dashed pending slot so the strip is never behind the conversation.
          The sheet (W16) is reached FROM the strip's expand mark. */}
      {!panelsHidden && projectId !== null && (
        <Filmstrip
          versions={versions}
          passInFlight={designLoopInFlight}
          pendingName={lastUserMessageRef.current || null}
          inset={OVERLAY_INSET_PX}
          onCompareSelect={handleCompareSelect}
          onOpenSheet={(id) => setSheetOpenFor(id)}
          sheetOpenFor={sheetOpenFor}
        />
      )}

      {/* Layer 10 — the history sheet (W16, issue #127). The expanded
          overlay: the branch riser graph, the two-viewport compare (shared
          camera, dimmed unchanged rows) and the restore / pin / compare
          actions. It is an overlay, not a route or a page, at the panels
          layer (10) — the same layer as the filmstrip it is opened from.
          It replaces the transitional VersionTail rail: compare, restore
          and pin are all reachable here, so the rail retires. */}
      {!panelsHidden && projectId !== null && sheetOpenFor !== null && (
        <HistorySheet
          versions={versions}
          compareIds={compareIds}
          compareResult={compareResult}
          compareError={compareError}
          inset={OVERLAY_INSET_PX}
          onRestore={handleVersionRestore}
          onPin={handleVersionPin}
          onCompareSelect={handleCompareSelect}
          onClose={() => setSheetOpenFor(null)}
        />
      )}

      {/* Layer 30 — the region-edit bar, anchored to the pin (issue #129).
          A STAGE-LEVEL SIBLING (not a child of .viewer-pane) at the pin+bar
          z-index. The bar's position is computed from the pin's position in
          the viewport and the viewport size — placed in the quadrant OPPOSITE
          the pin relative to the viewport centre; if that placement overflows,
          it is clamped to the viewport edge AND the leader line reverses
          (flipped = true). The bar never intersects the pin. The leader line
          connects the pin to the bar — a thin 1px line in the marker colour,
          which is the only place the marker legitimately belongs in the bar.
          The bar dims (opacity 0.5) while the pin is in orbiting state. */}
      {pendingSelection && !panelsHidden && (() => {
        const pin = pendingSelection.point;
        const { width: vw, height: vh } = viewportSize;
        // The bar's width is fixed at 320px (the spec's gap-gate answer: a
        // fixed width, clamped against the viewport). Height is derived from
        // the bar's content (thumbnail 32 + input + buttons + module chip +
        // hint); the tests pin the width, so the height is a measurement —
        // the clamp uses the viewport's available space, not a magic height.
        const BAR_WIDTH = 320;
        const BAR_GAP = 12; // gap between pin and bar edge
        const cx = vw / 2;
        const cy = vh / 2;
        // Quadrant: opposite the pin relative to the viewport centre.
        // pin above-centre-left → bar bottom-right, etc.
        const pinLeft = pin.x < cx;
        const pinUp = pin.y < cy;
        // Default position: the quadrant opposite the pin.
        // If pin is upper-left, bar goes lower-right, and vice versa.
        let barLeft: number;
        let barTop: number;
        // The bar's box: width BAR_WIDTH, height estimated at 120px
        // (32px thumbnail + 8px gap + 24px input row + 4px gap + 16px module
        // chip + 4px gap + 20px hint ≈ 90px content + 16px padding + border).
        const BAR_HEIGHT = 120;
        if (pinUp) {
          barTop = cy + BAR_GAP; // bar in lower half
        } else {
          barTop = cy - BAR_GAP - BAR_HEIGHT; // bar in upper half
        }
        if (pinLeft) {
          barLeft = cx + BAR_GAP; // bar in right half
        } else {
          barLeft = cx - BAR_GAP - BAR_WIDTH; // bar in left half
        }
        // Clamp: the bar's box must stay within the viewport.
        const clampedLeft = Math.max(4, Math.min(barLeft, vw - BAR_WIDTH - 4));
        const clampedTop = Math.max(4, Math.min(barTop, vh - BAR_HEIGHT - 4));
        const clamped = clampedLeft !== barLeft || clampedTop !== barTop;
        // The leader line goes from the pin to the bar's nearest edge.
        // When clamped, the leader reverses: it points FROM the bar TO the pin,
        // not from the pin to the bar's default (uncropped) position.
        const leaderStartX = pin.x;
        const leaderStartY = pin.y;
        const leaderEndX = clamped ? pin.x : (pinLeft ? clampedLeft : clampedLeft + BAR_WIDTH);
        const leaderEndY = clamped ? pin.y : (pinUp ? clampedTop + BAR_HEIGHT : clampedTop);
        const dimmed = orbitingPin || orbitClearedPin;
        const moduleChip = pendingSelection.moduleIds.length > 0
          ? pendingSelection.moduleIds[0]
          : null;
        return (
          <>
            {/* The leader line — a thin 1px line in the marker colour.
                The bar is at z-index 30; the leader is part of the bar's
                visual unit and sits at the same z-index. The line goes from
                the pin to the bar's nearest edge (default) or from the bar
                to the pin (flipped — the pin is the "source" the line points
                back to). */}
            <svg
              data-testid="region-edit-leader"
              style={{
                position: "absolute",
                left: 0,
                top: 0,
                width: vw,
                height: vh,
                pointerEvents: "none",
                zIndex: Z_INDEX.pinAndBar,
              }}
              aria-hidden="true"
            >
              <line
                x1={leaderStartX}
                y1={leaderStartY}
                x2={leaderEndX}
                y2={leaderEndY}
                stroke={MARKER_COLOR}
                strokeWidth={1}
                strokeOpacity={0.6}
              />
            </svg>
            <form
              className="region-edit-bar"
              data-testid="region-edit-bar"
              role="group"
              data-flipped={clamped ? "true" : "false"}
              style={{
                position: "absolute",
                left: clampedLeft,
                top: clampedTop,
                width: BAR_WIDTH,
                display: "flex",
                flexDirection: "column",
                gap: 6,
                padding: "8px 12px",
                backgroundColor: "rgba(0, 0, 0, 0.8)",
                borderRadius: 8,
                boxSizing: "border-box",
                zIndex: Z_INDEX.pinAndBar,
                opacity: dimmed ? 0.5 : 1,
                transition: "opacity 120ms ease",
              }}
              onSubmit={(e) => {
                e.preventDefault();
                handleRegionBarSubmit();
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <img
                  src={pendingSelection.thumbnail}
                  alt={`pending selection on ${pendingSelection.viewId}`}
                  className="pending-selection-thumbnail"
                  data-testid="pending-selection-thumbnail"
                  style={{
                    width: 32,
                    height: 32,
                    maxWidth: 200,
                    objectFit: "cover",
                    flex: "0 0 auto",
                  }}
                />
                <input
                  type="text"
                  className="region-edit-input"
                  data-testid="region-edit-input"
                  placeholder={copy.region.placeholder}
                  value={regionBarText}
                  onChange={(e) => setRegionBarText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") handleCancelPendingSelection();
                  }}
                  autoFocus
                  style={{
                    flex: "1 1 auto",
                    minWidth: 0,
                    padding: "4px 8px",
                    border: "none",
                    borderRadius: 4,
                    backgroundColor: "rgba(255, 255, 255, 0.95)",
                    color: "#1f2328",
                  }}
                />
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="submit"
                  className="region-edit-apply-btn"
                  data-testid="region-edit-apply-btn"
                  aria-label={copy.region.apply}
                  disabled={regionBarText.trim().length === 0}
                  style={{
                    flex: "0 0 auto",
                    padding: "4px 12px",
                    border: "none",
                    borderRadius: 4,
                    backgroundColor: "#0969da",
                    color: "#ffffff",
                    cursor: regionBarText.trim().length === 0 ? "not-allowed" : "pointer",
                    opacity: regionBarText.trim().length === 0 ? 0.5 : 1,
                  }}
                >
                  {copy.region.apply}
                </button>
                <button
                  type="button"
                  className="region-edit-cancel-btn"
                  data-testid="pending-selection-cancel-btn"
                  aria-label={copy.region.cancel}
                  onClick={handleCancelPendingSelection}
                  style={{
                    flex: "0 0 auto",
                    padding: "4px 12px",
                    border: "none",
                    borderRadius: 4,
                    backgroundColor: "rgba(255, 255, 255, 0.2)",
                    color: "#ffffff",
                    cursor: "pointer",
                  }}
                >
                  {copy.region.cancel}
                </button>
              </div>
              {/* The resolved module chip + the pose hint. The module name
                  is in mono (the raw detail); the sentence is in the UI face. */}
              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                {moduleChip !== null && (
                  <span
                    data-testid="region-edit-module-chip"
                    style={{
                      fontFamily: "var(--font-mono)",
                      fontSize: 11,
                      color: "var(--color-fg-2)",
                      padding: "2px 6px",
                      background: "rgba(255,255,255,0.08)",
                      borderRadius: 4,
                      display: "inline-block",
                      width: "fit-content",
                    }}
                  >
                    {moduleChip}
                    {" "}
                    {copy.region.resolvedTo}
                  </span>
                )}
                <span
                  data-testid="region-edit-pose-hint"
                  style={{ fontSize: 11, color: "var(--color-muted)" }}
                >
                  {copy.region.poseHint}
                </span>
                {orbitClearedPin && (
                  <span
                    data-testid="region-edit-cleared-hint"
                    style={{
                      fontSize: 11,
                      color: "var(--color-muted)",
                      marginTop: 2,
                    }}
                  >
                    {copy.region.clearedHint}
                  </span>
                )}
              </div>
            </form>
          </>
        );
      })()}

      {/* Selection notice — a CONTENT surface inside the conversation
          layer, not a separate z-index tier. */}
      {selectionNotice && !panelsHidden && (
        <div
          className="selection-notice"
          data-testid="selection-notice"
          role="status"
          style={{
            position: "absolute",
            top: OVERLAY_INSET_PX,
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: Z_INDEX.conversation,
          }}
        >
          {selectionNotice}
        </div>
      )}

      {/* Layer 10 — validation pane (Export3MF), bottom-right. */}
      {projectId !== null && !panelsHidden && (
        <div
          className="validation-pane"
          data-testid="validation-pane"
          style={{
            position: "absolute",
            bottom: OVERLAY_INSET_PX,
            right: OVERLAY_INSET_PX,
            zIndex: Z_INDEX.panels,
          }}
        >
          <Export3MF
            projectId={projectId}
            projectName={projectName}
            versionId={versions.length > 0 ? versions[versions.length - 1].id : undefined}
            client={apiClient}
            onExported={(vid) => void handleExported(vid)}
          />
        </div>
      )}
    </div>
  );
}
