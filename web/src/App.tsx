/**
 * App shell — the two-pane layout from 05-spa-core.md.
 *
 * Left: chat panel (with inline render images) + photo upload.
 * Right: three.js viewer + validation status + Export 3MF.
 *
 * No auto-generated slider panel. The pinned-parameter strip is opt-in
 * and holds at most 3 user-chosen entries.
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
import { PinnedParamStrip, type PinnedParam } from "./components/strip/PinnedParamStrip";
import { ModelViewer, type ModelViewerHandle, type LoadResult } from "./components/viewer/ModelViewer";
import { PickLayer } from "./components/viewer/PickLayer";
import { resolvePointPick } from "./components/viewer/ModelViewer";
import { DimensionCanvas } from "./components/canvas/DimensionCanvas";
import { Export3MF } from "./components/export/Export3MF";
import { compositeMarkedPng, stripDataUrlPrefix } from "./lib/markedPng";
import { displayDesignLoopError, type DisplayError } from "./lib/errorMapping";
import { VersionTimeline } from "./components/versions/VersionTimeline";
import { VariantGallery } from "./components/versions/VariantGallery";
import { CompareView } from "./components/versions/CompareView";
import {
  ApiClient,
  type RegionEditViewId,
  type VersionTimelineEntry,
  type VersionCompare,
} from "./lib/api";
import { loadModuleFixtureArrayBuffer } from "./assets/moduleFixture";

// ModelViewer and PickLayer each default independently to 600x400 when
// given no explicit size — that only lines up by coincidence. Pass one
// shared size to both so the pick's click coordinate space can never
// drift from the canvas the raycast (and compositeMarkedPng) actually
// read from.
const VIEWER_WIDTH = 600;
const VIEWER_HEIGHT = 400;

export interface RenderImage {
  /** view filename, e.g. "view_00_front.png" */
  filename: string;
  /** data URL or relative URL */
  src: string;
}

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
  /** Render images to display inline in the chat. Defaults to empty. */
  renders?: RenderImage[];
  /** Injectable API client (test seam). Defaults to a same-origin ApiClient. */
  client?: ApiClient;
}

