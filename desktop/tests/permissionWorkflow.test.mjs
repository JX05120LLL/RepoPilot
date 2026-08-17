import assert from "node:assert/strict";
import test from "node:test";

import { resolveComposerMode } from "../node_modules/.cache/repopilot-tests/permissionWorkflow.js";

test("fully local project access always enters code research instead of plain chat", () => {
  assert.equal(resolveComposerMode("chat", "full-local", true), "research");
  assert.equal(resolveComposerMode("auto", "full-local", true), "research");
});

test("plain chat remains available without a project or in approval mode", () => {
  assert.equal(resolveComposerMode("chat", "full-local", false), "chat");
  assert.equal(resolveComposerMode("chat", "safe-isolated", true), "chat");
});
