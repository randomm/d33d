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
import { ChatPanel, type ChatMessage } from "./components/chat/ChatPanel";
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
import { compositeMarkedPng } from "./lib/markedPng";
import { ApiClient, MAX_REGION_EDIT_MODULE_IDS } from "./lib/api";
import { loadModuleFixtureArrayBuffer } from "./assets/moduleFixture";

export interface RenderImage {
  /** view filename, e.g. "view_00_front.png" */
  filename: string;
  /** data URL or relative URL */
  src: string;
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

  // Region-selection (lasso) wiring (issue #29).
  const viewerHandleRef = useRef<ModelViewerHandle | null>(null);
  const [moduleFixtureData, setModuleFixtureData] = useState<ArrayBuffer | null>(null);
  const [moduleGroup, setModuleGroup] = useState<Object3D | null>(null);
  const [selectionNotice, setSelectionNotice] = useState<string | null>(null);

  // ModelViewer's onReady effect only fires once per mount — replace (never
  // merge) the captured handle on every call so a remount never leaves a
  // stale raycaster behind.
  const handleViewerReady = useCallback((handle: ModelViewerHandle) => {
    viewerHandleRef.current = handle;
  }, []);

  const handleViewerLoaded = useCallback((result: LoadResult) => {
    setModuleGroup(result.ok && result.mesh ? result.mesh.object : null);
  }, []);

  // Decode the named-module GLB fixture once on mount. Live module-registry
  // wiring is deferred (issue #29 design decision) — this fixture is the
  // sole moduleGroup source for resolveLassoSelection in this ticket.
  // Inlined base64 (decoded synchronously, no network round-trip) so it
  // never competes with `window.fetch` stubs other tests install for the
  // backend API.
  useEffect(() => {
    setModuleFixtureData(loadModuleFixtureArrayBuffer());
  }, []);

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

      setSelectionNotice(null);

      const moduleIds = ranked.slice(0, MAX_REGION_EDIT_MODULE_IDS).map((m) => m.name);
      const markedPngBase64 = compositeMarkedPng(canvas, event.points);

      if (projectId === null) {
        setStreamError("No project selected");
        return;
      }

      void apiClient
        .createRegionEdit(projectId, {
          module_ids: moduleIds,
          view_id: event.viewId,
          marked_png_base64: markedPngBase64,
          polygon: event.points,
          instruction: "",
        })
        .then(() => {
          setMessages((prev) => [
            ...prev,
            {
              id: `msg-${Date.now()}-selection`,
              role: "user",
              content: "Selected a region for editing.",
              selection: {
                thumbnail: `data:image/png;base64,${markedPngBase64}`,
                viewId: event.viewId,
                moduleIds,
              },
            },
          ]);
        })
        .catch((e) => {
          setStreamError(e instanceof Error ? e.message : "Failed to submit region edit");
        });
    },
    [apiClient, moduleGroup, projectId],
  );

  // Create the (single, default) project on mount.
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

  const handleSendMessage = useCallback(
    (text: string) => {
      const userMsg: ChatMessage = {
        id: `msg-${Date.now()}`,
        role: "user",
        content: text,
      };
      setMessages((prev) => [...prev, userMsg]);

      if (projectId === null) {
        setStreamError("No project selected");
        return;
      }

      const assistantId = `msg-${Date.now()}-assistant`;
      setMessages((prev) => [
        ...prev,
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);

      void apiClient
        .streamEvents(projectId, {
          onToken: (text) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId ? { ...m, content: m.content + text } : m,
              ),
            );
          },
          // Progress events (render pipeline steps) will feed the viewer/
          // validation pane once the design-loop-to-SSE wiring (a future
          // ticket per issue #23) produces actual model artifacts. For now
          // there is no render/model data flowing through the app to attach
          // this to, so progress is a no-op placeholder.
          onProgress: () => {},
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
        })
        // streamEvents rethrows after onError on a mid-stream failure so
        // callers that care can observe it; here onError already updated
        // the user-facing error state, so swallow the rejection to avoid
        // it surfacing as an unhandled promise rejection in the browser.
        .catch(() => {});
    },
    [projectId, apiClient],
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

      {/* Right pane: viewer + validation status */}
      <div className="app-right" data-testid="app-right-pane">
        <div className="viewer-pane" data-testid="viewer-pane" style={{ position: "relative" }}>
          {/* No live render/model artifact flows through the app yet — the
           * design-loop-to-SSE-to-model pipeline is a future ticket's
           * scope (see issue #23's documented deferral). The named-module
           * GLB fixture stands in as the moduleGroup source for lasso
           * region selection (issue #29's settled design decision). */}
          <ModelViewer
            data={moduleFixtureData}
            format="glb"
            onReady={handleViewerReady}
            onLoaded={handleViewerLoaded}
          />
          <ViewportLassoOverlay
            viewId="front"
            onLassoCompleted={handleLassoCompleted}
            disabled={moduleGroup === null}
          />
          {selectionNotice && (
            <div className="selection-notice" data-testid="selection-notice" role="status">
              {selectionNotice}
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
