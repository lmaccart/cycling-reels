import React from 'react';
import { AbsoluteFill, Sequence, useCurrentFrame } from 'remotion';
import type { Foreground, ReelProps, Segment } from '../types';
import { ForegroundView } from './Exhibit';
import { SegmentAudio, SegmentView } from './SegmentView';

const layer = (seg: Segment): number => Number((seg.track ?? 'V1').slice(1));
const ordered = (segments: Segment[]) =>
  segments.map((seg, index) => ({seg, index})).sort((a, b) =>
    layer(a.seg) - layer(b.seg) || (a.seg.startFrame ?? 0) - (b.seg.startFrame ?? 0) || a.index - b.index);

type PositionedForeground = Foreground & {
  ownedBehindRanges?: [number, number][];
  globalBehindRanges?: [number, number][];
};

export const inOwnedForegroundRange = (fg: Foreground, frame: number): boolean => {
  const ranges = (fg as PositionedForeground).ownedBehindRanges;
  return !ranges || ranges.some(([start, end]) => frame >= start && frame < end);
};

const pictureOpacity = (seg: Segment, frame: number): number => {
  const start = seg.startFrame ?? 0;
  if (frame < start || frame >= start + seg.durationInFrames) return 0;
  return seg.transitionInFrames ? Math.min(1, (frame - start) / seg.transitionInFrames) : 1;
};

/** Global behind exhibits sit above footage, but below each visible subject. */
export const GlobalForegrounds: React.FC<{p: ReelProps}> = ({p}) => {
  const frame = useCurrentFrame();
  const clips = ordered(p.segments);
  const globalBehind = (p.exhibits ?? []).some((e) =>
    e.depth === 'behind' && (e as {ownerSegmentIndex?: number}).ownerSegmentIndex === undefined
    && frame >= e.startFrame && frame < e.endFrame);
  if (!globalBehind) return null;
  return (
    <AbsoluteFill style={{isolation: 'isolate', pointerEvents: 'none'}}>
      {clips.map(({seg, index}, order) => {
        const fg = (p.foregrounds ?? []).find((item) => item.seg === index) as PositionedForeground | undefined;
        if (!fg || (fg.globalBehindRanges && !fg.globalBehindRanges.some(([a, b]) => frame >= a && frame < b))) return null;
        const opacity = clips.slice(order + 1).reduce(
          (value, upper) => value * (1 - pictureOpacity(upper.seg, frame)), pictureOpacity(seg, frame));
        if (opacity <= 0) return null;
        return (
          <AbsoluteFill key={index} style={{opacity, mixBlendMode: 'plus-lighter'}}>
            <Sequence from={fg.startFrame} durationInFrames={fg.durationInFrames}><ForegroundView fg={fg}/></Sequence>
          </AbsoluteFill>
        );
      })}
    </AbsoluteFill>
  );
};

const PlacedPicture: React.FC<{seg: Segment; index: number; renderOverlays?: (index: number) => React.ReactNode}> = ({
  seg, index, renderOverlays,
}) => {
  const frame = useCurrentFrame();
  const start = seg.startFrame ?? 0;
  if (frame < start || frame >= start + seg.durationInFrames) return null;
  const opacity = pictureOpacity(seg, frame);
  return (
    <AbsoluteFill style={{opacity}}>
      <Sequence from={start} durationInFrames={seg.durationInFrames}><SegmentView seg={seg}/></Sequence>
      {renderOverlays?.(index)}
    </AbsoluteFill>
  );
};

/** Absolute starts retain source holes; every clip keeps its own local clock/audio. */
export const PlacedSegments: React.FC<{
  segments: Segment[]; audioOnly?: boolean; renderOverlays?: (index: number) => React.ReactNode;
}> = ({segments, audioOnly, renderOverlays}) => (
  <AbsoluteFill>
    {ordered(segments).map(({seg, index}) => (
      audioOnly
        ? <Sequence key={index} from={seg.startFrame ?? 0} durationInFrames={seg.durationInFrames}>
            <SegmentAudio seg={seg}/>
          </Sequence>
        : <PlacedPicture key={index} seg={seg} index={index} renderOverlays={renderOverlays}/>
    ))}
  </AbsoluteFill>
);

/** Keep absolute overlay clocks, but group each overlay with its actual owner. */
export const ownerProps = (input: ReelProps, index: number | null): ReelProps => {
  const owned = (value: object): boolean => {
    const owner = (value as {ownerSegmentIndex?: number}).ownerSegmentIndex;
    return index === null ? owner === undefined : owner === index;
  };
  return {
    ...input,
    hook: index === null ? input.hook : null,
    endCard: index === null ? input.endCard : null,
    titleCard: index === null ? input.titleCard : undefined,
    beats: input.beats.filter(owned), captions: input.captions.filter(owned),
    stats: input.stats.filter(owned), routes: input.routes?.filter(owned),
    progress: input.progress?.filter(owned), exhibits: input.exhibits?.filter(owned),
    foregrounds: input.foregrounds?.filter(owned),
  };
};
