import React from 'react';
import { AbsoluteFill } from 'remotion';
import type { TextBeat } from '../types';
import { TikTokText, useEntrance } from './TikTokText';

// A line of on-screen text over footage ("and then he attacked", "legs: gone").
export const TextBeatView: React.FC<{ beat: TextBeat }> = ({ beat }) => {
  const duration = beat.endFrame - beat.startFrame;
  const entrance = useEntrance(duration);
  // A clipped owned fragment has no original animation-phase metadata. Avoid a
  // fresh entrance and exit making its entire short interval invisible.
  const shortOwned = duration < 8 && (beat as TextBeat & {ownerSegmentIndex?: number}).ownerSegmentIndex !== undefined;
  const anim = shortOwned ? {opacity: 1, transform: 'none'} : entrance;
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
