import React from 'react';
import { Composition } from 'remotion';
import { CyclingReel, ReelAudio, ReelOverlay } from './CyclingReel';
import { defaultProps } from './defaultProps';
import type { ReelProps } from './types';

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="CyclingReel"
        component={CyclingReel}
        width={1080}
        height={1920}
        fps={30}
        durationInFrames={defaultProps.totalFrames}
        defaultProps={defaultProps}
        calculateMetadata={({ props }: { props: ReelProps }) => ({
          durationInFrames: Math.max(1, Math.round(props.totalFrames)),
          fps: props.fps,
          width: props.width,
          height: props.height,
        })}
      />
      <Composition
        id="ReelOverlay"
        component={ReelOverlay}
        width={1080}
        height={1920}
        fps={30}
        durationInFrames={defaultProps.totalFrames}
        defaultProps={defaultProps}
        calculateMetadata={({ props }: { props: ReelProps }) => ({
          durationInFrames: Math.max(1, Math.round(props.totalFrames)),
          fps: props.fps,
          width: props.width,
          height: props.height,
        })}
      />
      <Composition
        id="ReelAudio"
        component={ReelAudio}
        width={1080}
        height={1920}
        fps={30}
        durationInFrames={defaultProps.totalFrames}
        defaultProps={defaultProps}
        calculateMetadata={({ props }: { props: ReelProps }) => ({
          durationInFrames: Math.max(1, Math.round(props.totalFrames)),
          fps: props.fps,
          width: props.width,
          height: props.height,
        })}
      />
    </>
  );
};