export default function App({ renders = [], client }: AppProps) {
  const apiClient = useRef(client ?? new ApiClient()).current;

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pinnedParams, setPinnedParams] = useState<PinnedParam[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [photoSrc, setPhotoSrc] = useState<string | null>(null);
  const [photoDimensions, setPhotoDimensions] = useState<{ width: number; height: number } | null>(
    null,
  );
  const [streamError, setStreamError] = useState<DisplayError | null>(null);
  // The kind of error ("stream" = design-loop/chat stream, "other" = e.g.
  // a region edit that failed before the stream opened, or a photo upload
  // failure) — the Retry control only renders for "stream" errors (issue
  // #82): a region-edit failure carries a consumed selection and must not
  // be silently resent without it.
  const [streamErrorKind, setStreamErrorKind] = useState<"stream" | "other">("other");
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

  // Region-selection (point pick) wiring (issue #98).
  const viewerHandleRef = useRef<ModelViewerHandle | null>(null);
  const [moduleFixtureData, setModuleFixtureData] = useState<ArrayBuffer | null>(null);
  // Stream-driven model (issue #69): once a design-loop pass streams its
  // best render's STL through the version-created progress frame
  // (`stl_data_uri`), the viewer swaps from the static GLB fixture to the
  // decoded STL ArrayBuffer (format "stl"). Stays null until the first
  // stream-driven pass — the fixture is the pre-pass state.
  //
  // ONE-WAY LATCH: once set, it is never reset to null. A later pass whose
  // frame omits stl_data_uri does not restore the fixture — the streamed
  // model stays mounted for the session. This is intentional (a streamed
  // pass is a terminal state for the viewer's source); a future ticket
  // that needs the fixture back must add an explicit reset path here, not
  // an implicit one.
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
  // The viewer's mounted data + format, derived ONCE from the source-of-
  // truth pair (streamModelData / moduleFixtureData) so the "which source
  // is mounted" decision lives in one place, not in JSX.
  const viewerSource = streamModelData
    ? { data: streamModelData, format: "stl" as const }
    : { data: moduleFixtureData, format: "glb" as const };
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
    setSelectionNotice(
      "Selection cleared — the view changed. Orbit to a good angle first, then click the model to point at a part.",
    );
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

  // Decode the named-module GLB fixture once on mount. Live module-registry
  // wiring is deferred (issue #29 design decision) — this fixture is the
  // viewer's pre-pass state (issue #69): a stream-driven pass replaces it
  // with the design loop's rendered STL via streamModelData. Any named
  // modules under the pick (the GLB fixture path) resolve as a bonus via
  // resolvePointPick; unnamed streamed STL is still fully selectable.
  // Inlined base64 (decoded synchronously, no network round-trip) so it
  // never competes with `window.fetch` stubs other tests install for the
  // backend API.
  useEffect(() => {
    setModuleFixtureData(loadModuleFixtureArrayBuffer());
  }, []);

  // Consume the design-loop frame fields (issue #69): the version-created
  // progress frame carries `stl_data_uri` (the best iteration's STL as a
  // base64 data URI) plus a `views` map of data URIs. Swap the viewer to
  // the stream-derived STL. A streamed pass renders a single unnamed mesh,
  // so picks on it resolve no module ids — selection still works, grounded
  // by the marked PNG alone (issue #98). (SCAD ownership is untouched —
  // onToken stays the sole SCAD carrier.) The streamed source is a one-way
  // latch (see streamModelData): a later frame that omits stl_data_uri does
  // not restore the fixture.
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
  }, []);

  // Create the (single, default) project on mount. Once it resolves,
  // load the version timeline (the side rail) — the project resumes at
  // its latest version.
  useEffect(() => {
    let cancelled = false;
    apiClient
      .createProject({ name: "untitled project" })
      .then((project) => {
        if (!cancelled) setProjectId(project.id);
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
              setStreamError({
                message: `Region edit failed — selection restored, please resend: ${detail}`,
                detail,
                retryable: false,
              });
            } else {
              // The user already drew a new selection or cancelled while this
              // request was in flight — nothing to restore, and claiming so
              // would be dishonest about what state the UI is actually in.
              setStreamError({
                message: `Region edit failed: ${detail}`,
                detail,
                retryable: false,
              });
            }
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
            onToken: (text) => {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId ? { ...m, content: m.content + text } : m,
                ),
              );
            },
            onProgress: (step, data) => {
              handleStreamViewerData(data);
              // Issue #82: the step name was previously discarded ("_step").
              // The stage indicator uses it to show a human-readable label.
              // design-loop-pass fires once per iteration (up to 3×); setting
              // an identical value is a no-op state update (React bails out),
              // so repeated steps do not reset the elapsed timer or flicker.
              if (typeof step === "string") setDesignLoopStep(step);
            },
            onDone: () => {
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m)),
              );
            },
            onError: (data) => {
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m)),
              );
              setStreamErrorKind("stream");
              // The structured `reason` (when present) is mapped to plain
              // language; a missing reason is an infra failure (or a legacy
              // frame) — generic copy, never a gate mapping (issue #82).
              setStreamError(displayDesignLoopError(data));
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
          setStreamErrorKind("stream");
          setStreamError({
            message: "The request could not be sent. The design did not start — you can retry.",
            detail,
            retryable: true,
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
        });
    },
    [projectId, apiClient, pendingSelection, messages, handleStreamViewerData],
  );

  // The inline bar's submit path — routes the typed instruction through the
  // SAME handleSendMessage the chat panel uses, so the pending selection is
  // attached and createRegionEdit fires exactly once, with the same
  // empty/whitespace guard, generation race-guard, and error handling as a
  // chat send (no duplicated request logic). handleSendMessage trims the
  // text; a whitespace-only draft therefore also cannot fire a request, and
  // an empty draft never even reaches it (guard below).
  // Retry (issue #82): re-sends the LAST PLAIN CHAT message through the
  // SAME handleSendMessage path the chat panel uses — no duplicated request
  // logic, so the same empty/whitespace guard and (now-empty) pending-
  // selection handling apply. A region edit failure is never retryable
  // (streamErrorKind !== "stream" hides the control); a plain chat failure
  // whose request carried no region selection is. Guarded by
  // `!designLoopInFlight` so a retry cannot double-send.
  const handleRetry = useCallback(() => {
    if (designLoopInFlight) return;
    const text = lastUserMessageRef.current;
    if (text.trim().length === 0) return;
    handleSendMessage(text);
  }, [designLoopInFlight, handleSendMessage]);

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

  const togglePinParam = useCallback((name: string, value: number) => {
    setPinnedParams((prev) => {
      const existing = prev.find((p) => p.name === name);
      if (existing) {
        return prev.filter((p) => p.name !== name);
      }
      if (prev.length >= 3) return prev; // cap at 3
      return [...prev, { name, value }];
    });
  }, []);

  return (
    <div
      className="app-shell"
      data-testid="app-shell"
      style={{
        display: "flex",
        flexDirection: "row",
        height: "100vh",
        margin: 0,
        boxSizing: "border-box",
        overflow: "hidden",
      }}
    >
      {/* Left pane: chat + upload */}
      <div
        className="app-left"
        data-testid="app-left-pane"
        style={{
          flex: "1 1 0",
          minWidth: 0,
          overflowY: "auto",
          boxSizing: "border-box",
        }}
      >
        <ChatPanel
          messages={messages}
          onSend={handleSendMessage}
          renders={renders}
          inFlight={designLoopInFlight}
        />
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
        <PinnedParamStrip
          params={pinnedParams}
          onToggle={togglePinParam}
        />
        {streamError && (
          <div className="app-error" data-testid="app-error" role="alert">
            {streamError.message}
            {streamError.detail && (
              <details data-testid="app-error-detail" className="app-error-detail">
                <summary>Details</summary>
                {streamError.detail}
              </details>
            )}
            {streamError.retryable && streamErrorKind === "stream" && (
              <button
                type="button"
                className="app-error-retry-btn"
                data-testid="app-error-retry"
                onClick={handleRetry}
                disabled={designLoopInFlight}
              >
                Retry
              </button>
            )}
          </div>
        )}
        {designLoopInFlight && (
          <div className="design-loop-progress" data-testid="design-loop-progress" role="status">
            <span data-testid="design-loop-stage">
              {designLoopStep === "design-loop-start"
                ? "Generating design…"
                : designLoopStep === "design-loop-pass"
                  ? "Rendering and checking…"
                  : designLoopStep === "version-created"
                    ? "Saving version…"
                    : "Working on your design…"}
            </span>
            <div
              className="design-loop-progress-bar"
              data-testid="design-loop-progress-bar"
              aria-hidden="true"
            >
              <span className="design-loop-progress-indicator" />
            </div>
            <span data-testid="design-loop-elapsed">{designLoopElapsed}s</span>
          </div>
        )}
      </div>

      {/* Right pane: version timeline (side rail) + viewer + validation */}
      <div
        className="app-right"
        data-testid="app-right-pane"
        style={{
          flex: `0 0 ${VIEWER_WIDTH}px`,
          width: VIEWER_WIDTH,
          display: "flex",
          flexDirection: "column",
          overflowY: "auto",
          boxSizing: "border-box",
        }}
      >
        {/* The version timeline side rail (issue #8) — the project resumes
            at its latest version; the timeline is the history surface. */}
        {projectId !== null && (
          <div className="version-tail-pane" data-testid="version-timeline-pane">
            <VersionTimeline
              versions={versions}
              latestId={versions.length > 0 ? versions[versions.length - 1].id : 0}
              onRestore={handleVersionRestore}
              onPin={handleVersionPin}
              onCompareSelect={handleCompareSelect}
            />
            {/* The two-viewport compare (surface 5) — rendered once two
                versions have been selected from the timeline. */}
            {compareIds !== null && compareResult !== null && (
              <div data-testid="compare-pane">
                {compareError && (
                  <p data-testid="compare-error" role="alert">
                    {compareError}
                  </p>
                )}
                <CompareView
                  compare={compareResult}
                  aId={compareIds[0]}
                  bId={compareIds[1]}
                />
              </div>
            )}
            {/* The pinned variant gallery (surface 2) — the browse-my-options
                surface, distinct from the linear timeline. */}
            {versions.filter((v) => v.pinned && !v.archived).length > 0 && (
              <div data-testid="gallery-pane">
                <VariantGallery
                  cards={versions
                    .filter((v) => v.pinned && !v.archived)
                    .map((v) => ({
                      ...v,
                      actions: ["set-as-main", "branch-from", "archive"] as const,
                    }))}
                />
              </div>
            )}
          </div>
        )}
        <div
          className="viewer-pane"
          data-testid="viewer-pane"
          style={{
            position: "relative",
            width: VIEWER_WIDTH,
            height: VIEWER_HEIGHT,
            // flexShrink 0 pins this pane's box: a tall version-tail-pane
            // above can no longer compress the 600x400 containing block that
            // the pick layer (absolute, inset 0) and the inline region
            // bar (absolute, bottom 0) are positioned against — their
            // coordinate space stays locked to the canvas regardless of how
            // much history the side rail grows (issue #76).
            flex: "0 0 auto",
            flexShrink: 0,
            overflow: "hidden",
            boxSizing: "border-box",
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
            width={VIEWER_WIDTH}
            height={VIEWER_HEIGHT}
            onReady={handleViewerReady}
            onLoaded={handleViewerLoaded}
          />
          {/* Single-click pick layer (issue #98) — the replacement for the
              pick layer (issue #98). `ready` flips once a model is loaded
              (e2e waits on `data-ready`); `marker` is the visible red dot
              while a selection is pending. The layer never swallows
              drag/wheel events, so OrbitControls always gets the camera's
              gestures. */}
          <PickLayer
            ready={pickLayerReady}
            marker={pickMarker}
            onPointSelected={handlePointSelected}
            width={VIEWER_WIDTH}
            height={VIEWER_HEIGHT}
          />
          {/* Inline region bar (issue #76) — the pending-selection affordance
              relocated from a sibling-below card into .viewer-pane itself:
              absolute, bottom 0, full width, semi-transparent so the model
              shows through. Renders ONLY while a selection is pending —
              with pendingSelection null it is absent from the DOM, so it can
              never block the pick layer's clicks or cover the canvas.
              Apply submits through the SAME handleSendMessage path as the
              chat panel (same guard, attach, and race-guard); the draft
              lives in local state, and Escape or Cancel clears it and the
              selection together. The thumbnail stays small inside the bar —
              the red marker dot on the canvas is the primary visual
              reference, the crop is the secondary one. */}
          {pendingSelection && (
            <form
              className="region-edit-bar"
              data-testid="region-edit-bar"
              role="group"
              style={{
                position: "absolute",
                bottom: 0,
                left: 0,
                right: 0,
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "8px 12px",
                backgroundColor: "rgba(0, 0, 0, 0.8)",
                boxSizing: "border-box",
                zIndex: 10,
              }}
              onSubmit={(e) => {
                e.preventDefault();
                handleRegionBarSubmit();
              }}
            >
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
                placeholder="Describe the change to this region…"
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
              <button
                type="submit"
                className="region-edit-apply-btn"
                data-testid="region-edit-apply-btn"
                aria-label="Apply"
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
                Apply
              </button>
              <button
                type="button"
                className="region-edit-cancel-btn"
                data-testid="pending-selection-cancel-btn"
                aria-label="Cancel selection"
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
                Cancel
              </button>
            </form>
          )}
        </div>
        {selectionNotice && (
          <div className="selection-notice" data-testid="selection-notice" role="status">
            {selectionNotice}
          </div>
        )}
        <div className="validation-pane" data-testid="validation-pane">
          <span data-testid="validation-status">Waiting for render…</span>
          {projectId !== null && <Export3MF projectId={projectId} client={apiClient} />}
        </div>
      </div>
    </div>
  );
}
