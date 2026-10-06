/**
 * DisabledReason — the one place the disabled-reason wiring lives
 * (issue #388 lens finding 4). A disabled control says WHY it is
 * disabled: the `disabledReasonProps(id, disabled)` helper returns
 * `{title, "aria-describedby"}` for the control (both undefined when not
 * disabled), and `<DisabledReasonHint id visible />` renders the hint
 * line the `aria-describedby` id points at (the shared
 * `copy.shell.disabledReason` string — never a per-site copy).
 *
 * Each call site passes its OWN hint id (BriefRow's id is row-scoped —
 * `brief-disabled-reason-{name}` — and its hint renders only for rows
 * that carry a send control).
 */
import { copy } from "../../copy";

/** The {title, "aria-describedby"} pair for a disabled control. Both
 *  values are undefined when the control is not disabled (the attributes
 *  must be absent, not empty, when enabled). */
export function disabledReasonProps(
  id: string,
  disabled: boolean,
): { title?: string; "aria-describedby"?: string } {
  if (!disabled) return {};
  return { title: copy.shell.disabledReason, "aria-describedby": id };
}

/** The visible hint line a `disabledReasonProps` id points at. Render
 *  alongside the control(s) it describes (omitted when `visible` is
 *  false — the attribute is absent in that render pass too). */
export function DisabledReasonHint({ id, visible }: { id: string; visible: boolean }) {
  if (!visible) return null;
  return (
    <span
      id={id}
      data-testid={id}
      style={{ fontSize: 12, color: "var(--color-muted)" }}
    >
      {copy.shell.disabledReason}
    </span>
  );
}
