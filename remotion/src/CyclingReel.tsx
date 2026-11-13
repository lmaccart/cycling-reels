import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';
import { TransitionSeries, linearTiming } from '@remotion/transitions';
import { fade } from '@remotion/transitions/fade';
import './fonts';
import { Captions } from './components/Captions';
import { EndCardView } from './components/EndCard';
import { ExhibitView, ForegroundView } from './components/Exhibit';
import { HookTitle } from './components/HookTitle';
import { MusicTrack } from './components/MusicTrack';
import { ProgressBarView } from './components/ProgressBar';
import { RouteMapView } from './components/RouteMap';
import { SafeArea, Vignette } from './components/Overlays';
import { SegmentView } from './components/SegmentView';
import { SfxTrack } from './components/SfxTrack';
import { StatChips } from './components/StatChips';
import { TextBeatView } from './components/TextBeat';
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

      {p.music ? <MusicTrack music={p.music} totalFrames={p.totalFrames} /> : null}
      {p.sfx?.length ? <SfxTrack cues={p.sfx} /> : null}
      {p.safeArea ? <SafeArea /> : null}
    </AbsoluteFill>
  );
};
