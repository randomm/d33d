/**
 * Data-URI → ArrayBuffer decoding (issue #69).
 *
 * The design loop's version-created progress frame carries the best
 * iteration's STL as a `stl_data_uri` base64 data URI (the render worker's
 * tempdir is torn down before the frame is yielded, so only in-band bytes
 * are available). This is the SPA's sole decoder for that field — the
 * `views` map is display-only and never decoded back to bytes client-side.
 */

/**
 * Decode a `data:...;base64,` data URI into its raw byte payload.
 *
 * @throws {Error} if the URI is not a base64 data URI or its base64
 *   payload is malformed.
 */
export function dataUriToArrayBuffer(uri: string): ArrayBuffer {
  const match = /^data:[^,]*;base64,(.*)$/s.exec(uri);
  if (!match) {
    throw new Error("not a base64 data URI");
  }
  const binary = atob(match[1]);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}
