/**
 * App shell — the two-pane layout from 05-spa-core.md.
 *
 * Left: chat panel (with inline render images) + photo upload.
 * Right: three.js viewer + validation status + Export 3MF.
 *
 * No auto-generated slider panel. The pinned-parameter strip is opt-in
 * and holds at most 3 user-chosen entries.
 */

import { useState, useCallback } from "react";
import { ChatPanel, type ChatMessage } from "./components/chat/ChatPanel";
import { PhotoUpload } from "./components/upload/PhotoUpload";
import { PinnedParamStrip, type PinnedParam } from "./components/strip/PinnedParamStrip";

export interface RenderImage {
  /** view filename, e.g. "view_00_front.png" */
  filename: string;
  /** data URL or relative URL */
  src: string;
}

interface AppProps {
  /** Render images to display inline in the chat. Defaults to empty. */
  renders?: RenderImage[];
}

export default function App({ renders = [] }: AppProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pinnedParams, setPinnedParams] = useState<PinnedParam[]>([]);

  const handleSendMessage = useCallback(
    (text: string) => {
      const msg: ChatMessage = {
        id: `msg-${Date.now()}`,
        role: "user",
        content: text,
      };
      setMessages((prev) => [...prev, msg]);
    },
    [],
  );

  const handlePhotoUploaded = useCallback((_photoPath: string) => {
    // Photo upload success — the photo path is now stored server-side.
    // The chat panel picks up the new photo context on next interaction.
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
        <PhotoUpload onUploaded={handlePhotoUploaded} />
        <PinnedParamStrip
          params={pinnedParams}
          onToggle={togglePinParam}
        />
      </div>

      {/* Right pane: viewer + validation status */}
      <div className="app-right" data-testid="app-right-pane">
        <div className="viewer-pane" data-testid="viewer-pane">
          {/* three.js viewer mounts here (task-d) */}
          <div className="viewer-placeholder" data-testid="viewer-placeholder">
            Model viewer
          </div>
        </div>
        <div className="validation-pane" data-testid="validation-pane">
          <span data-testid="validation-status">Waiting for render…</span>
          <button
            className="export-btn"
            data-testid="export-3mf-btn"
            disabled
          >
            Export 3MF
          </button>
        </div>
      </div>
    </div>
  );
}
