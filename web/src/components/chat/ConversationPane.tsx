/**
 * Conversation pane — the conversation layer's shell (issue #195, the
 * #116/W8 extraction lineage).
 *
 * Extracted verbatim from App.tsx (behaviour-neutral: every prop maps 1:1
 * to a state value, handler or derived value the shell already computes).
 * The shell keeps all state and API wiring; this component is presentational
 * JSX only — the docked/floating ternary, the collapse rail, the header,
 * the docked filmstrip render site, and the photo/dimension surfaces.
 *
 * Issue #194: at short viewports the pane docks to the bottom (full width,
 * CONVERSATION_DOCK_HEIGHT_VH) instead of floating top-left; the docked
 * notice and the docked filmstrip render site live here. The floating
 * filmstrip itself stays at the stage level (App.tsx) so its box never
 * resolves against this positioned pane.
 */
import {
  ChatPanel,
  type ChatMessage,
} from "./ChatPanel";
import { FLEX_FILL } from "./flexFill";
import { PhotoUpload } from "../upload/PhotoUpload";
import { DimensionCanvas } from "../canvas/DimensionCanvas";
import { PassProgress } from "../progress/PassProgress";
import { FailureCard } from "../failure/FailureCard";
import { Filmstrip } from "../versions/Filmstrip";
import type { Envelope, VersionTimelineEntry } from "../../lib/api";
import type { ViewProgressState } from "../../lib/viewProgress";
import type { DisplayError } from "../../lib/errorMapping";
import type { ReactNode } from "react";
import { Z_INDEX } from "../../App";
import copy from "../../copy";

/** Inset of the floating overlays from the stage edge (issue #119). */
const OVERLAY_INSET_PX = 24;
/** The docked band's height, as a percentage of the viewport (issue #194). */
const CONVERSATION_DOCK_HEIGHT_VH = 48;

interface ConversationPaneProps {
  /** Docked (full-width bottom band) instead of floating (issue #194). */
  docked: boolean;
  /** User-initiated rail collapse (independent of the dock). */
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  /** The project id — null until the lazy creation resolves (issue #192). */
  projectId: number | null;
  /** The chat transcript. */
  messages: ChatMessage[];
  onSend: (text: string) => void;
  inFlight: boolean;
  onBesidePhoto: () => void;
  /** The build envelope for the failure turn's measured number. */
  envelope: Envelope | null;
  /** Issue #193: the exactly-one-composer invariant — the pane's ChatPanel
   *  hides its composer while FirstRun carries the only one. */
  hideComposer: boolean;
  /** The last user message text (the filmstrip's pending slot). */
  lastUserMessage: string;
  /** The version timeline (the docked filmstrip's data). */
  versions: VersionTimelineEntry[];
  onCompareSelect: (versionId: number) => void;
  onOpenSheet: (versionId: number) => void;
  sheetOpenFor: number | null;
  /** The conversation header (rendered only while not collapsed). App
   *  supplies it — the W191 design-contract tripwire slices the collapse
   *  button's source in App.tsx, so the control itself is not moved here. */
  header: ReactNode | null;
  /** The current design-loop progress step (null when idle). */
  designLoopStep: string | null;
  /** Elapsed seconds of the in-flight design loop. */
  designLoopElapsed: number;
  /** The per-view progress state (issue #118). */
  viewProgress: ViewProgressState;
  /** The app-level error card's data. */
  streamError: DisplayError | null;
  /** The uploaded reference photo (null before any upload). */
  photoSrc: string | null;
  photoDimensions: { width: number; height: number } | null;
  onPhotoUploaded: (photoPath: string, width: number, height: number) => void;
  onPhotoError: (msg: string) => void;
  /** Issue #282: the single-flight lazy-creation latch (App's
   *  ensureProject), routed to PhotoUpload so a photo chosen before any
   *  message creates the project instead of bailing "No project selected". */
  onEnsureProject: () => Promise<number>;
}

