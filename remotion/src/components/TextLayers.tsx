import React from 'react';
import { Sequence } from 'remotion';
import type { ReelProps } from '../types';
import { Captions } from './Captions';
import { EndCardView } from './EndCard';
import { HookTitle } from './HookTitle';
import { TextBeatView } from './TextBeat';

// Text beats, speech captions, the hook and the end card, in stacking order.
// Shared by the full reel and the text-only overlay (ReelOverlay), so both draw text the same way.
export const TextLayers: React.FC<ReelProps> = (p) => (
  <>
    {p.beats.map((b, i) => (
      <Sequence key={`beat-${i}`} from={b.startFrame} durationInFrames={b.endFrame - b.startFrame}>
        <TextBeatView beat={b} />
      </Sequence>
    ))}

    <Captions pages={p.captions} brand={p.brand} />

    {p.hook ? (
      <Sequence from={p.hook.startFrame} durationInFrames={p.hook.endFrame - p.hook.startFrame}>
        <HookTitle hook={p.hook} />
      </Sequence>
    ) : null}

    {p.endCard ? (
      <Sequence from={p.endCard.startFrame} durationInFrames={p.endCard.endFrame - p.endCard.startFrame}>
        <EndCardView card={p.endCard} />
      </Sequence>
    ) : null}
  </>
);
