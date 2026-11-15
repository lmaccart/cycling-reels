import React from 'react';
import { AbsoluteFill } from 'remotion';
import type { Hook } from '../types';
import { TEXT } from '../fonts';
import { TikTokText, useEntrance } from './TikTokText';

// Opening line, placed where TikTok creators put it: upper third (or centre).
// A hook that opens the reel is fully visible on frame 0: that frame is the feed's first
// impression, and every loop replay starts there. Only its exit animates.
export const HookTitle: React.FC<{ hook: Hook }> = ({ hook }) => {
  const dur = hook.endFrame - hook.startFrame;
  const anim = useEntrance(dur, { enter: hook.startFrame > 0 });
  const big = hook.size === 'big';
  const fontSize = big ? 168 : hook.text.length > 70 ? 52 : hook.text.length > 42 ? 58 : 64;
  const placement: React.CSSProperties =
    hook.position === 'center'
      ? { justifyContent: 'center' }
      : { justifyContent: 'flex-start', paddingTop: hook.offsetTop ?? 420 };
  if (big) {
    // a statement hook: clean off-white type with a soft glow, no box and no outline (trend look)
    return (
      <AbsoluteFill style={{ alignItems: 'center', ...placement }}>
        <div style={anim}>
          <div
            style={{
              fontFamily: TEXT,
              fontWeight: 600,
              fontSize,
              lineHeight: 1.06,
              letterSpacing: '0.01em',
              textAlign: 'center',
              color: '#fff8e6',
              textShadow: '0 0 18px rgba(0,0,0,0.35), 0 2px 6px rgba(0,0,0,0.3)',
              whiteSpace: 'pre-wrap',
              maxWidth: 1000,
            }}
          >
            {hook.text}
          </div>
        </div>
      </AbsoluteFill>
    );
  }
  return (
    <AbsoluteFill style={{ alignItems: 'center', ...placement }}>
      <div style={anim}>
        <TikTokText text={hook.text} textStyle={hook.style} fontSize={fontSize} maxWidth={880} />
      </div>
    </AbsoluteFill>
  );
};
