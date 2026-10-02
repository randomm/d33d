/**
 * usePartStl — Screen 2's part-STL fetch (issue #334, D7, extracted from
 * App: the part feature used to add seven hooks/effects/wrappers to the
 * 1961-line App inline; this hook owns the fetch, its abort + sequence
 * guard, and the "only refetch when the part's identity changed" key).
 *
 * `partKey` is the part's semantic identity (filename/format/unit_status/
 * scale): a repeat refetch of the design state allocates a fresh `part`
 * object, so keying on it (not on object identity) is what stops the STL
 * from restarting on every refetch. Returns the fetched `ArrayBuffer`
 * (null while loading / on failure / with no part) — a 404 (no part) or
 * 409 (source missing) leaves it null, never a fabricated model.
 *
 * Memory: the previous buffer is explicitly zeroed and released when a new
 * part takes the slot (the 50 MB ArrayBuffer is otherwise only reclaimed
 * at GC — a long session with several re-imported parts would hold them
 * all). The viewer decodes the bytes on arrival, so the release happens
 * after the new fetch settles, before the stale buffer can be observed
 * again.
 */
import { useEffect, useRef, useState } from "react";
import type { ApiClient } from "../lib/api";

export function usePartStl(
  projectId: number | null,
  partKey: string | null,
  client: ApiClient,
): ArrayBuffer | null {
  const [data, setData] = useState<ArrayBuffer | null>(null);
  const seqRef = useRef(0);
  // The previous buffer, held only until the next buffer (or the teardown
  // to "no part") arrives — then zeroed + released (see the module doc).
  // The view (`Uint8Array`) carries `.fill` — `ArrayBuffer` itself has no
  // fill, so the release goes through the view. The held view is a
  // zero-copy wrapper over the same memory (the zero-fill releases the
  // bytes; the buffer then only holds the view's reference until GC).
  const prevViewRef = useRef<Uint8Array | null>(null);

  useEffect(() => {
    if (projectId === null || partKey === null) {
      // Leaving Screen 2 (no part, or no project): release the held buffer
      // now — the part is gone, nothing is coming.
      prevViewRef.current?.fill(0);
      prevViewRef.current = null;
      setData(null);
      return;
    }
    const seq = ++seqRef.current;
    // The in-flight fetch is aborted on cleanup (a stale response can never
    // overwrite a newer project's state; an abandoned request stops here).
    const controller = new AbortController();
    setData(null);
    client
      .fetchPartStl(projectId, controller.signal)
      .then((buf) => {
        if (!controller.signal.aborted && seq === seqRef.current) {
          // The new buffer is in; the old one is no longer observable
          // anywhere — zero it and drop the reference (the 50 MB release).
          prevViewRef.current?.fill(0);
          prevViewRef.current = new Uint8Array(buf);
          setData(buf);
        }
      })
      .catch((e) => {
        // A missing / unreadable part leaves the viewer empty (the report
        // still renders its honest state) — never a fabricated model.
        if (controller.signal.aborted || seq !== seqRef.current) return;
        console.warn("part.stl fetch failed:", e);
        prevViewRef.current?.fill(0);
        prevViewRef.current = null;
        setData(null);
      });
    return () => {
      controller.abort();
      // Unmount (or the key changing before the fetch settled): the buffer
      // is never re-served, so the zero-fill here keeps the release
      // synchronous.
      prevViewRef.current?.fill(0);
      prevViewRef.current = null;
    };
  }, [projectId, partKey, client]);

  return data;
}
