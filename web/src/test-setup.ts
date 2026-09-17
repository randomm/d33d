import "@testing-library/jest-dom";

// jsdom does not implement scrollIntoView — provide a no-op stub.
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});

// jsdom does not implement ResizeObserver — provide a no-op stub.
// The bar's anchoring uses it to read the stage element's clientWidth/Height.
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// jsdom does not implement URL.createObjectURL
if (!URL.createObjectURL) {
  Object.defineProperty(URL, "createObjectURL", {
    value: () => "blob:mock-url",
    writable: true,
  });
}
