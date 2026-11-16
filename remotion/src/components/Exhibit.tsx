import React from 'react';
import { AbsoluteFill, Easing, Img, OffthreadVideo, interpolate, useCurrentFrame } from 'remotion';
import { editorStaticFile } from '../../editorStaticFile';
import { TEXT } from '../fonts';
import type { Chart, Exhibit, Foreground } from '../types';
import { punchAt } from './SegmentView';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;
const popEase = Easing.bezier(0.2, 1.3, 0.4, 1); // lands with a small overshoot
const IN = 7;
const OUT = 5;

// A study screenshot, diagram or chart the rider points at: an image the user put in exhibits/
// (or a chart of their own numbers), shown as a plain card with a soft shadow or as a raw
// cut-out. The CLI decides where it sits — typed in, or found from the pointing fingertip.
export const ExhibitView: React.FC<{ exhibit: Exhibit; accent: string }> = ({ exhibit: e, accent }) => {
  const frame = useCurrentFrame();
  const dur = e.endFrame - e.startFrame;

  let x = e.x;
  let y = e.y;
  if (e.path && e.path.length > 1) {
    const fs = e.path.map((k) => k[0]);
    x = interpolate(frame, fs, e.path.map((k) => k[1]), clamp);
    y = interpolate(frame, fs, e.path.map((k) => k[2]), clamp);
  }

  const shortOwned = dur < 8 && (e as Exhibit & {ownerSegmentIndex?: number}).ownerSegmentIndex !== undefined;
  const inP = shortOwned || e.enter === 'none' ? 1 : interpolate(frame, [0, IN], [0, 1], clamp);
  const outP = shortOwned || e.exit === 'none' ? 1 : interpolate(frame, [dur - OUT, dur], [1, 0], clamp);
  let opacity = 1;
  let scale = 1;
  let dy = 0;
  if (e.enter === 'pop') {
    scale *= 0.8 + 0.2 * popEase(inP);
    opacity *= interpolate(inP, [0, 0.5], [0, 1], clamp);
  } else if (e.enter === 'fade') {
    opacity *= inP;
  } else if (e.enter === 'slide') {
    dy += 90 * (1 - Easing.out(Easing.cubic)(inP));
    opacity *= interpolate(inP, [0, 0.6], [0, 1], clamp);
  }
  if (e.exit === 'pop') {
    scale *= 0.85 + 0.15 * outP;
    opacity *= outP;
  } else if (e.exit === 'fade') {
    opacity *= outP;
  } else if (e.exit === 'slide') {
    dy += 90 * (1 - outP);
    opacity *= outP;
  }

  const card = e.style === 'card';
  const box: React.CSSProperties = {
    position: 'absolute',
    left: x - e.width / 2,
    top: y - e.height / 2 + dy,
    width: e.width,
    height: e.height,
    boxSizing: 'border-box',
    opacity,
    transform: `rotate(${e.tilt}deg) scale(${scale})`,
    padding: e.padding,
    background: card && e.background ? e.background : undefined,
    boxShadow: card && e.shadow ? '0 10px 36px rgba(0,0,0,0.38), 0 2px 6px rgba(0,0,0,0.22)' : undefined,
    // a cut-out's shadow follows its shape
    filter: !card && e.shadow ? 'drop-shadow(0 8px 16px rgba(0,0,0,0.45))' : undefined,
  };

  return (
    <AbsoluteFill style={{ pointerEvents: 'none' }}>
      <div style={box}>
        {e.kind === 'chart' && e.chart ? (
          <ChartView chart={e.chart} width={e.width - 2 * e.padding} height={e.height - 2 * e.padding} accent={accent} />
        ) : e.src ? (
          <Img src={editorStaticFile(e.src)} style={{ display: 'block', width: '100%', height: '100%', objectFit: 'contain' }} />
        ) : null}
      </div>
    </AbsoluteFill>
  );
};

// The rider, cut out of the same clip and laid back over an exhibit ("depth": "behind"),
// so the pointing hand and the head pass in front of it. Follows the segment's punch-ins.
export const ForegroundView: React.FC<{ fg: Foreground }> = ({ fg }) => {
  const frame = useCurrentFrame();
  const style: React.CSSProperties = { width: '100%', height: '100%', objectFit: 'cover' };
  if (fg.punch && fg.punch.length) {
    const [scale, x, y] = punchAt(fg.punch, frame + fg.offset);
    if (scale !== 1) {
      style.transform = `scale(${scale})`;
      style.transformOrigin = `${x * 100}% ${y * 100}%`;
    }
  }
  return (
    <AbsoluteFill style={{ pointerEvents: 'none' }}>
      <OffthreadVideo src={editorStaticFile(fg.src)} transparent muted style={style} />
    </AbsoluteFill>
  );
};

const THEMES = {
  light: { surface: '#ffffff', ink: '#111111', muted: '#6b7280', grid: '#e5e7eb', series: '#2563eb' },
  dark: { surface: '#16181d', ink: '#f5f5f5', muted: '#a1a1aa', grid: 'rgba(255,255,255,0.16)', series: '' },
} as const;

