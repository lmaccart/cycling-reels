import React from 'react';
import { AbsoluteFill, Easing, interpolate, spring, useCurrentFrame, useVideoConfig } from 'remotion';
import { FELL, WOODTYPE } from '../fonts';
import type { TitleCard } from '../types';

// Series title board (a generated visual: only for series the user said yes to, see
// scripts/reels/titlecard.py). Fully on screen at frame 0, then a nail flies into the
// top-left corner and the board swings on it, then fades out over the first shot.

const W = 840;
const H = 360;
const LEFT = 150; // right of centre: the swing carries the lower-left corner out to the left
const TOP = 300;
const PIVOT = { x: 66, y: 64 }; // nail position inside the board
const REST_ANGLE = 27; // degrees the board settles at, hanging from the nail

// Slightly wonky plank outline, like the sketch: fixed points so every episode matches.
const OUTLINE = [
  'M 10 22',
  'C 210 8, 500 30, 828 10',
  'C 836 120, 832 250, 838 344',
  'C 570 356, 290 338, 14 352',
  'C 6 240, 14 120, 10 22 Z',
].join(' ');

const SPLINTERS = [
  { a: -150, d: 70, r: -30, w: 16 },
  { a: -105, d: 88, r: 40, w: 11 },
  { a: -60, d: 64, r: 15, w: 14 },
  { a: -20, d: 80, r: -55, w: 9 },
  { a: 160, d: 58, r: 70, w: 12 },
  { a: 200, d: 76, r: -10, w: 10 },
  { a: 120, d: 50, r: 25, w: 8 },
];

const Wood: React.FC = () => (
  <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ position: 'absolute', inset: 0, overflow: 'visible' }}>
    <defs>
      <linearGradient id="tc-base" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#b07a43" />
        <stop offset="0.45" stopColor="#966232" />
        <stop offset="1" stopColor="#7a4b22" />
      </linearGradient>
      <radialGradient id="tc-edge" cx="0.5" cy="0.5" r="0.75">
        <stop offset="0.55" stopColor="#000" stopOpacity="0" />
        <stop offset="1" stopColor="#1e0e04" stopOpacity="0.55" />
      </radialGradient>
      <radialGradient id="tc-knot" cx="0.5" cy="0.5" r="0.5">
        <stop offset="0" stopColor="#3b200c" stopOpacity="0.95" />
        <stop offset="0.5" stopColor="#5a3415" stopOpacity="0.6" />
        <stop offset="1" stopColor="#7a4b22" stopOpacity="0" />
      </radialGradient>
      <filter id="tc-grain" x="0" y="0" width="100%" height="100%">
        <feTurbulence type="fractalNoise" baseFrequency="0.0022 0.055" numOctaves="4" seed="11" result="n" />
        <feColorMatrix in="n" type="matrix" values="0 0 0 0 0.20  0 0 0 0 0.10  0 0 0 0 0.03  3.2 0 0 0 -1.25" />
      </filter>
      <filter id="tc-fine" x="0" y="0" width="100%" height="100%">
        <feTurbulence type="fractalNoise" baseFrequency="0.012 0.32" numOctaves="2" seed="4" result="n" />
        <feColorMatrix in="n" type="matrix" values="0 0 0 0 0.16  0 0 0 0 0.08  0 0 0 0 0.02  2.4 0 0 0 -0.95" />
      </filter>
      <clipPath id="tc-clip">
        <path d={OUTLINE} />
      </clipPath>
    </defs>
    <path d={OUTLINE} fill="url(#tc-base)" />
    <g clipPath="url(#tc-clip)">
      <rect width={W} height={H} filter="url(#tc-grain)" />
      <rect width={W} height={H} filter="url(#tc-fine)" opacity={0.7} />
      <ellipse cx={610} cy={250} rx={34} ry={13} fill="url(#tc-knot)" />
      <ellipse cx={610} cy={250} rx={58} ry={22} fill="none" stroke="#4a2a10" strokeOpacity={0.25} strokeWidth={2} />
      <ellipse cx={250} cy={300} rx={22} ry={8} fill="url(#tc-knot)" />
      <rect width={W} height={H} fill="url(#tc-edge)" />
      {/* top-edge highlight and bottom-edge shade read as the board's thickness */}
      <path d="M 10 26 C 210 12, 500 34, 828 14" stroke="#e2b27a" strokeOpacity={0.45} strokeWidth={5} fill="none" />
      <path d="M 14 348 C 290 334, 570 352, 838 340" stroke="#2a1405" strokeOpacity={0.6} strokeWidth={10} fill="none" />
    </g>
    <path d={OUTLINE} fill="none" stroke="#3a1f0b" strokeWidth={4} />
  </svg>
);

// Lettering burned into the wood: dark fill, a light lip underneath, a shade on top.
const carved: React.CSSProperties = {
  color: '#2a1407',
  textShadow: '0 3px 0 rgba(255, 214, 160, 0.30), 0 -2px 1px rgba(0, 0, 0, 0.45)',
  whiteSpace: 'nowrap',
  lineHeight: 1,
};

const NailHead: React.FC<{ scale: number }> = ({ scale }) => (
  <svg width={60} height={60} viewBox="-30 -30 60 60"
    style={{ position: 'absolute', left: PIVOT.x - 30, top: PIVOT.y - 30, transform: `scale(${scale})`, overflow: 'visible' }}>
    <defs>
      <radialGradient id="tc-head" cx="0.35" cy="0.35" r="0.7">
        <stop offset="0" stopColor="#e6e6e6" />
        <stop offset="0.5" stopColor="#8d8d8d" />
        <stop offset="1" stopColor="#3c3c3c" />
      </radialGradient>
    </defs>
    <circle r={19} cx={2} cy={4} fill="#1a0c03" opacity={0.55} />
    <circle r={17} fill="url(#tc-head)" stroke="#2b2b2b" strokeWidth={2} />
  </svg>
);

