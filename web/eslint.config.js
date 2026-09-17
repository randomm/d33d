import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// Issue #132: flat config for the web workspace (ESLint 9, TS/TSX + React
// hooks).
//
// `reportUnusedDisableDirectives` is deliberately NOT overridden here: it
// stays at the linter default ("warn"), so a `// eslint-disable` comment
// that no longer suppresses anything is surfaced on the first lint run
// instead of rotting silently. Warnings do not affect `eslint .`'s exit
// code (the gate runs with no --max-warnings), so an honest tree stays
// green while inert directives stay visible.
//
// The two scoped `off` rules are structural, not silencing:
//   - @typescript-eslint/no-unused-vars: tsconfig.json sets noUnusedLocals
//     and noUnusedParameters (confirmed present), so tsc --noEmit already
//     hard-blocks unused locals and params; the ESLint rule would only
//     duplicate a check the type-check gate enforces.
//   - @typescript-eslint/no-require-imports (tests only): `require('three')`
//     inside vi.mock factory callbacks is the only import form that
//     resolves through the mocks; top-level imports would bypass them.
const jsRecommended = { ...js.configs.recommended };
// eslint-recommended (the second of the two configs in tseslint.configs.recommended)
// turns the core `no-unused-vars` off in favour of the @typescript-eslint variant.
// We keep that override; the config below then turns the TS variant off too
// because tsc --noEmit already enforces noUnusedLocals/noUnusedParameters.
const tsEslintRecommended = [...tseslint.configs.recommended];

export default [
  {
    ignores: ["node_modules/", "dist/", "test-results/", "playwright-report/"],
  },
  jsRecommended,
  ...tsEslintRecommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      // off (structural, not cosmetic): tsconfig.json sets noUnusedLocals and
      // noUnusedParameters, so tsc --noEmit already hard-blocks unused locals
      // and params. Turning this ESLint rule on would only duplicate a check
      // the type-check gate enforces (and re-flag underscore-prefixed
      // intentional arguments the compiler is configured to accept).
      "@typescript-eslint/no-unused-vars": "off",
    },
  },
  {
    // off (structural, test-scoped only): `require('three')` inside vi.mock
    // factory callbacks is deliberate — the factories run before the module
    // under test is imported, and a top-level `import ... from "three"` in
    // the test file would bypass the mocks. The scoping is genuinely
    // test-only: the files glob matches `**/__tests__/**`, and a grep of
    // src/ shows require() calls only in `__tests__` test files — src
    // outside __tests__ is never covered by this block.
    files: ["**/__tests__/**/*.ts", "**/__tests__/**/*.tsx"],
    rules: {
      "@typescript-eslint/no-require-imports": "off",
    },
  },
];
