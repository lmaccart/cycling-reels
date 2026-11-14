// Fast path for plain-footage reels (see scripts/reels/overlay.py):
//   node scripts/overlay.mjs <job.json>
// job = {props, publicDir, audioOut, stillsDir, frames: [frame, ...], concurrency?, browserExecutable?}
// Renders the reel's audio exactly as the full render would (ReelAudio: the same sound, no pictures) and one transparent
// PNG of the text layer (ReelOverlay) per listed frame, as <stillsDir>/<frame>.png. Bundles once and reuses one browser.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { bundle } from '@remotion/bundler';
import { openBrowser, renderMedia, renderStill, selectComposition } from '@remotion/renderer';

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
  await Promise.all([audio.then(() => lap('audio')), Promise.all(Array.from({ length: workers }, work)).then(() => lap(`${job.frames.length} stills`))]);
} finally {
  await browser.close({ silent: true });
}
