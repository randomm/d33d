/**
 * The 3MF download name, built from the project name and version.
 *
 * This is filename construction, not display copy: the slugification and
 * extension belong with the other filename logic (lib/), not in the copy
 * deck (copy.ts), which holds only words. `copy.shell.exportDone` renders
 * whatever name comes out of here.
 */

export const exportFilename = (project: string, version: string): string => {
  const slug = project
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  // A project name with no Latin letters or digits (accented, CJK, …)
  // slugifies to nothing; "model" keeps the download recognisable.
  const base = slug === "" ? "model" : slug;
  return `${base}-${version}.3mf`;
};
