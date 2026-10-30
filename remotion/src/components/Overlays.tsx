import React from 'react';
import { AbsoluteFill } from 'remotion';

// Very light top/bottom shading so plain white text stays readable on bright sky or road.
export const Vignette: React.FC = () => (
  <AbsoluteFill
    style={{
      background:
        'linear-gradient(180deg, rgba(0,0,0,0.22) 0%, rgba(0,0,0,0) 20%, rgba(0,0,0,0) 62%, rgba(0,0,0,0.28) 100%)',
    }}
  />
);

// Studio-only guide: red areas are where Instagram's Reels UI sits.
export const SafeArea: React.FC = () => (
  <AbsoluteFill style={{ pointerEvents: 'none' }}>
    <div style={{ position: 'absolute', left: 0, right: 0, top: 0, height: 220, background: 'rgba(255,0,0,0.25)' }} />
    <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: 420, background: 'rgba(255,0,0,0.25)' }} />
    <div style={{ position: 'absolute', right: 0, width: 130, top: 1000, bottom: 420, background: 'rgba(255,0,0,0.25)' }} />
  </AbsoluteFill>
);