// Side view of the nail in flight, pointing along its path.
const FlyingNail: React.FC<{ x: number; y: number; angle: number }> = ({ x, y, angle }) => (
  <svg width={150} height={40} viewBox="-140 -20 150 40"
    style={{ position: 'absolute', left: x - 140, top: y - 20, transformOrigin: '140px 20px', transform: `rotate(${angle}deg) scale(1.6)`, overflow: 'visible' }}>
    <defs>
      <linearGradient id="tc-shaft" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#d9d9d9" />
        <stop offset="0.5" stopColor="#8a8a8a" />
        <stop offset="1" stopColor="#4a4a4a" />
      </linearGradient>
    </defs>
    {/* speed lines */}
    <path d="M -230 -9 L -150 -9 M -260 0 L -150 0 M -220 9 L -150 9" stroke="#fff" strokeOpacity={0.55} strokeWidth={3} strokeLinecap="round" />
    <rect x={-130} y={-17} width={8} height={34} rx={2} fill="url(#tc-shaft)" stroke="#2b2b2b" strokeWidth={1.5} />
    <path d="M -122 -5 L -6 -4 L 8 0 L -6 4 L -122 5 Z" fill="url(#tc-shaft)" stroke="#2b2b2b" strokeWidth={1.5} />
  </svg>
);

export const TitleCardView: React.FC<{ card: TitleCard }> = ({ card }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const dur = card.endFrame - card.startFrame;

  // nail in flight: from off-screen top-left, accelerating into the corner
  const fly = interpolate(frame, [card.nailFrame, card.hitFrame], [0, 1], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.in(Easing.sin),
  });
  const from = { x: -300, y: -400 };
  const target = { x: LEFT + PIVOT.x, y: TOP + PIVOT.y };
  const nx = target.x + from.x * (1 - fly);
  const ny = target.y + from.y * (1 - fly);
  const flightAngle = (Math.atan2(-from.y, -from.x) * 180) / Math.PI;
  const inFlight = frame >= card.nailFrame && frame < card.hitFrame;
  const hit = frame >= card.hitFrame;

  // impact: a short jolt of the board, the nail head punching in, splinters flying off
  const since = frame - card.hitFrame;
  const jolt = hit ? Math.exp(-since / 2.5) * Math.sin(since * 2.4) * 7 : 0;
  const headScale = hit ? interpolate(since, [0, 4], [1.7, 1], { extrapolateRight: 'clamp' }) : 1;
  const burst = hit ? interpolate(since, [0, 10], [0, 1], { extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic) }) : 0;
  const burstOpacity = hit ? interpolate(since, [0, 4, 12], [1, 1, 0], { extrapolateRight: 'clamp' }) : 0;

  // the swing: a loose spring around the nail that overshoots and settles at REST_ANGLE
  const s = spring({ frame: frame - card.swingFrame, fps, config: { damping: 7, stiffness: 90, mass: 1.1 } });
  const angle = frame >= card.swingFrame ? REST_ANGLE * s : 0;

  const opacity = interpolate(frame, [dur - card.fadeFrames, dur], [1, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });

  return (
    <AbsoluteFill style={{ opacity }}>
      <div
        style={{
          position: 'absolute', left: LEFT, top: TOP, width: W, height: H,
          transformOrigin: `${PIVOT.x}px ${PIVOT.y}px`,
          transform: `translate(${jolt * 0.4}px, ${jolt}px) rotate(${angle}deg)`,
          filter: 'drop-shadow(0 18px 22px rgba(0, 0, 0, 0.55))',
        }}
      >
        <Wood />
        <div style={{ position: 'absolute', left: 0, right: 0, top: 58, textAlign: 'center', fontFamily: WOODTYPE, fontSize: 88, ...carved }}>
          {card.title}
        </div>
        <div style={{ position: 'absolute', left: 78, right: 64, top: 210, display: 'flex', alignItems: 'baseline', justifyContent: card.episode ? 'space-between' : 'center' }}>
          {card.episode ? <span style={{ fontFamily: WOODTYPE, fontSize: 72, ...carved }}>{card.episode}</span> : null}
          {card.subtitle ? <span style={{ fontFamily: FELL, fontStyle: 'italic', fontSize: 70, ...carved }}>{card.subtitle}</span> : null}
        </div>
        {hit ? <NailHead scale={headScale} /> : null}
      </div>

      {hit && burstOpacity > 0
        ? SPLINTERS.map((p, i) => {
            const rad = (p.a * Math.PI) / 180;
            const x = target.x + Math.cos(rad) * p.d * burst;
            const y = target.y + Math.sin(rad) * p.d * burst + 30 * burst * burst;
            return (
              <div key={i} style={{
                position: 'absolute', left: x, top: y, width: p.w, height: 4, borderRadius: 2,
                background: i % 2 ? '#c99358' : '#7a4b22', opacity: burstOpacity,
                transform: `rotate(${p.r + 180 * burst}deg)`,
              }} />
            );
          })
        : null}

      {inFlight ? <FlyingNail x={nx} y={ny} angle={flightAngle} /> : null}
    </AbsoluteFill>
  );
};
