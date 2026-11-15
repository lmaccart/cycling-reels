import { cp, mkdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const out = process.env.REELS_EDITOR_PREVIEW_OUT
  ? path.resolve(process.env.REELS_EDITOR_PREVIEW_OUT)
  : path.join(root, 'dist', 'editor-preview');

await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
await build({
  entryPoints: [path.join(root, 'editor-preview.tsx')],
  bundle: true,
  minify: true,
  format: 'iife',
  platform: 'browser',
  target: ['es2022'],
  loader: { '.woff2': 'file' },
  assetNames: 'assets/[name]-[hash]',
  outfile: path.join(out, 'editor-preview.js'),
  sourcemap: false,
});
await cp(path.join(root, 'editor-preview.html'), path.join(out, 'index.html'));
console.log(`Built editor preview at ${path.relative(root, out)}`);
