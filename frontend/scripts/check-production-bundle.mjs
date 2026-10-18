import fs from "node:fs";
import path from "node:path";

const assets = path.resolve("dist/assets");
const marker = "LYNEWRIGHT_DESIGN_GALLERY_ONLY";
const bundles = fs.readdirSync(assets).filter((name) => name.endsWith(".js"));
const found = bundles.filter((name) => fs.readFileSync(path.join(assets, name), "utf8").includes(marker));
if (found.length) { console.error(`Dev-only gallery leaked into production bundle: ${found.join(", ")}`); process.exitCode = 1; }
else console.log("Production bundle check passed: dev-only /design gallery is absent.");
