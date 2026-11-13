import { cancelRender, continueRender, delayRender } from 'remotion';
import w500 from '@fontsource/tiktok-sans/files/tiktok-sans-latin-500-normal.woff2';
import w600 from '@fontsource/tiktok-sans/files/tiktok-sans-latin-600-normal.woff2';
import w700 from '@fontsource/tiktok-sans/files/tiktok-sans-latin-700-normal.woff2';
import w800 from '@fontsource/tiktok-sans/files/tiktok-sans-latin-800-normal.woff2';
import rye from '@fontsource/rye/files/rye-latin-400-normal.woff2';
import fellItalic from '@fontsource/im-fell-english/files/im-fell-english-latin-400-italic.woff2';

// TikTok Sans (TikTok's own typeface, SIL Open Font License), bundled from npm
// so renders never need the network. Emoji fall back to the system emoji font
// (Apple Color Emoji on a Mac).
export const TEXT =
  'ReelSans, "Apple Color Emoji", "Noto Color Emoji", "Segoe UI Emoji", "Helvetica Neue", Arial, sans-serif';

// Old-timey faces for the series title board (SIL OFL, bundled from npm).
export const WOODTYPE = 'Rye, Georgia, serif';
export const FELL = 'FellEnglish, Georgia, serif';

const faces = [
  new FontFace('ReelSans', `url(${w500}) format('woff2')`, { weight: '500' }),
  new FontFace('ReelSans', `url(${w600}) format('woff2')`, { weight: '600' }),
  new FontFace('ReelSans', `url(${w700}) format('woff2')`, { weight: '700' }),
  new FontFace('ReelSans', `url(${w800}) format('woff2')`, { weight: '800' }),
  new FontFace('Rye', `url(${rye}) format('woff2')`, { weight: '400' }),
  new FontFace('FellEnglish', `url(${fellItalic}) format('woff2')`, { weight: '400', style: 'italic' }),
];

const handle = delayRender('Loading fonts');

Promise.all(faces.map((f) => f.load()))
  .then((loaded) => {
    loaded.forEach((f) => document.fonts.add(f));
    continueRender(handle);
  })
  .catch((err) => cancelRender(err));
