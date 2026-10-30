import React from 'react';
import { Audio, interpolate, staticFile } from 'remotion';
import type { Music } from '../types';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;

// Background track: short fade in, ducks under speech and ride-sound moments, drops out
// for music drops, fades out at the end (loop reels: no fades, so the replay doesn't dip).
export const MusicTrack: React.FC<{ music: Music; totalFrames: number }> = ({ music, totalFrames }) => {
  const fadeOut = Math.max(1, Math.min(music.fadeOutFrames, totalFrames - 1));
  const fadeInFrames = music.fadeInFrames ?? 8;
  const volume = (f: number) => {
    const fadeIn = fadeInFrames > 0 ? interpolate(f, [0, fadeInFrames], [0, 1], clamp) : 1;
    const out = music.fadeOutFrames > 0 ? interpolate(f, [totalFrames - fadeOut, totalFrames], [1, 0], clamp) : 1;
    let duck = 1;
    for (const [s, e] of music.duckRanges) {
      if (e <= s) {
        continue;
      }
      duck = Math.min(duck, interpolate(f, [s - 8, s, e, e + 10], [1, music.duckVolume, music.duckVolume, 1], clamp));
    }
    for (const r of music.levels ?? []) {
      if (r.endFrame <= r.startFrame) {
        continue;
      }
      const a = Math.max(1, r.rampIn);
      const b = Math.max(1, r.rampOut);
      duck = Math.min(duck, interpolate(f, [r.startFrame - a, r.startFrame, r.endFrame, r.endFrame + b], [1, r.level, r.level, 1], clamp));
    }
    return music.volume * fadeIn * out * duck;
  };
  return <Audio src={staticFile(music.src)} trimBefore={music.trimBefore} volume={volume} loop />;
};
