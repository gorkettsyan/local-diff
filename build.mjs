// Bundles the CLI (Node) and the page script (browser) into dist/.
import { build } from "esbuild";
import { cpSync, writeFileSync } from "node:fs";

// Theme used by the page; every other Shiki theme is left out of the browser bundle.
const THEMES = ["github-dark", "github-light"];

/**
 * Keeps the browser bundle small. Shiki ships every grammar (~8 MB) and theme (~1.3 MB); the page
 * only needs the grammars for the files in that particular diff, which the CLI embeds in the page.
 * So `shiki/langs` is replaced by a module serving those embedded grammars, and unused themes are stubbed.
 */
const slimShiki = {
  name: "slim-shiki",
  setup(b) {
    b.onResolve({ filter: /^shiki\/langs$/ }, () => ({ path: "page-grammars", namespace: "slim-shiki" }));
    b.onResolve({ filter: /^@shikijs\/themes\/[\w-]+$/ }, (args) =>
      THEMES.includes(args.path.split("/").pop()) ? undefined : { path: "missing-theme", namespace: "slim-shiki" },
    );
    b.onLoad({ filter: /^page-grammars$/, namespace: "slim-shiki" }, () => ({
      loader: "js",
      contents: `
        const el = document.getElementById("diff-grammars");
        const grammars = el ? JSON.parse(el.textContent) : {};
        export const bundledLanguages = {};
        for (const [name, data] of Object.entries(grammars)) {
          bundledLanguages[name] = () => Promise.resolve({ default: data });
        }
      `,
    }));
    b.onLoad({ filter: /^missing-theme$/, namespace: "slim-shiki" }, () => ({
      loader: "js",
      contents: `throw new Error("local-diff: this theme is not bundled");`,
    }));
  },
};

const [, client] = await Promise.all([
  build({
    entryPoints: ["src/cli.ts"],
    outfile: "dist/local-diff.js",
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node20",
    banner: { js: "#!/usr/bin/env node" },
  }),
  // A classic (non-module) script: ES modules can't load from file:// pages.
  build({
    entryPoints: ["src/client.ts"],
    outfile: "dist/client.js",
    bundle: true,
    platform: "browser",
    format: "iife",
    target: "es2022",
    minify: true,
    legalComments: "none",
    plugins: [slimShiki],
    metafile: !!process.env.ANALYZE,
  }),
]);

if (process.env.ANALYZE) writeFileSync("dist/meta.json", JSON.stringify(client.metafile));
cpSync("templates/diff.html", "dist/diff.html");
console.log("Built dist/local-diff.js, dist/client.js, dist/diff.html");
