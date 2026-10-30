import React from 'react';
import { AbsoluteFill } from 'remotion';
import type { Hook } from '../types';
import { TikTokText, useEntrance } from './TikTokText';

// Opening line, placed where TikTok creators put it: upper third (or centre).
export const HookTitle: React.FC<{ hook: Hook }> = ({ hook }) => {
  const dur = hook.endFrame - hook.startFrame;
  const anim = useEntrance(dur);
  const fontSize = hook.text.length > 70 ? 52 : hook.text.length > 42 ? 58 : 64;
  const placement: React.CSSProperties =
    hook.position === 'center'
      ? { justifyContent: 'center' }
      : { justifyContent: 'flex-start', paddingTop: hook.offsetTop ?? 420 };
  return (
    <AbsoluteFill style={{ alignItems: 'center', ...placement }}>
      <div style={anim}>
        <TikTokText text={hook.text} textStyle={hook.style} fontSize={fontSize} maxWidth={880} />
      </div>
    </AbsoluteFill>
  );
};