// One series, drawn large for a phone: a line that draws itself on (or bars that grow), a quiet
// grid, and at most one labelled point. Numbers come from the CLI: the user's own, or their Strava.
export const ChartView: React.FC<{ chart: Chart; width: number; height: number; accent: string }> = ({ chart: c, width, height, accent }) => {
  const frame = useCurrentFrame();
  const th = THEMES[c.theme];
  const series = c.color || th.series || accent;
  const u = width / 640; // sizes below are for a 640px-wide chart
  const fs = 24 * u;
  const head = (c.title || c.unit ? 62 : 22) * u;
  const m = { l: 76 * u, r: 28 * u, t: head, b: (c.xLabel ? 78 : 54) * u };
  const pw = Math.max(10, width - m.l - m.r);
  const ph = Math.max(10, height - m.t - m.b);
  const x0 = c.xs[0];
  const x1 = c.xs[c.xs.length - 1];
  const bar = c.type === 'bar';
  const slot = pw / c.xs.length;
  const px = (x: number, i?: number) =>
    bar && i !== undefined ? m.l + slot * (i + 0.5) : m.l + (x1 === x0 ? 0.5 : (x - x0) / (x1 - x0)) * pw;
  const py = (y: number) => m.t + ph - ((y - c.yMin) / Math.max(1e-9, c.yMax - c.yMin)) * ph;
  const t = c.draw ? interpolate(frame, [6, 30], [0, 1], { ...clamp, easing: Easing.inOut(Easing.cubic) }) : 1;
  const markIn = c.draw ? interpolate(frame, [28, 34], [0, 1], clamp) : 1;
  const d = c.xs.map((x, i) => `${i ? 'L' : 'M'}${px(x).toFixed(1)},${py(c.ys[i]).toFixed(1)}`).join(' ');
  const text = { fontFamily: TEXT, fontSize: fs, fill: th.muted, fontVariantNumeric: 'tabular-nums' } as const;
  const barW = Math.min(slot - 2, slot * 0.68);
  const markI = c.mark ? c.xs.findIndex((x) => x === c.mark!.x) : -1;
  const mx = c.mark ? px(c.mark.x, bar && markI >= 0 ? markI : undefined) : 0;
  const my = c.mark ? py(c.mark.y) : 0;
  const labelLeft = mx > m.l + pw * 0.6;

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{ display: 'block', background: th.surface }}>
      {c.title ? (
        <text x={m.l - 52 * u} y={40 * u} style={{ fontFamily: TEXT, fontSize: 30 * u, fontWeight: 700, fill: th.ink }}>
          {c.title}
          {c.unit ? <tspan style={{ fontWeight: 500, fill: th.muted }}>{`  ${c.unit}`}</tspan> : null}
        </text>
      ) : c.unit ? (
        <text x={m.l - 52 * u} y={40 * u} style={{ ...text, fontSize: 28 * u }}>
          {c.unit}
        </text>
      ) : null}
      {c.yTicks.map((tk, i) => (
        <g key={`y${i}`}>
          <line x1={m.l} x2={m.l + pw} y1={py(tk.v)} y2={py(tk.v)} stroke={th.grid} strokeWidth={2 * u} />
          <text x={m.l - 12 * u} y={py(tk.v) + fs * 0.34} textAnchor="end" style={text}>
            {tk.label}
          </text>
        </g>
      ))}
      {c.xTicks.map((tk, i) => (
        <text key={`x${i}`} x={px(tk.v, bar ? c.xs.indexOf(tk.v) : undefined)} y={m.t + ph + 34 * u} textAnchor="middle" style={text}>
          {tk.label}
        </text>
      ))}
      {c.xLabel ? (
        <text x={m.l + pw / 2} y={height - 14 * u} textAnchor="middle" style={text}>
          {c.xLabel}
        </text>
      ) : null}
      {bar ? (
        c.xs.map((x, i) => {
          const top = py(c.yMin + (c.ys[i] - c.yMin) * t);
          const h = Math.max(0, m.t + ph - top);
          const r = Math.min(6 * u, h, barW / 2);
          const l = px(x, i) - barW / 2;
          // rounded at the data end, square on the baseline
          const path = `M${l},${m.t + ph} V${top + r} Q${l},${top} ${l + r},${top} H${l + barW - r} Q${l + barW},${top} ${l + barW},${top + r} V${m.t + ph} Z`;
          return <path key={`b${i}`} d={path} fill={series} opacity={c.mark && markI >= 0 && i !== markI ? 0.45 : 1} />;
        })
      ) : (
        <path d={d} fill="none" stroke={series} strokeWidth={5 * u} strokeLinejoin="round" strokeLinecap="round" pathLength={1}
          strokeDasharray="1 1" strokeDashoffset={1 - t} />
      )}
      {c.mark ? (
        <g opacity={markIn}>
          {bar ? null : <circle cx={mx} cy={my} r={11 * u} fill={series} stroke={th.surface} strokeWidth={4 * u} />}
          <text x={bar ? mx : mx + (labelLeft ? -20 : 20) * u} y={bar ? my - 12 * u : my - 16 * u}
            textAnchor={bar ? 'middle' : labelLeft ? 'end' : 'start'}
            style={{ fontFamily: TEXT, fontSize: 28 * u, fontWeight: 700, fill: th.ink, paintOrder: 'stroke', stroke: th.surface, strokeWidth: 8 * u, strokeLinejoin: 'round' }}>
            {c.mark.label}
          </text>
        </g>
      ) : null}
    </svg>
  );
};
