import fs from "node:fs";
import path from "node:path";

const root = path.resolve("src");
const legacy = new Set(["styles/tokens.css", "styles/base.css"]);
function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(dir, entry.name);
    return entry.isDirectory() ? walk(file) : file.endsWith(".css") ? [file] : [];
  });
}
const commentsRemoved = (value) => value.replace(/\/\*[\s\S]*?\*\//g, "");
const files = walk(root);
const failures = [];
for (const file of files) {
  const rel = path.relative(root, file).replaceAll("\\", "/");
  if (!legacy.has(rel) && !/^(?:components|pages)\/.+\.module\.css$/.test(rel)) failures.push(`${rel}: CSS file must be a token/base file or a component/page CSS Module`);
  const source = commentsRemoved(fs.readFileSync(file, "utf8"));
  if (rel !== "styles/tokens.css" && /#[0-9a-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\s*\(/i.test(source)) failures.push(`${rel}: color literal outside tokens.css`);
  if (/!important/i.test(source)) failures.push(`${rel}: !important declaration is not on the allowlist`);
}
if (failures.length) { console.error(failures.join("\n")); process.exitCode = 1; }
else console.log(`Style guards passed: ${files.length} stylesheets, 0 color literals outside tokens.css, 0 !important declarations.`);
