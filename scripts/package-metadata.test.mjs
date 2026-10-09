import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

async function readJson(relativePath) {
  return JSON.parse(await readFile(path.join(repoRoot, relativePath), "utf8"));
}

test("application versions stay aligned", async () => {
  const [
    rootPackage,
    rootLock,
    electronPackage,
    electronLock,
    electronVersion,
  ] =
    await Promise.all([
      readJson("package.json"),
      readJson("package-lock.json"),
      readJson("electron/package.json"),
      readJson("electron/package-lock.json"),
      readJson("electron/version.json"),
    ]);

  assert.equal(electronPackage.version, rootPackage.version);
  assert.equal(rootLock.version, rootPackage.version);
  assert.equal(rootLock.packages[""].version, rootPackage.version);
  assert.equal(electronLock.version, electronPackage.version);
  assert.equal(electronLock.packages[""].version, electronPackage.version);
  assert.equal(electronVersion.version, electronPackage.version);
  for (const downloadUrl of Object.values(electronVersion.downloads)) {
    assert.match(downloadUrl, new RegExp(`electron-v${electronPackage.version}/`));
    assert.match(downloadUrl, new RegExp(`Presenton-${electronPackage.version}\\.`));
  }
});

test("Docker and Electron use the same pinned presentation export", async () => {
  const [rootPackage, electronPackage, dockerfile, dockerfileDev] =
    await Promise.all([
      readJson("package.json"),
      readJson("electron/package.json"),
      readFile(path.join(repoRoot, "Dockerfile"), "utf8"),
      readFile(path.join(repoRoot, "Dockerfile.dev"), "utf8"),
    ]);

  assert.equal(
    electronPackage.exportVersion,
    rootPackage.presentationExportVersion,
  );
  assert.match(dockerfile, /COPY package\.json \/app\//);
  assert.match(dockerfile, /COPY presentation-export \/app\/presentation-export/);
  assert.match(
    dockerfile,
    /sync-presentation-export\.cjs --check-only/,
  );
  assert.doesNotMatch(dockerfile, /sync-presentation-export\.cjs --force/);
  assert.match(dockerfileDev, /COPY package\.json package-lock\.json \/app\//);
  assert.match(
    dockerfileDev,
    /sync-presentation-export\.cjs --force/,
  );
});
