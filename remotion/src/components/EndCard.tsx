import React from 'react';
import { AbsoluteFill } from 'remotion';
import type { EndCard } from '../types';
import { TikTokText, useEntrance } from './TikTokText';

// Optional closing line over the last moments of footage (off by default — reels loop).
export const EndCardView: React.FC<{ card: EndCard }> = ({ card }) => {
  const anim = useEntrance(card.endFrame - card.startFrame);
  return (
    <AbsoluteFill style={{ justifyContent: 'center', alignItems: 'center' }}>
      <div style={{ ...anim, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 18 }}>
        {card.cta ? <TikTokText text={card.cta} textStyle={card.style} fontSize={56} /> : null}
        {card.handle ? <TikTokText text={card.handle} textStyle="plain" fontSize={44} weight={600} /> : null}
      </div>
    </AbsoluteFill>
  );
};
