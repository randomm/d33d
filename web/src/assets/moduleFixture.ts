/**
 * Named-module GLB fixture (issue #29), inlined as base64.
 *
 * Live module-registry wiring (`POST /api/projects/{id}/module-registry`)
 * is explicitly deferred per issue #29's settled design decision — this
 * checked-in fixture (identical bytes to
 * `web/tests/fixtures/viewer/mini-model.glb`, already used by
 * model-viewer.test.ts) stands in as the sole moduleGroup source for
 * `resolvePointPick` in this ticket. Inlined (not fetched at
 * runtime) so it never competes with `window.fetch` stubs other tests
 * install for the backend API, and never trips `tsconfig.json`'s
 * `rootDir: "src"` constraint that a static import reaching outside
 * `src/` would.
 */
const MODULE_FIXTURE_GLB_BASE64 =
  "Z2xURgIAAAA8AwAAMAIAAEpTT057ImFzc2V0IjogeyJ2ZXJzaW9uIjogIjIuMCIsICJnZW5lcmF0b3IiOiAiZDMzZC10ZXN0In0sICJzY2VuZSI6IDAsICJzY2VuZXMiOiBbeyJub2RlcyI6IFswXX1dLCAibm9kZXMiOiBbeyJtZXNoIjogMH1dLCAibWVzaGVzIjogW3sicHJpbWl0aXZlcyI6IFt7ImF0dHJpYnV0ZXMiOiB7IlBPU0lUSU9OIjogMH0sICJpbmRpY2VzIjogMX1dfV0sICJhY2Nlc3NvcnMiOiBbeyJidWZmZXJWaWV3IjogMCwgImNvbXBvbmVudFR5cGUiOiA1MTI2LCAiY291bnQiOiA4LCAidHlwZSI6ICJWRUMzIiwgIm1pbiI6IFstNS4wLCAtNS4wLCAtNS4wXSwgIm1heCI6IFs1LjAsIDUuMCwgNS4wXX0sIHsiYnVmZmVyVmlldyI6IDEsICJjb21wb25lbnRUeXBlIjogNTEyNSwgImNvdW50IjogMzYsICJ0eXBlIjogIlNDQUxBUiJ9XSwgImJ1ZmZlclZpZXdzIjogW3siYnVmZmVyIjogMCwgImJ5dGVPZmZzZXQiOiAwLCAiYnl0ZUxlbmd0aCI6IDk2fSwgeyJidWZmZXIiOiAwLCAiYnl0ZU9mZnNldCI6IDk2LCAiYnl0ZUxlbmd0aCI6IDE0NH1dLCAiYnVmZmVycyI6IFt7ImJ5dGVMZW5ndGgiOiAyNDB9XX0gIPAAAABCSU4AAACgwAAAoMAAAKDAAACgwAAAoMAAAKBAAACgwAAAoEAAAKDAAACgwAAAoEAAAKBAAACgQAAAoMAAAKDAAACgQAAAoMAAAKBAAACgQAAAoEAAAKDAAACgQAAAoEAAAKBAAQAAAAMAAAAAAAAABAAAAAEAAAAAAAAAAAAAAAMAAAACAAAAAgAAAAQAAAAAAAAAAQAAAAcAAAADAAAABQAAAAEAAAAEAAAABQAAAAcAAAABAAAAAwAAAAcAAAACAAAABgAAAAQAAAACAAAAAgAAAAcAAAAGAAAABgAAAAUAAAAEAAAABwAAAAUAAAAGAAAA";

/** Decode the inlined base64 GLB fixture into an ArrayBuffer. */
export function loadModuleFixtureArrayBuffer(): ArrayBuffer {
  const binary = atob(MODULE_FIXTURE_GLB_BASE64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}
