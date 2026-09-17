import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

// Issue #132: flat config for the web workspace (ESLint 9, TS/TSX + React
// hooks).
//
// `reportUnusedDisableDirectives` is set to "off" (the default is "warn")
// so the gate exits 0 on a clean tree. The existing
// `// eslint-disable-next-line react-hooks/exhaustive-deps` comments are
// intentional: several of them sit above effects whose full dependency list
// is deliberately narrowed. Turning the meta-rule on would flag the
// resize effect in ModelViewer.tsx as an unused directive and block the
// gate over a warning, not an error.
//
// The two scoped `off` rules are structural, not silencing:
//   - @typescript-eslint/no-unused-vars: TS already enforces this for the
//     project (noUnusedLocals / noUnusedParameters in tsconfig.json);
//     the JS rule would double-report without adding coverage.
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
    linterOptions: {
      reportUnusedDisableDirectives: "off",
    },
  },
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
      // TS itself flags unused locals/params (noUnusedLocals/noUnusedParameters
      // are on in tsconfig.json); the JS rule would double-report every
      // conventionally underscore-prefixed argument.
      "@typescript-eslint/no-unused-vars": "off",
    },
  },
  {
    // `require('three')` inside vi.mock factory callbacks is deliberate: the
    // factories run before the module under test is imported, and top-level
    // `import { ... } from "three"` in the test file would bypass the mocks.
    files: ["**/__tests__/**/*.ts", "**/__tests__/**/*.tsx"],
    rules: {
      "@typescript-eslint/no-require-imports": "off",
    },
  },
];
