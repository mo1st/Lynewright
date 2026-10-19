import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

const script = fileURLToPath(new URL("./check-styles.mjs", import.meta.url));

function check(css, { file = "components/example.module.css", tokens = ":root { --color-text: #fff; }" } = {}) {
  const temporaryRoot = fs.realpathSync(os.tmpdir());
  const temporary = fs.mkdtempSync(path.join(temporaryRoot, "lynewright-style-check-"));
  try {
    for (const [relative, content] of [["styles/tokens.css", tokens], [file, css]]) {
      const target = path.join(temporary, "src", relative);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, content);
    }
    const result = spawnSync(process.execPath, [script], { cwd: temporary, encoding: "utf8" });
    assert.ifError(result.error);
    return { status: result.status, output: result.stdout + result.stderr };
  } finally {
    assert.equal(path.dirname(fs.realpathSync(temporary)), temporaryRoot);
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

test("accepts token colours, variables, and comments", () => {
  assert.equal(check("/* #fff !important */ .example { color: var(--color-text); }").status, 0);
});

for (const color of ["#abc", "#11223344", "rgb(1, 2, 3)", "hsl(0 0% 0%)"]) {
  test(`rejects ${color} outside tokens`, () => {
    const result = check(`.example { color: ${color}; }`);
    assert.equal(result.status, 1);
    assert.match(result.output, /color literal outside tokens/);
  });
}

for (const important of ["!important", "! IMPORTANT", "! /* comment */ important"]) {
  test(`rejects declaration priority ${important}`, () => {
    const result = check(`.example { color: var(--color-text) ${important}; }`);
    assert.equal(result.status, 1);
    assert.match(result.output, /!important declaration/);
  });
}

test("rejects important declarations in tokens", () => {
  const result = check(".example { color: var(--color-text); }", { tokens: ":root { --color-text: #fff !important; }" });
  assert.equal(result.status, 1);
});

test("rejects malformed CSS", () => {
  const result = check(".example { color: var(--color-text);");
  assert.equal(result.status, 1);
  assert.match(result.output, /Unclosed block/);
});

test("rejects CSS outside the allowed locations", () => {
  const result = check(".example { color: var(--color-text); }", { file: "other.css" });
  assert.equal(result.status, 1);
  assert.match(result.output, /CSS file must be/);
});
