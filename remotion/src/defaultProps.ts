import type { ReelProps } from './types';

// Studio preview of the text styles only (black background — the template has
// no visuals of its own; real renders get footage and props from the reels CLI).
export const defaultProps: ReelProps = {
  width: 1080,
  height: 1920,
  fps: 30,
  totalFrames: 300,
  brand: { handle: '@yourhandle', accent: '#FFE14D', captionStyle: 'native', textStyle: 'light' },
  segments: [],
  hook: { text: 'this hill had no business being this steep', startFrame: 0, endFrame: 72, style: 'light', position: 'top' },
  captions: [
    {
      startFrame: 80,
      endFrame: 140,
      words: [
        { text: 'okay', startFrame: 80, endFrame: 92 },
        { text: 'last', startFrame: 92, endFrame: 100 },
        { text: 'kick', startFrame: 100, endFrame: 108 },
        { text: 'to', startFrame: 108, endFrame: 114 },
        { text: 'the', startFrame: 114, endFrame: 120 },
        { text: 'top', startFrame: 120, endFrame: 140 },
      ],
    },
  ],
  beats: [{ text: 'legs said no', startFrame: 150, endFrame: 220, position: 'center', style: 'dark' }],
  stats: [{ label: 'Grade', value: '12%', startFrame: 20, endFrame: 140 }],
  music: null,
  endCard: null,
  safeArea: true,
};
