// Lets `node --test extensions/chromecli/test` (a directory) run every *.test.mjs here.
// `node --test extensions/chromecli/test/*.test.mjs` works too.
const { readdirSync } = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

(async () => {
  for (const f of readdirSync(__dirname).filter((n) => n.endsWith(".test.mjs")).sort()) {
    await import(pathToFileURL(path.join(__dirname, f)).href);
  }
})();
