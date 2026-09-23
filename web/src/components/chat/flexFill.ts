/**
 * Shared flex-chain style for the conversation pane (issue #220).
 *
 * The sole-scroller invariant: the transcript (div.chat-messages) is the
 * pane's sole scroll container. It — and its parent wrappers — must stay
 * BOUNDED (flex: 1 1 auto + minHeight: 0 inside a fixed-height flex
 * column) so the transcript overflows and scrolls on its own, while the
 * photo-attach block sits as a pinned flex: 0 0 auto sibling below it and
 * can never overlap the transcript at any scroll position. Both the
 * ConversationPane inner wrapper and ChatPanel reference this same style
 * object so the flex chain cannot silently diverge across the two
 * components.
 */
import type { CSSProperties } from "react";

export const FLEX_FILL: CSSProperties = { flex: "1 1 auto", minHeight: 0 };
