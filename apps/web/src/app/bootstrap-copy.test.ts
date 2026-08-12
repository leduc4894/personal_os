import { describe, expect, it } from "vitest";

import { WORKSPACE_SHELL_HEADING } from "./bootstrap-copy";

describe("Web workspace shell", () => {
  it("identifies the bootstrap shell without product navigation", () => {
    expect(WORKSPACE_SHELL_HEADING).toBe("Workspace bootstrap ready");
  });
});
