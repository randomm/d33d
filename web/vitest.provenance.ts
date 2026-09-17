import { createRequire } from "node:module";
import type { Reporter } from "vitest/node";

const require = createRequire(import.meta.url);

export function provenanceReporter(): Reporter {
  const projectRoot = process.cwd();
  let vitestPath: string;
  try {
    vitestPath = require.resolve("vitest");
  } catch {
    vitestPath = "(unresolvable)";
  }

  return {
    onInit() {
      console.log(`gate: project root = ${projectRoot}`);
      console.log(`gate: vitest resolved = ${vitestPath}`);
    },
  };
}
