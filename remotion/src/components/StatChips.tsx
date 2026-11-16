import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';
import { TEXT } from '../fonts';
import type { StatChip } from '../types';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;

// Small, low-key data pills top-left ("grade 12%"), not a HUD.
export const StatChips: React.FC<{ stats: StatChip[] }> = ({ stats }) => {
  const frame = useCurrentFrame();
  const visible = stats.filter((s) => frame >= s.startFrame && frame < s.endFrame);
  if (visible.length === 0) {
    return null;
  }
  return (
    <AbsoluteFill style={{ justifyContent: 'flex-start', alignItems: 'flex-start', padding: '240px 0 0 60px' }}>
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 12 }}>
        {visible.map((s) => {
          const local = frame - s.startFrame;
          const dur = s.endFrame - s.startFrame;
          const shortOwned = dur < 8 && (s as StatChip & {ownerSegmentIndex?: number}).ownerSegmentIndex !== undefined;
          const opacity = shortOwned ? 1 : Math.min(interpolate(local, [0, 4], [0, 1], clamp), interpolate(local, [dur - 4, dur], [1, 0], clamp));
          return (
            <div
              key={`${s.label}-${s.startFrame}`}
              style={{
                opacity,
                fontFamily: TEXT,
                fontSize: 40,
                lineHeight: 1,
                color: '#fff',
                background: 'rgba(0,0,0,0.55)',
                borderRadius: 999,
                padding: '12px 24px 14px',
                whiteSpace: 'nowrap',
              }}
            >
              <span style={{ fontWeight: 500, opacity: 0.8 }}>{s.label.toLowerCase()}</span>{' '}
              <ChipValue chip={s} local={local} />
              {s.unit ? <span style={{ fontWeight: 500, opacity: 0.8 }}> {s.unit}</span> : null}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

// Live GPS values tick over every few frames; tabular digits in a fixed-width box
// so the pill doesn't twitch when 9 becomes 10.
const ChipValue: React.FC<{ chip: StatChip; local: number }> = ({ chip, local }) => {
  if (!chip.live || chip.live.values.length === 0) {
    return <span style={{ fontWeight: 800 }}>{chip.value}</span>;
  }
  const { values, everyFrames } = chip.live;
  const i = Math.min(values.length - 1, Math.max(0, Math.floor(local / everyFrames)));
  const width = Math.max(...values.map((v) => v.length));
  return (
    <span
      style={{
        fontWeight: 800,
        fontVariantNumeric: 'tabular-nums',
        display: 'inline-block',
        minWidth: `${width}ch`,
        textAlign: 'right',
      }}
    >
      {values[i]}
    </span>
  );
};
