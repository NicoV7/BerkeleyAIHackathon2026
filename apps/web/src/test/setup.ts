// Vitest global setup. Runs before each test file (configured via
// vitest.config.ts setupFiles). Wires jest-dom matchers and auto-cleanup.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

function installStorageFallback() {
  const storage = window.localStorage;
  if (typeof storage?.clear === "function") return;

  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, String(value)),
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
      key: (index: number) => Array.from(values.keys())[index] ?? null,
      get length() {
        return values.size;
      },
    },
  });
}

installStorageFallback();

afterEach(() => {
  cleanup();
});
