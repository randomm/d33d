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
import { Vector2, type Object3D } from "three";
import { dataUriToArrayBuffer } from "./lib/dataUri";
import {
  ChatPanel,
  type ChatMessage,
  type ChatMessageSelection,
} from "./components/chat/ChatPanel";
import { PhotoUpload } from "./components/upload/PhotoUpload";
import { PinnedParamStrip, type PinnedParam } from "./components/strip/PinnedParamStrip";
import {
  ModelViewer,
  resolveLassoSelection,
  type ModelViewerHandle,
  type LoadResult,
} from "./components/viewer/ModelViewer";
import {
  ViewportLassoOverlay,
  type ViewportLassoCompletedEvent,
} from "./components/viewer/ViewportLassoOverlay";
import { DimensionCanvas } from "./components/canvas/DimensionCanvas";
import { Export3MF } from "./components/export/Export3MF";
import { compositeMarkedPng, stripDataUrlPrefix } from "./lib/markedPng";
import { VersionTimeline } from "./components/versions/VersionTimeline";
import { VariantGallery } from "./components/versions/VariantGallery";
import { CompareView } from "./components/versions/CompareView";
import {
  ApiClient,
  MAX_REGION_EDIT_MODULE_IDS,
  type RegionEditPolygonPoint,
  type RegionEditViewId,
  type VersionTimelineEntry,
  type VersionCompare,
} from "./lib/api";
import { loadModuleFixtureArrayBuffer } from "./assets/moduleFixture";

// ModelViewer and ViewportLassoOverlay each default independently to
// 600x400 when given no explicit size — that only lines up by
// coincidence. Pass one shared size to both so the lasso's click
// coordinate space can never drift from the canvas the raycast (and
// compositeMarkedPng) actually read from.
const VIEWER_WIDTH = 600;
const VIEWER_HEIGHT = 400;

export interface RenderImage {
  /** view filename, e.g. "view_00_front.png" */
  filename: string;
  /** data URL or relative URL */
  src: string;
}

/**
 * A lasso selection that has been resolved to ranked module ids and a
 * composited marked PNG, but has NOT yet been sent as a region edit — the
 * user must still supply the free-text instruction via chat (see
 * `handleLassoCompleted` / `handleSendMessage`). Carries everything
 * `RegionEditRequest` needs except `instruction`.
 */
interface PendingRegionSelection {
  /** The composited red-marked view PNG as a data URL (for the chat
   *  thumbnail) — `stripDataUrlPrefix`'d again when building the request. */
  thumbnail: string;
  viewId: RegionEditViewId;
  moduleIds: string[];
  polygon: RegionEditPolygonPoint[];
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
  const [streamError, setStreamError] = useState<string | null>(null);
  const [designLoopInFlight, setDesignLoopInFlight] = useState(false);

  // Version timeline (issue #8) — the side rail. Loaded when the project
  // resolves (opening a project RESUMES the chat at its latest version
  // with the timeline as a side rail — the project home is the
  // conversation and the design together).
  const [versions, setVersions] = useState<VersionTimelineEntry[]>([]);
  const [compareIds, setCompareIds] = useState<[number, number] | null>(null);

