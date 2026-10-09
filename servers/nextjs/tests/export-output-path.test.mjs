import assert from "node:assert/strict";
import test from "node:test";
import { loadTsModule } from "./load-ts-module.mjs";

const { getSafeExportName } = loadTsModule("lib/export-output-path.ts");

test("allows only the caller's per-user exports", () => {
  assert.equal(getSafeExportName("users/alice/design.pptx", "alice", false), "users/alice/design.pptx");
  assert.equal(getSafeExportName("users/bob/design.pptx", "alice", false), null);
  assert.equal(getSafeExportName("users/bob/design.pptx", "alice", true), null);
});
test("rejects traversal, absolute paths and Windows separators", () => {
  for (const name of ["../secret.pptx", "/tmp/secret.pptx", "users/alice/../../../secret.pptx", "users\\alice\\secret.pptx", null]) {
    assert.equal(getSafeExportName(name, "alice", true), null);
  }
});
test("limits legacy root exports to admins and preserves unicode names", () => {
  assert.equal(getSafeExportName("客室.pptx", "alice", false), null);
  assert.equal(getSafeExportName("客室.pptx", "alice", true), "客室.pptx");
  assert.equal(getSafeExportName("legacy/subdir/report.pptx", "alice", true), null);
});
