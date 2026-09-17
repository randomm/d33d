/**
 * The shared render-image shape: one produced view image (a data URL or
 * relative URL, named by view filename). App owns the instances (from the
 * SSE stream); the chat surface and the pass-card surface both display
 * them.
 */
export interface RenderImage {
  /** view filename, e.g. "view_00_front.png" */
  filename: string;
  /** data URL or relative URL */
  src: string;
}
