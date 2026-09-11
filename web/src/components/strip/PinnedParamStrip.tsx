/**
 * PinnedParamStrip — opt-in pinned-parameter strip (max 3 entries).
 *
 * Per 05-spa-core.md: "No auto-generated slider panel. At most an opt-in
 * pinned-parameter strip of 3 that the user chooses."
 *
 * This strip starts EMPTY. The user explicitly pins a named parameter
 * (e.g. from a dimension they typed in chat) to keep it visible. The
 * strip never auto-populates from the LLM's parameter block.
 */

import { useState } from "react";

export interface PinnedParam {
  name: string;
  value: number;
  /** Unit label, e.g. "mm" */
  unit?: string;
}

interface PinnedParamStripProps {
  params: PinnedParam[];
  onToggle: (name: string, value: number) => void;
}

export const MAX_PINNED = 3;

export function PinnedParamStrip({ params, onToggle }: PinnedParamStripProps) {
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState("");
  const [newValue, setNewValue] = useState("");

  const isFull = params.length >= MAX_PINNED;

  const handleAdd = () => {
    const trimmed = newName.trim();
    const parsed = parseFloat(newValue);
    if (!trimmed || isNaN(parsed)) return;
    if (isFull) return;
    onToggle(trimmed, parsed);
    setNewName("");
    setNewValue("");
    setShowAdd(false);
  };

  return (
    <div
      className="pinned-strip"
      data-testid="pinned-strip"
      aria-label="Pinned parameters"
    >
      <div className="pinned-strip-items">
        {params.map((p) => (
          <span
            key={p.name}
            className="pinned-item"
            data-testid={`pinned-item-${p.name}`}
          >
            {p.name}: {p.value}
            {p.unit ?? "mm"}
            <button
              className="pinned-remove"
              data-testid={`pinned-remove-${p.name}`}
              onClick={() => onToggle(p.name, p.value)}
              aria-label={`Unpin ${p.name}`}
            >
              ×
            </button>
          </span>
        ))}
      </div>

      {params.length === 0 && (
        <span className="pinned-empty" data-testid="pinned-empty">
          No pinned parameters
        </span>
      )}

      {showAdd && !isFull ? (
        <div className="pinned-add-form" data-testid="pinned-add-form">
          <input
            type="text"
            placeholder="param name"
            data-testid="pinned-new-name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            aria-label="New parameter name"
          />
          <input
            type="number"
            placeholder="value (mm)"
            data-testid="pinned-new-value"
            value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
            aria-label="New parameter value"
          />
          <button onClick={handleAdd} data-testid="pinned-add-confirm">
            Add
          </button>
        </div>
      ) : (
        !isFull && (
          <button
            className="pinned-add-btn"
            data-testid="pinned-add-btn"
            onClick={() => setShowAdd(true)}
          >
            + Pin parameter
          </button>
        )
      )}
    </div>
  );
}
