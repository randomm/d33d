import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { provenanceReporter } from "./vitest.provenance";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/__tests__/**/*.{test,spec}.{ts,tsx}"],
    setupFiles: ["./src/test-setup.ts"],
    reporters: ["default", provenanceReporter()],
  },
});
