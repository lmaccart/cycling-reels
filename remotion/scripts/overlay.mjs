// Fast path for plain-footage reels (see scripts/reels/overlay.py):
//   node scripts/overlay.mjs <job.json>
// job = {props, publicDir, audioOut, stillsDir, frames: [frame, ...], chunks: [{kind, start, end, dir}, ...],
//        concurrency?, browserExecutable?}
// Renders the reel's audio exactly as the full render would (ReelAudio: the same sound, no pictures), one transparent
// PNG of the overlay layer (ReelOverlay) per listed frame, and PNG sequences for the frame ranges Chrome must draw:
// chunks = frame ranges as PNG sequences: kind 'full' = the whole opaque reel (CyclingReel), 'over' = the overlay layer.
// Text stills land as <stillsDir>/<frame>.png, chunks as <stillsDir>/<dir>/<frame>.png.
// Bundles once and reuses one browser.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { bundle } from '@remotion/bundler';
import { openBrowser, renderFrames, renderMedia, renderStill, selectComposition } from '@remotion/renderer';

const here = path.dirname(fileURLToPath(import.meta.url));
const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const inputProps = JSON.parse(fs.readFileSync(job.props, 'utf8'));
const t0 = Date.now();
const lap = (what) => console.log(`${what}: ${((Date.now() - t0) / 1000).toFixed(1)}s`);

const serveUrl = await bundle({ entryPoint: path.join(here, '..', 'src', 'index.ts'), publicDir: job.publicDir });
lap('bundled');
const browser = await openBrowser('chrome', { browserExecutable: job.browserExecutable ?? null });
try {
  const sound = await selectComposition({ serveUrl, id: 'ReelAudio', inputProps, puppeteerInstance: browser });
  const audio = renderMedia({
    serveUrl, composition: sound, inputProps, codec: 'wav', enforceAudioTrack: true,
    outputLocation: job.audioOut, puppeteerInstance: browser, overwrite: true,
  });
  const overlay = await selectComposition({ serveUrl, id: 'ReelOverlay', inputProps, puppeteerInstance: browser });
  fs.mkdirSync(job.stillsDir, { recursive: true });
  const queue = [...job.frames];
  const workers = Math.max(1, Math.min(job.concurrency ?? Math.ceil(os.cpus().length / 2), queue.length));
  const work = async () => {
    for (let f = queue.shift(); f !== undefined; f = queue.shift()) {
      await renderStill({
        serveUrl, composition: overlay, inputProps, frame: f, imageFormat: 'png',
        output: path.join(job.stillsDir, `${f}.png`), puppeteerInstance: browser, overwrite: true,
      });
    }
  };
  // Chrome's own frames, a run (consecutive chunks of one kind) per renderFrames call: <stillsDir>/<chunk dir>/<frame>.png
  const runs = async () => {
    const groups = [];
    for (const c of job.chunks ?? []) {
      const g = groups[groups.length - 1];
      if (g && g.kind === c.kind && g.end === c.start) {
        g.end = c.end;
        g.chunks.push(c);
      } else {
        groups.push({ kind: c.kind, start: c.start, end: c.end, chunks: [c] });
      }
    }
    for (const g of groups) {
      const tmp = path.join(job.stillsDir, `.tmp-${g.kind}-${g.start}`);
      await renderFrames({
        serveUrl, composition: g.kind === 'full' ? reel : overlay, inputProps, imageFormat: 'png', outputDir: tmp,
        frameRange: [g.start, g.end - 1], concurrency: job.concurrency ?? null, puppeteerInstance: browser,
        onStart: () => {}, onFrameUpdate: () => {},
      });
      for (const c of g.chunks) fs.mkdirSync(path.join(job.stillsDir, c.dir), { recursive: true });
      for (const name of fs.readdirSync(tmp)) {
        const m = /(\d+)\.png$/.exec(name);
        if (!m) continue;
        const f = Number(m[1]);
        const c = g.chunks.find((k) => f >= k.start && f < k.end);
        if (c) fs.renameSync(path.join(tmp, name), path.join(job.stillsDir, c.dir, `${f}.png`));
      }
      fs.rmSync(tmp, { recursive: true, force: true });
    }
    lap(`${groups.length} Chrome runs`);
  };
  const reel = await selectComposition({ serveUrl, id: 'CyclingReel', inputProps, puppeteerInstance: browser });
  await Promise.all([audio.then(() => lap('audio')), Promise.all(Array.from({ length: workers }, work)).then(() => lap(`${job.frames.length} stills`))]);
  await runs(); // after the stills and the sound: one browser, so fewer pages open at once
} finally {
  await browser.close({ silent: true });
}
