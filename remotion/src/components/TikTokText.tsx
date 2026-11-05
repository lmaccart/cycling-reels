import React from 'react';
import { interpolate, useCurrentFrame } from 'remotion';
import { TEXT } from '../fonts';
import type { TextStyle } from '../types';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;

// Even line lengths instead of a lone word on the last line, like a person would break it.
const balanced = { textWrap: 'balance' } as React.CSSProperties;

// The look of TikTok's in-app text tool: sentence-case TikTok Sans, either on
// rounded "label" boxes that hug each line (light / dark) or plain white with a
// thin outline. Nothing is uppercased or restyled — text shows as written.
export const TikTokText: React.FC<{
  text: string;
  textStyle: TextStyle;
  fontSize: number;
  maxWidth?: number;
  weight?: number;
}> = ({ text, textStyle, fontSize, maxWidth = 880, weight = 700 }) => {
  if (textStyle === 'plain') {
    return (
      <div
        style={{
          ...balanced,
          maxWidth,
          textAlign: 'center',
          fontFamily: TEXT,
          fontWeight: weight,
          fontSize,
          lineHeight: 1.22,
          color: '#fff',
          WebkitTextStroke: `${Math.max(2, Math.round(fontSize * 0.07))}px rgba(0,0,0,0.85)`,
          paintOrder: 'stroke fill',
          textShadow: '0 2px 10px rgba(0,0,0,0.35)',
          whiteSpace: 'pre-wrap',
        }}
      >
        {text}
      </div>
    );
  }
  const light = textStyle === 'light';
  return (
    <div style={{ ...balanced, maxWidth, textAlign: 'center', lineHeight: 1.36, fontSize }}>
      <span
        style={{
          fontFamily: TEXT,
          fontWeight: weight,
          fontSize,
          color: light ? '#111' : '#fff',
          background: light ? '#fff' : 'rgba(0,0,0,0.8)',
          padding: `${Math.round(fontSize * 0.1)}px ${Math.round(fontSize * 0.3)}px`,
          borderRadius: Math.round(fontSize * 0.26),
          WebkitBoxDecorationBreak: 'clone',
          boxDecorationBreak: 'clone',
          whiteSpace: 'pre-wrap',
        }}
      >
        {text}
      </span>
    </div>
  );
};

/** Quick, understated entrance/exit like TikTok's default text: tiny pop in, fade out.
 * `enter: false` = already fully there on the first frame (only the exit animates). */
export const useEntrance = (durationInFrames: number, { enter = true }: { enter?: boolean } = {}) => {
  const frame = useCurrentFrame();
  const inP = enter ? interpolate(frame, [0, 4], [0, 1], clamp) : 1;
  const outP = interpolate(frame, [durationInFrames - 4, durationInFrames], [1, 0], clamp);
  return {
    opacity: Math.min(inP, outP),
    transform: `scale(${0.94 + 0.06 * inP})`,
  };
};
