import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';
import { TEXT } from '../fonts';
import type { Brand, CaptionPage } from '../types';
import { TikTokText } from './TikTokText';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;

// Speech captions from the transcript, in the lower third.
// native: the whole phrase in plain white, the way TikTok's auto-captions look.
// highlight: same, with the word being spoken in the accent colour.
export const Captions: React.FC<{ pages: CaptionPage[]; brand: Brand }> = ({ pages, brand }) => {
  const frame = useCurrentFrame();
  const page = pages.find((p) => frame >= p.startFrame && frame < p.endFrame);
  if (!page) {
    return null;
  }
  const opacity = interpolate(frame - page.startFrame, [0, 2], [0.4, 1], clamp);
  const fontSize = 58;

  return (
    <AbsoluteFill style={{ justifyContent: 'flex-end', alignItems: 'center', paddingBottom: page.offsetBottom ?? 560 }}>
      <div style={{ opacity }}>
        {brand.captionStyle === 'highlight' ? (
          <div
            style={{
              ...({ textWrap: 'balance' } as React.CSSProperties),
              maxWidth: 860,
              textAlign: 'center',
              fontFamily: TEXT,
              fontWeight: 700,
              fontSize,
              lineHeight: 1.22,
              WebkitTextStroke: '4px rgba(0,0,0,0.85)',
              paintOrder: 'stroke fill',
              textShadow: '0 2px 10px rgba(0,0,0,0.35)',
            }}
          >
            {page.words.map((w, i) => (
              <React.Fragment key={i}>
                {i > 0 ? ' ' : null}
                <span style={{ color: frame >= w.startFrame && frame < w.endFrame ? brand.accent : '#fff' }}>{w.text}</span>
              </React.Fragment>
            ))}
          </div>
        ) : (
          <TikTokText text={page.words.map((w) => w.text).join(' ')} textStyle="plain" fontSize={fontSize} maxWidth={860} />
        )}
      </div>
    </AbsoluteFill>
  );
};
