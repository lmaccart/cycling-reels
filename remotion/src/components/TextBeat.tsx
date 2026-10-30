import React from 'react';
import { AbsoluteFill } from 'remotion';
import type { TextBeat } from '../types';
import { TikTokText, useEntrance } from './TikTokText';

// A line of on-screen text over footage ("and then he attacked", "legs: gone").
export const TextBeatView: React.FC<{ beat: TextBeat }> = ({ beat }) => {
  const anim = useEntrance(beat.endFrame - beat.startFrame);
  const placement: React.CSSProperties =
    beat.position === 'top'
      ? { justifyContent: 'flex-start', paddingTop: beat.offsetTop ?? 420 }
      : beat.position === 'center'
        ? { justifyContent: 'center' }
        : { justifyContent: 'flex-end', paddingBottom: 600 };
  return (
    <AbsoluteFill style={{ alignItems: 'center', ...placement }}>
      <div style={anim}>
        <TikTokText text={beat.text} textStyle={beat.style} fontSize={56} maxWidth={860} />
      </div>
    </AbsoluteFill>
  );
};
