/**
 * VariantGallery — the pinned variant grid (issue #8, surface 2).
 *
 * The Midjourney-grid pattern adapted to named parameter sets: cards with
 * thumbnail + parameter set + the closed action set (set-as-main,
 * branch-from, archive). This is the "browse my options" surface, distinct
 * from the linear timeline. Archived variants are hidden from the default
 * listing (the backend's `?archived=1` reveals them).
 */

import type { GalleryCard } from "../../lib/api";

interface VariantGalleryProps {
  cards: GalleryCard[];
  onSetAsMain?: (versionId: number) => void;
  onBranchFrom?: (versionId: number) => void;
  onArchive?: (versionId: number, archived: boolean) => void;
}

function paramsSummary(card: GalleryCard): string {
  // The parameter summary — every key: value, comma-separated.
  return Object.entries(card.params)
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
}

export function VariantGallery({
  cards,
  onSetAsMain,
  onBranchFrom,
  onArchive,
}: VariantGalleryProps) {
  if (cards.length === 0) {
    return (
      <section className="variant-gallery" data-testid="variant-gallery-empty">
        <p>No pinned variants — pin a version from the timeline to browse it here.</p>
      </section>
    );
  }
  return (
    <section className="variant-gallery" data-testid="variant-gallery" aria-label="Pinned variant gallery">
      <h2>
        Pinned variants{" "}
        <span data-testid="variant-gallery-count" className="gallery-count">
          {cards.length}
        </span>
      </h2>
      <div className="gallery-grid" data-testid="gallery-grid">
        {cards.map((card) => (
          <div key={card.id} className="gallery-card" data-testid={`gallery-card-${card.id}`}>
            {card.thumbnail && (
              <img
                className="gallery-thumb"
                src={card.thumbnail}
                alt={`${card.name} thumbnail`}
                data-testid={`gallery-thumb-${card.id}`}
              />
            )}
            <span className="gallery-name" data-testid={`gallery-name-${card.id}`}>
              {card.name}
            </span>
            <span className="gallery-params" data-testid={`gallery-params-${card.id}`}>
              {paramsSummary(card)}
            </span>
            <div className="gallery-actions" data-testid={`gallery-actions-${card.id}`}>
              <button
                type="button"
                className="gallery-action set-as-main"
                data-testid={`gallery-action-set-as-main-${card.id}`}
                onClick={() => onSetAsMain?.(card.id)}
              >
                Set as main
              </button>
              <button
                type="button"
                className="gallery-action branch-from"
                data-testid={`gallery-action-branch-from-${card.id}`}
                onClick={() => onBranchFrom?.(card.id)}
              >
                Branch from
              </button>
              <button
                type="button"
                className="gallery-action archive"
                data-testid={`gallery-action-archive-${card.id}`}
                onClick={() => onArchive?.(card.id, !card.archived)}
              >
                {card.archived ? "Unarchive" : "Archive"}
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
