import React from 'react';
import { Audio, Sequence, staticFile } from 'remotion';
import type { SfxCue } from '../types';

// Sound effects: each cue is its own short audio clip on the timeline.
export const SfxTrack: React.FC<{ cues: SfxCue[] }> = ({ cues }) => (
  <>
    {cues.map((c, i) =>
      c.durationInFrames > 0 ? (
        <Sequence key={`sfx-${i}`} from={c.startFrame} durationInFrames={c.durationInFrames} layout="none">
          <Audio src={staticFile(c.src)} volume={c.volume} trimBefore={c.trimBefore ?? 0} />
        </Sequence>
      ) : null,
    )}
  </>
);
