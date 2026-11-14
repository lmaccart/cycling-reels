import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';
import { TransitionSeries, linearTiming } from '@remotion/transitions';
import { fade } from '@remotion/transitions/fade';
import './fonts';
import { ExhibitView, ForegroundView } from './components/Exhibit';
import { MusicTrack } from './components/MusicTrack';
import { ProgressBarView } from './components/ProgressBar';
import { RouteMapView } from './components/RouteMap';
import { SafeArea, Vignette } from './components/Overlays';
import { SegmentAudio, SegmentView } from './components/SegmentView';
import { SfxTrack } from './components/SfxTrack';
import { StatChips } from './components/StatChips';
import { TextLayers } from './components/TextLayers';
import { TitleCardView } from './components/TitleCard';
import type { ReelProps } from './types';

export const CyclingReel: React.FC<ReelProps> = (p) => {
  const exhibits = (depth: 'front' | 'behind') =>
    (p.exhibits ?? [])
      .filter((e) => e.depth === depth)
      .map((e, i) => (
        <Sequence key={`exhibit-${depth}-${i}`} from={e.startFrame} durationInFrames={e.endFrame - e.startFrame}>
          <ExhibitView exhibit={e} accent={p.brand.accent} />
        </Sequence>
      ));
  return (
    <AbsoluteFill style={{ backgroundColor: '#000' }}>
      <TransitionSeries>
        {p.segments.map((seg, i) => (
          <React.Fragment key={i}>
            {i > 0 && seg.transitionInFrames > 0 ? (
              <TransitionSeries.Transition
                presentation={fade()}
                timing={linearTiming({ durationInFrames: seg.transitionInFrames })}
              />
            ) : null}
            <TransitionSeries.Sequence durationInFrames={seg.durationInFrames}>
              <SegmentView seg={seg} />
            </TransitionSeries.Sequence>
          </React.Fragment>
        ))}
      </TransitionSeries>

      {/* Exhibits: 'behind' ones sit under the rider's cut-out; all of them under the text. */}
      {exhibits('behind')}
      {(p.foregrounds ?? []).map((fg, i) => (
        <Sequence key={`fg-${i}`} from={fg.startFrame} durationInFrames={fg.durationInFrames}>
          <ForegroundView fg={fg} />
        </Sequence>
      ))}
      <Vignette />
      {exhibits('front')}
      <StatChips stats={p.stats} />
      {(p.routes ?? []).map((r, i) => (
        <Sequence key={`route-${i}`} from={r.startFrame} durationInFrames={r.endFrame - r.startFrame}>
          <RouteMapView route={r} accent={p.brand.accent} />
        </Sequence>
      ))}

      {(p.progress ?? []).map((b, i) => (
        <Sequence key={`progress-${i}`} from={b.startFrame} durationInFrames={b.endFrame - b.startFrame}>
          <ProgressBarView bar={b} accent={p.brand.accent} />
        </Sequence>
      ))}

      {p.titleCard ? (
        <Sequence from={p.titleCard.startFrame} durationInFrames={p.titleCard.endFrame - p.titleCard.startFrame}>
          <TitleCardView card={p.titleCard} />
        </Sequence>
      ) : null}

      <TextLayers {...p} />

      {p.music ? <MusicTrack music={p.music} totalFrames={p.totalFrames} /> : null}
      {p.sfx?.length ? <SfxTrack cues={p.sfx} /> : null}
      {p.safeArea ? <SafeArea /> : null}
    </AbsoluteFill>
  );
};

// The text and the vignette only, on a transparent background: the reels CLI renders stills of this
// for reels whose picture is plain footage (cuts and cutaways) and lays them over the clips with ffmpeg,
// instead of screenshotting every frame. Same components as above, so the text looks identical.
export const ReelOverlay: React.FC<ReelProps> = (p) => (
  <AbsoluteFill>
    <Vignette />
    <TextLayers {...p} />
  </AbsoluteFill>
);

// The reel's sound only: each clip's own audio at the same volumes and times as the full reel, the music and the
// sound effects, with no pictures, so an audio-only render doesn't decode every video frame.
export const ReelAudio: React.FC<ReelProps> = (p) => (
  <AbsoluteFill>
    <TransitionSeries>
      {p.segments.map((seg, i) => (
        <React.Fragment key={i}>
          {i > 0 && seg.transitionInFrames > 0 ? (
            <TransitionSeries.Transition
              presentation={fade()}
              timing={linearTiming({ durationInFrames: seg.transitionInFrames })}
            />
          ) : null}
          <TransitionSeries.Sequence durationInFrames={seg.durationInFrames}>
            <SegmentAudio seg={seg} />
          </TransitionSeries.Sequence>
        </React.Fragment>
      ))}
    </TransitionSeries>
    {p.music ? <MusicTrack music={p.music} totalFrames={p.totalFrames} /> : null}
    {p.sfx?.length ? <SfxTrack cues={p.sfx} /> : null}
  </AbsoluteFill>
);
