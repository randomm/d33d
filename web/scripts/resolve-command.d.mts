/**
 * Resolve the spawn argv for the run-vitest wrapper.
 *
 * @param env - environment variables. `D33D_TEST_COMMAND` is honoured only
 *   when `D33D_TEST_MODE === "1"`.
 * @param url - `import.meta.url` of the calling module (the wrapper).
 * @returns the argv for `spawn(argv[0], argv.slice(1), ...)`.
 */
export declare function resolveCommand(env: NodeJS.ProcessEnv, url: string): string[];
