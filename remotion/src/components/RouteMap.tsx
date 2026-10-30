import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';
import type { RouteMap } from '../types';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;
// Top-right, level with the stat pills: clear of Instagram's top bar (220px) and
// the right-hand buttons (from y=1000). The CLI pushes top text below it (ROUTE_* in render.py).
const TOP = 240;
const SIZE = 220;
const RIGHT = 56;
const PAD = 26;

// The ride's GPS track as a plain line, with a dot where the camera is now. Drawn
// only from the camera's own GPS fixes — no map tiles or imagery.
export const RouteMapView: React.FC<{ route: RouteMap; accent: string }> = ({ route, accent }) => {
  const frame = useCurrentFrame();
  const dur = route.endFrame - route.startFrame;
  const opacity = Math.min(interpolate(frame, [0, 8], [0, 1], clamp), interpolate(frame, [dur - 5, dur], [1, 0], clamp));
  const inner = SIZE - PAD * 2;
  const pt = ([x, y]: [number, number]) => `${(PAD + x * inner).toFixed(1)},${(PAD + y * inner).toFixed(1)}`;
  const lines = route.paths.map((p) => p.map(pt).join(' '));
  const dot = route.dots[Math.min(Math.max(frame, 0), route.dots.length - 1)];
  return (
    <AbsoluteFill style={{ pointerEvents: 'none' }}>
      <svg
        width={SIZE}
        height={SIZE}
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        style={{ position: 'absolute', top: TOP, right: RIGHT, opacity }}
      >
        <rect x={0} y={0} width={SIZE} height={SIZE} rx={30} fill="rgba(0,0,0,0.38)" />
        {lines.map((d, i) => (
          <polyline key={`s${i}`} points={d} fill="none" stroke="rgba(0,0,0,0.45)" strokeWidth={9} strokeLinecap="round" strokeLinejoin="round" />
        ))}
        {lines.map((d, i) => (
          <polyline key={`l${i}`} points={d} fill="none" stroke="rgba(255,255,255,0.92)" strokeWidth={5} strokeLinecap="round" strokeLinejoin="round" />
        ))}
        {dot ? (
          <circle
            cx={PAD + dot[0] * inner}
            cy={PAD + dot[1] * inner}
            r={10}
            fill={accent}
            stroke="#fff"
            strokeWidth={3.5}
          />
        ) : null}
      </svg>
    </AbsoluteFill>
  );
};
