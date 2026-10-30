import { cancelRender, continueRender, delayRender } from 'remotion';
import w500 from '@fontsource/tiktok-sans/files/tiktok-sans-latin-500-normal.woff2';
import w600 from '@fontsource/tiktok-sans/files/tiktok-sans-latin-600-normal.woff2';
import w700 from '@fontsource/tiktok-sans/files/tiktok-sans-latin-700-normal.woff2';
import w800 from '@fontsource/tiktok-sans/files/tiktok-sans-latin-800-normal.woff2';

// TikTok Sans (TikTok's own typeface, SIL Open Font License), bundled from npm
// so renders never need the network. Emoji fall back to the system emoji font
// (Apple Color Emoji on a Mac).
export const TEXT =
  'ReelSans, "Apple Color Emoji", "Noto Color Emoji", "Segoe UI Emoji", "Helvetica Neue", Arial, sans-serif';

const faces = [
  new FontFace('ReelSans', `url(${w500}) format('woff2')`, { weight: '500' }),
  new FontFace('ReelSans', `url(${w600}) format('woff2')`, { weight: '600' }),
  new FontFace('ReelSans', `url(${w700}) format('woff2')`, { weight: '700' }),
  new FontFace('ReelSans', `url(${w800}) format('woff2')`, { weight: '800' }),
];

const handle = delayRender('Loading fonts');

Promise.all(faces.map((f) => f.load()))
  .then((loaded) => {
    loaded.forEach((f) => document.fonts.add(f));
    continueRender(handle);
  })
  .catch((err) => cancelRender(err));
