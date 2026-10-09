import assert from "node:assert/strict";
import test from "node:test";
import { loadTsModule } from "./load-ts-module.mjs";

const { validateLayoutCode, LayoutCodeValidationError } = loadTsModule("lib/validate-layout-code.ts");
const validCode = `
import { z } from 'zod';
const layoutId = 'rail-test';
const layoutName = 'Rail test';
const layoutDescription = 'Test layout';
const Schema = z.object({ title: z.string() });
function dynamicSlideLayout() { return <div>Rail</div>; }
`;

test("accepts a genuine TSX/zod layout and extracts its metadata", () => {
  const result = validateLayoutCode(validCode);
  assert.equal(result.layoutId, "rail-test");
  assert.equal(result.layoutName, "Rail test");
  assert.equal(result.schemaJSON.type, "object");
});
test("strips model-returned code fences", () => {
  assert.equal(validateLayoutCode("\`\`\`tsx\n" + validCode + "\n\`\`\`").layoutId, "rail-test");
});
test("rejects empty and syntactically invalid layouts", () => {
  for (const code of ["", "const Schema = ;"]) assert.throws(() => validateLayoutCode(code), LayoutCodeValidationError);
});
test("rejects missing layout and metadata declarations", () => {
  for (const code of [
    validCode.replace("function dynamicSlideLayout", "function wrongLayout"),
    validCode.replace("const layoutId = 'rail-test';", ""),
    validCode.replace("const layoutName = 'Rail test';", "const layoutName = '';"),
  ]) assert.throws(() => validateLayoutCode(code), LayoutCodeValidationError);
});
test("rejects unsafe schema expressions instead of executing them", () => {
  assert.throws(() => validateLayoutCode(validCode.replace("z.string()", "z.string().constructor('return process')()")), LayoutCodeValidationError);
});
