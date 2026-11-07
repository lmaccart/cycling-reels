import React from 'react';
import { AbsoluteFill, Easing, interpolate, useCurrentFrame } from 'remotion';
import { TEXT } from '../fonts';
import type { ProgressBar } from '../types';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;
// Full-width card under Instagram's top bar (220px), clear of the right-hand buttons
// (from y=1000). Same numbers as BAR_TOP / BAR_HEIGHT in scripts/reels/progress.py,
// which pushes top text and the hook below it.
const TOP = 240;
const HEIGHT = 200;
const SIDE = 60;

// Series progress ("road to 5 w/kg"): the user's own FTP / weight, from the series'
// first entry (empty bar) to the goal (full). Fills from the previous entry.
export const ProgressBarView: React.FC<{ bar: ProgressBar; accent: string }> = ({ bar, accent }) => {
  const frame = useCurrentFrame();
  const dur = bar.endFrame - bar.startFrame;
  const opacity = Math.min(interpolate(frame, [0, 6], [0, 1], clamp), interpolate(frame, [dur - 5, dur], [1, 0], clamp));
  const t = bar.fillFrames > 0 ? interpolate(frame, [6, 6 + bar.fillFrames], [0, 1], { ...clamp, easing: Easing.out(Easing.cubic) }) : 1;
  const fill = bar.prevFill + (bar.fill - bar.prevFill) * t;
  const value = bar.prevValue + (bar.value - bar.prevValue) * t;
  return (
    <AbsoluteFill style={{ pointerEvents: 'none' }}>
      <div
        style={{
          position: 'absolute',
          top: TOP,
          left: SIDE,
          right: SIDE,
          height: HEIGHT,
          boxSizing: 'border-box',
          opacity,
          fontFamily: TEXT,
          color: '#fff',
          background: 'rgba(0,0,0,0.55)',
          borderRadius: 36,
          padding: '26px 34px',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 34, fontWeight: 500, opacity: 0.85, lineHeight: 1 }}>
          <span>{bar.label}</span>
          <span>{bar.note}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', lineHeight: 1 }}>
          <span>
            <span style={{ fontSize: 62, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>{value.toFixed(2)}</span>
            <span style={{ fontSize: 34, fontWeight: 500, opacity: 0.85 }}> {bar.unit}</span>
          </span>
          <span style={{ fontSize: 34, fontWeight: 500, opacity: 0.85 }}>{bar.goalText}</span>
        </div>
        <div style={{ position: 'relative', height: 18, borderRadius: 9, background: 'rgba(255,255,255,0.25)' }}>
          <div
            style={{
              position: 'absolute',
              left: 0,
              top: 0,
              bottom: 0,
              width: `${Math.max(fill * 100, 0)}%`,
              minWidth: 18,
              borderRadius: 9,
              background: accent,
            }}
          />
        </div>
      </div>
    </AbsoluteFill>
  );
};