  // Region-selection (lasso) wiring (issue #29).
  const viewerHandleRef = useRef<ModelViewerHandle | null>(null);
  const [moduleFixtureData, setModuleFixtureData] = useState<ArrayBuffer | null>(null);
  // Stream-driven model (issue #69): once a design-loop pass streams its
  // best render's STL through the version-created progress frame
  // (`stl_data_uri`), the viewer swaps from the static GLB fixture to the
  // decoded STL ArrayBuffer (format "stl"). Stays null until the first
  // stream-driven pass — the fixture (and its named modules, the lasso's
  // moduleGroup source) is the pre-pass state.
  //
  // ONE-WAY LATCH: once set, it is never reset to null. A later pass whose
  // frame omits stl_data_uri does not restore the fixture — the streamed
  // model stays mounted and the lasso stays degraded for the session.
  // This is intentional (a streamed pass is a terminal state for the
  // viewer's source); a future ticket that needs the fixture back must
  // add an explicit reset path here, not an implicit one.
  const [streamModelData, setStreamModelData] = useState<ArrayBuffer | null>(null);
  const [moduleGroup, setModuleGroup] = useState<Object3D | null>(null);
  const [selectionNotice, setSelectionNotice] = useState<string | null>(null);
  // The viewer's mounted data + format, derived ONCE from the source-of-
  // truth pair (streamModelData / moduleFixtureData) so the "which source
  // is mounted" decision lives in one place, not in JSX.
  const viewerSource = streamModelData
    ? { data: streamModelData, format: "stl" as const }
    : { data: moduleFixtureData, format: "glb" as const };
  // A lasso selection that has been resolved (module ids + marked PNG) but
  // not yet sent — the instruction is the user's own free text, which the
  // server requires non-empty (`RegionEditRequest.instruction`,
  // `Field(min_length=1)`). The selection is attached to the NEXT chat
  // message the user sends, not fired immediately from the lasso.
  const [pendingSelection, setPendingSelection] = useState<PendingRegionSelection | null>(
    null,
  );
  // Bumped on every draw/cancel/send-clear of pendingSelection (see the
  // three setPendingSelection call sites below). A createRegionEdit
  // rejection captures the generation at send time and only restores the
  // selection if nothing has touched pendingSelection since — otherwise a
  // slow/failed request for an OLD selection could silently clobber a NEWER
  // one the user drew afterward, or resurrect one they explicitly cancelled.
  const pendingSelectionGenerationRef = useRef(0);

  // ModelViewer's onReady effect only fires once per mount — replace (never
  // merge) the captured handle on every call so a remount never leaves a
  // stale raycaster behind.
  const handleViewerReady = useCallback((handle: ModelViewerHandle) => {
    viewerHandleRef.current = handle;
  }, []);

  const handleViewerLoaded = useCallback(
    (result: LoadResult) => {
      if (result.ok && result.mesh) {
        // Format-based moduleGroup policy (issue #69): the GLB fixture's
        // named meshes are the lasso's moduleGroup source; a streamed STL
        // is a single unnamed mesh, so it does NOT re-enable the lasso —
        // the viewer displays the streamed model but the lasso stays
        // disabled with the degradation notice.
        if (result.mesh.format === "glb") {
          setModuleGroup(result.mesh.object);
          setSelectionNotice(null);
        } else {
          setModuleGroup(null);
        }
        return;
      }
    // A load failure (decode/parse error) is distinct from "still
    // loading" — both leave moduleGroup null (disabling the lasso via
    // `disabled={moduleGroup === null}`), but only a failure should tell
    // the user *why* the lasso is unavailable instead of leaving them to
    // wonder if it will ever appear.
      setModuleGroup(null);
      setSelectionNotice(
        result.error
          ? `Model failed to load — lasso unavailable: ${result.error}`
          : "Model failed to load — lasso unavailable.",
      );
    },
    [],
  );

  // Decode the named-module GLB fixture once on mount. Live module-registry
  // wiring is deferred (issue #29 design decision) — this fixture is the
  // sole moduleGroup source for resolveLassoSelection in this ticket. It is
  // the viewer's pre-pass state (issue #69): a stream-driven pass replaces
  // it with the design loop's rendered STL via streamModelData.
  // Inlined base64 (decoded synchronously, no network round-trip) so it
  // never competes with `window.fetch` stubs other tests install for the
  // backend API.
  useEffect(() => {
    setModuleFixtureData(loadModuleFixtureArrayBuffer());
  }, []);

