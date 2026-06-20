// Vitest global setup. Runs before each test file (configured via
// vitest.config.ts setupFiles). Wires jest-dom matchers and auto-cleanup.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
