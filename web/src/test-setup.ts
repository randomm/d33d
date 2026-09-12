import "@testing-library/jest-dom";

// jsdom does not implement scrollIntoView — provide a no-op stub.
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});

// jsdom does not implement URL.createObjectURL
if (!URL.createObjectURL) {
  Object.defineProperty(URL, "createObjectURL", {
    value: () => "blob:mock-url",
    writable: true,
  });
}
