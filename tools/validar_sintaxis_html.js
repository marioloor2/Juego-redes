"use strict";

const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const scriptPattern = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
let match;
let inlineCount = 0;

while ((match = scriptPattern.exec(html))) {
  const attributes = match[1];
  const source = match[2];
  if (/\bsrc\s*=/.test(attributes) || /\btype\s*=\s*["']module["']/.test(attributes)) continue;
  inlineCount += 1;
  try {
    // Parsing only: the DOM-dependent application code is not executed here.
    new Function(source);
  } catch (error) {
    throw new Error(`Error de sintaxis en el script inline ${inlineCount}: ${error.message}`);
  }
}

if (!inlineCount) throw new Error("No se encontraron scripts inline para validar");
console.log(JSON.stringify({ ok: true, inlineScripts: inlineCount }));
