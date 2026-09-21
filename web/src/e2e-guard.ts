/**
 * E2E skip-server guard (issue #207).
 *
 * When E2E_SKIP_SERVER is set, the Playwright config spawns no server of its
 * own, so the D33D_DATA_DIR override that points a Playwright-spawned server
 * at a throwaway directory never applies — the specs would write real HTTP
 * calls into whatever data directory the operator's already-running server
 * resolved (typically the live ~/.d33d). This module holds the guard's
 * comparison logic as a pure, side-effect-free exported helper so it can be
 * unit-tested here without loading the Playwright config (whose top-level
 * mkdtemp + process handlers make it an unsuitable test target).
 *
 * playwright.config.ts calls shouldGuardE2eSkipServer(process.env.D33D_DATA_DIR)
 * at module load and throws on true.
 */

/**
 * Whether the E2E suite must refuse to run against the operator's
 * already-running server.
 *
 * Fires when the D33D_DATA_DIR value is unset, empty, or whitespace-only —
 * i.e. the operator did not declare intent to point their server at a
 * throwaway data dir. Any non-blank value (even one that resolves to
 * ~/.d33d via symlink/tilde) means the guard does NOT fire: the guard
 * validates declared intent, not the value.
 *
 * Pure: the value is a parameter, so no process.env mutation is needed to
 * test it.
 */
export function shouldGuardE2eSkipServer(dataDirValue: string | undefined): boolean {
  return (dataDirValue ?? "").trim() === "";
}
