import path from "node:path";

/** Keep downloads inside the caller's export namespace (or legacy admin root). */
export function getSafeExportName(
  decodedName: string | null,
  userId: string | null,
  isAdmin: boolean,
): string | null {
  if (!decodedName || decodedName.includes("\\") || path.isAbsolute(decodedName)) {
    return null;
  }
  const normalized = path.normalize(decodedName);
  if (normalized === ".." || normalized.startsWith(`..${path.sep}`)) {
    return null;
  }
  if (!userId) return normalized;
  const parts = normalized.split(path.sep);
  if (parts[0] === "users") {
    return parts.length >= 3 && parts[1] === userId ? normalized : null;
  }
  return isAdmin && parts.length === 1 ? normalized : null;
}