  // Consume the design-loop frame fields (issue #69): the version-created
  // progress frame carries `stl_data_uri` (the best iteration's STL as a
  // base64 data URI) plus a `views` map of data URIs. Swap the viewer to
  // the stream-derived STL. Runs BEFORE the lasso-degradation notice: a
  // pass always renders a single unnamed mesh, so the fixture's named
  // modules are gone and the lasso can no longer resolve to module ids.
  // (SCAD ownership is untouched — onToken stays the sole SCAD carrier.)
  // The streamed source is a one-way latch (see streamModelData): a later
  // frame that omits stl_data_uri does not restore the fixture.
  const handleStreamViewerData = useCallback(
    (data: Record<string, unknown>) => {
      const stlDataUri = typeof data.stl_data_uri === "string" ? data.stl_data_uri : null;
      if (stlDataUri === null) return;
      try {
        setStreamModelData(dataUriToArrayBuffer(stlDataUri));
      } catch (e) {
        // A corrupt data URI must not break the stream turn — keep the
        // exception visible in the console and surface the same notice
        // channel used for load failures (never a crash). The lasso is
        // unchanged (no new model mounted), so the notice names the decode
        // failure only.
        console.error("failed to decode streamed STL data URI", e);
        setSelectionNotice(
          "Could not decode the streamed model — the displayed model is unchanged.",
        );
        return;
      }
      setModuleGroup(null);
      setSelectionNotice(
        "Lasso selection is unavailable for streamed models — the rendered STL has no named modules.",
      );
    },
    [],
  );

  const handleLassoCompleted = useCallback(
    (event: ViewportLassoCompletedEvent) => {
      const handle = viewerHandleRef.current;
      if (!handle || !moduleGroup) {
        // "No model loaded yet" — distinct from "lasso hit nothing".
        setSelectionNotice("Model not loaded yet — draw the lasso again once it appears.");
        return;
      }

      const canvas = handle.renderer.domElement;
      // CSS-pixel viewport size, NOT canvas.width/canvas.height (the
      // WebGL drawing-buffer size, which renderer.setPixelRatio scales by
      // devicePixelRatio). ViewportLassoOverlay's points come from
      // getBoundingClientRect() — always CSS pixels — so the NDC
      // conversion in resolveLassoSelection must divide by the same
      // CSS-pixel dimensions or every raycast mis-registers on any
      // DPR!==1 display.
      const cssSize = handle.renderer.getSize(new Vector2());

      // resolveLassoSelection and compositeMarkedPng both throw on
      // unexpected failures (notably compositeMarkedPng's
      // `canvas.getContext("2d")` returning null on context loss, exhausted
      // canvas contexts, or headless quirks). There is no ErrorBoundary in
      // this app, so an uncaught throw here would unmount the whole React
      // tree — losing chat history and project state — instead of
      // degrading like the existing "nothing selected" path. Catch and
      // route through the same selection-notice channel.
      try {
        const { ranked, primary } = resolveLassoSelection(
          event.points,
          cssSize.x,
          cssSize.y,
          handle.camera,
          handle.raycaster,
          moduleGroup,
        );

        if (primary === null || ranked.length === 0) {
          // Empty ranked list — distinct "nothing selected" state. Never
          // falls back to selecting the whole model, never calls the API.
          setSelectionNotice("Nothing selected — the lasso didn't hit any part of the model.");
          return;
        }

        const moduleIds = ranked.slice(0, MAX_REGION_EDIT_MODULE_IDS).map((m) => m.name);
        // compositeMarkedPng's polygon argument is in the same CSS-pixel space
        // as event.points (ViewportLassoOverlay draws via getBoundingClientRect()),
        // so it needs the same CSS-pixel cssSize used for the raycast above to
        // scale into the canvas's drawing-buffer pixel space.
        const markedPngBase64 = compositeMarkedPng(canvas, event.points, cssSize.x, cssSize.y);

        setSelectionNotice(null);

        // The server requires a non-empty free-text `instruction`
        // (`RegionEditRequest.instruction`, `Field(min_length=1)`) that only
        // the user can supply. Rather than call createRegionEdit here with
        // no instruction (which would always 422), stash the resolved
        // selection and the polygon/view needed to build the request, and
        // surface an affordance telling the user to describe the change in
        // chat. The pending selection is attached to whichever chat message
        // the user sends next (see handleSendMessage).
        pendingSelectionGenerationRef.current += 1;
        setPendingSelection({
          thumbnail: `data:image/png;base64,${markedPngBase64}`,
          viewId: event.viewId,
          moduleIds,
          polygon: event.points,
        });
      } catch (e) {
        const detail = e instanceof Error ? e.message : "unknown error";
        setSelectionNotice(`Selection failed — could not process the lasso: ${detail}`);
      }
    },
    [moduleGroup],
  );