export function ConversationPane({
  docked,
  collapsed,
  onCollapsedChange,
  projectId,
  messages,
  onSend,
  inFlight,
  onBesidePhoto,
  envelope,
  hideComposer,
  lastUserMessage,
  versions,
  onCompareSelect,
  onOpenSheet,
  sheetOpenFor,
  header,
  designLoopStep,
  designLoopElapsed,
  viewProgress,
  streamError,
  photoSrc,
  photoDimensions,
  onPhotoUploaded,
  onPhotoError,
  onEnsureProject,
}: ConversationPaneProps) {
  return (
    <div
      className="app-left"
      data-testid="app-left-pane"
      style={
        docked
          ? {
              // Issue #194 docked layout: a full-width bar across the
              // bottom band. The canvas keeps 100% − 48vh above it, so
              // the pane (and the failure card inside it) can no longer
              // overlap the build plate; the filmstrip keeps its
              // bottom inset inside that band (its own box, unchanged).
              position: "absolute",
              left: 0,
              right: 0,
              bottom: 0,
              width: "100%",
              height: `${CONVERSATION_DOCK_HEIGHT_VH}vh`,
              zIndex: Z_INDEX.conversation,
              display: "flex",
              flexDirection: "column",
              gap: 12,
              padding: 12,
              minWidth: 0,
              overflowY: "auto",
              boxSizing: "border-box",
              borderTop: "1px solid var(--color-hairline)",
              background:
                "color-mix(in srgb, var(--color-panel) 92%, transparent)",
            }
          : {
              position: "absolute",
              top: OVERLAY_INSET_PX,
              left: OVERLAY_INSET_PX,
              width: collapsed ? 240 : 420,
              // The pane's bottom stops clear of the filmstrip's box: the
              // filmstrip sits at the bottom inset with a 96px track, and
              // the pane must not cover the filmstrip's expand mark — the
              // history sheet's only entry point (issue #184). The calc
              // reserves, in order: the top inset (24), the filmstrip's
              // 96px track, the bottom inset (24), plus a 12px clear gap
              // above the track and a 12px clear gap below the top inset.
              // Total 168; at the 640 floor the pane spans 24→496,
              // leaving 24px above the track's top (520) and the full
              // filmstrip box (520→616) unobstructed.
              height: `calc(100% - ${OVERLAY_INSET_PX * 2 + 96 + 12 + 12}px)`, // 168
              zIndex: Z_INDEX.conversation,
              display: "flex",
              flexDirection: "column",
              gap: 12,
              minWidth: 0,
              overflowY: "auto",
              boxSizing: "border-box",
            }
      }
    >
      {docked && (
        <div
          data-testid="conversation-docked-notice"
          style={{ flex: "0 0 auto", fontSize: 12, color: "var(--color-fg-2)" }}
        >
          {copy.shell.conversationDocked}
        </div>
      )}
      {/* The conversation rail: collapsed keeps the last summary
          legible (copy.shell.conversationCollapsed). */}
      {collapsed ? (
        <button
          type="button"
          data-testid="conversation-rail"
          onClick={() => onCollapsedChange(false)}
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
          {header}
          {/* FLEX_FILL keeps this wrapper bounded so the transcript
              (div.chat-messages) is the DELIVERED sole scroll container
              (issue #220, adversarial round 1 — the ticket's mechanism
              text names the pane, but the pane's own overflowY:auto is
              intentionally left inert and the transcript does the
              scrolling). If this wrapper ever stops bounding, the pane's
              overflowY:auto becomes the effective scroller again and the
              photo block re-enters the scroll flow (the pre-fix overlap). */}
          <div style={{ ...FLEX_FILL, display: "flex", flexDirection: "column", gap: 12 }}>
            {/* Issue #194: the filmstrip lives INSIDE the docked bar —
                the same component and the same wiring as the floating
                instance (which is absent in the docked case, so exactly
                one strip renders), rendered in normal flow above the
                conversation content. The pane no longer covers the
                strip's box, so the expand marks stay reachable. */}
            {docked && projectId !== null && (
              <Filmstrip
                versions={versions}
                passInFlight={inFlight}
                pendingName={lastUserMessage || null}
                inset={0}
                docked
                onCompareSelect={onCompareSelect}
                onOpenSheet={onOpenSheet}
                sheetOpenFor={sheetOpenFor}
              />
            )}
            {/* Issue #220: the transcript is the pane's sole scroll
                container (ChatPanel's div.chat-messages now bounds and
                scrolls itself). The photo surface and the dimension
                canvas sit as pinned flex: 0 0 auto siblings below it —
                always visible, never scrolled with the transcript, and
                never overlapping it at any scroll position. */}
            <ChatPanel
              messages={messages}
              onSend={onSend}
              inFlight={inFlight}
              onBesidePhoto={onBesidePhoto}
              envelope={envelope}
              keptVersion={
                versions.length > 0 ? versions[versions.length - 1].name : null
              }
              hideComposer={hideComposer}
            />
            {inFlight && (
              <PassProgress
                step={designLoopStep}
                elapsed={designLoopElapsed}
                viewProgress={viewProgress}
              />
            )}
            {streamError && <FailureCard error={streamError} />}
            <div style={{ flex: "0 0 auto" }}>
              <PhotoUpload
                projectId={projectId ?? undefined}
                onEnsureProject={onEnsureProject}
                onUploaded={onPhotoUploaded}
                onError={onPhotoError}
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
          </div>
        </>
      )}
      {/* The sheet is a STAGE-LEVEL sibling (see the stage-level render
          site in App.tsx): in the docked layout it occupies the canvas
          band above this bar (top = the inset, bottom = band height +
          inset), never a child of the bar — the bar is positioned, so
          a child's top/bottom would resolve against the bar, not the
          stage (the issue #194 adversarial-fix defect). */}
    </div>
  );
}