  const handleCancelPendingSelection = useCallback(() => {
    pendingSelectionGenerationRef.current += 1;
    setPendingSelection(null);
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
          setStreamError(e instanceof Error ? e.message : "Failed to create project");
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
        setStreamError(
          `Restore failed: ${e instanceof Error ? e.message : "unknown error"}`,
        );
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
          prev.map((v) => (v.id === versionId ? { ...v, pinned } : v)),
        );
      } catch (e) {
        setStreamError(`Pin failed: ${e instanceof Error ? e.message : "unknown error"}`);
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

      // Bail before constructing/appending anything if there's no project to
      // send to — a message (and any attached selection) must never render
      // as sent when the region-edit request that would justify it can
      // never fire. Checked ahead of selectionToAttach/userMsg construction
      // so a pending selection is never displayed as "submitted" while
      // still sitting untouched in state.
      if (projectId === null) {
        setStreamError("No project selected");
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

      if (selectionToAttach) {
        // Clear immediately so a slow createRegionEdit response can't race a
        // second send into re-attaching the same pending selection. Snapshot
        // the generation counter first so the reject handler below can tell
        // whether the user drew a new lasso or clicked cancel while this
        // request was in flight.
        const sentGeneration = pendingSelectionGenerationRef.current;
        pendingSelectionGenerationRef.current += 1;
        setPendingSelection(null);
        void apiClient
          .createRegionEdit(projectId, {
            module_ids: selectionToAttach.moduleIds,
            view_id: selectionToAttach.viewId,
            marked_png_base64: stripDataUrlPrefix(selectionToAttach.thumbnail),
            polygon: selectionToAttach.polygon,
            instruction: trimmed,
          })
          .then(() => {
            // 202 Accepted means the request was validated and queued —
            // NOT that any regeneration happened (`RegionEditResult.status`
            // is always "deferred"; scoped-edit regeneration is not yet
            // implemented server-side, see api.ts's createRegionEdit doc
            // comment). Without this, a successful request produced zero
            // feedback, indistinguishable from a silent failure or a
            // request still in flight. Word it so it can never read as a
            // completed edit.
            setMessages((prev) => [
              ...prev,
              {
                id: `msg-${Date.now()}-region-edit-accepted`,
                role: "assistant",
                content:
                  "Region edit request accepted — scoped regeneration is not implemented yet.",
              },
            ]);
          })
          .catch((e) => {
            // The request failed (network error, or a 4xx/5xx from the
            // server) after the pending selection was already cleared —
            // restore it so the user doesn't have to redraw the lasso, and
            // say plainly that THIS is what failed (the chat message
            // above already shows the selection thumbnail as sent, so a
            // generic error would leave that looking correct).
            //
            // Only restore if the generation counter is UNCHANGED since this
            // request started (i.e. the send's own +1 is still the latest
            // bump) — this request's selectionToAttach is a value closed over
            // at send time. If the user drew a new lasso or clicked cancel
            // while this request was in flight, the counter has moved on and
            // this stale value must NOT win: an unconditional (or a merely
            // current-is-null) overwrite here would silently clobber a newer
            // selection, or resurrect one the user explicitly cancelled, with
            // no way for the user to tell the difference.
            const detail = e instanceof Error ? e.message : "unknown error";
            if (pendingSelectionGenerationRef.current === sentGeneration + 1) {
              setPendingSelection(selectionToAttach);
              setStreamError(
                `Region edit failed — selection restored, please resend: ${detail}`,
              );
            } else {
              // The user already drew a new selection or cancelled while this
              // request was in flight — nothing to restore, and claiming so
              // would be dishonest about what state the UI is actually in.
              setStreamError(`Region edit failed: ${detail}`);
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
            onProgress: (_step, data) => {
              handleStreamViewerData(data);
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
              setStreamError(typeof data.message === "string" ? data.message : "Stream error");
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
          setStreamError(detail);
        })
        .finally(() => {
          // Release the in-flight flag on ALL exit paths (pass, exhausted,
          // exception, network error). The server also clears its flag in
          // a finally, but the client-side flag is what disables the send
          // button.
          setDesignLoopInFlight(false);
        });
    },
    [projectId, apiClient, pendingSelection, messages, handleStreamViewerData],
  );

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
    <div className="app-shell" data-testid="app-shell">
      {/* Left pane: chat + upload */}
      <div className="app-left" data-testid="app-left-pane">
        <ChatPanel
          messages={messages}
          onSend={handleSendMessage}
          renders={renders}
          inFlight={designLoopInFlight}
        />
        <PhotoUpload projectId={projectId ?? undefined} onUploaded={handlePhotoUploaded} onError={setStreamError} />
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
            {streamError}
          </div>
        )}
      </div>

      {/* Right pane: version timeline (side rail) + viewer + validation */}
      <div className="app-right" data-testid="app-right-pane">
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
        <div className="viewer-pane" data-testid="viewer-pane" style={{ position: "relative" }}>
          {/* The viewer's pre-pass state is the named-module GLB fixture —
           * the moduleGroup source for lasso region selection (issue #29's
           * settled design decision). After a stream-driven design-loop
           * pass (issue #69) it is REPLACED by the design loop's rendered
           * STL (streamModelData, from the version-created frame's
           * stl_data_uri) so the browser displays the model the loop
           * actually produced. */}
          <ModelViewer
            data={viewerSource.data}
            format={viewerSource.format}
            width={VIEWER_WIDTH}
            height={VIEWER_HEIGHT}
            onReady={handleViewerReady}
            onLoaded={handleViewerLoaded}
          />
          <ViewportLassoOverlay
            viewId="front"
            width={VIEWER_WIDTH}
            height={VIEWER_HEIGHT}
            onLassoCompleted={handleLassoCompleted}
            disabled={moduleGroup === null}
          />
          {selectionNotice && (
            <div className="selection-notice" data-testid="selection-notice" role="status">
              {selectionNotice}
            </div>
          )}
          {pendingSelection && (
            <div
              className="pending-selection-notice"
              data-testid="pending-selection-notice"
              role="status"
              style={{
                border: "2px solid #d0d7de",
                backgroundColor: "#f6f8fa",
                padding: "8px",
              }}
            >
              <span>Region selected — describe the change below.</span>
              <img
                src={pendingSelection.thumbnail}
                alt={`pending selection on ${pendingSelection.viewId}`}
                className="pending-selection-thumbnail"
                data-testid="pending-selection-thumbnail"
                style={{ maxWidth: "200px" }}
              />
              <button
                type="button"
                data-testid="pending-selection-cancel-btn"
                onClick={handleCancelPendingSelection}
              >
                Cancel selection
              </button>
            </div>
          )}
        </div>
        <div className="validation-pane" data-testid="validation-pane">
          <span data-testid="validation-status">Waiting for render…</span>
          {projectId !== null && <Export3MF projectId={projectId} client={apiClient} />}
        </div>
      </div>
    </div>
  );
}
