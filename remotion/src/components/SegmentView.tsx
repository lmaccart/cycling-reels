import React from 'react';
import { AbsoluteFill, Audio, Easing, Img, OffthreadVideo, Sequence, interpolate, useCurrentFrame } from 'remotion';
import { editorStaticFile } from '../../editorStaticFile';
import type { Cutaway, Segment } from '../types';

/** What a still needs to be shown: a photo segment, or a photo cutaway. */
type Still = Pick<Segment, 'src' | 'durationInFrames' | 'motion' | 'fit' | 'focusX' | 'focusY'>;

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;

// Punch-in: a snappy zoom that settles (no overshoot).
const punchEase = Easing.bezier(0.25, 0.9, 0.3, 1);

export const punchAt = (keys: [number, number, number, number][], f: number): [number, number, number] => {
  if (f <= keys[0][0]) {
    return [keys[0][1], keys[0][2], keys[0][3]];
  }
  for (let i = 1; i < keys.length; i++) {
    const [b, s1, x1, y1] = keys[i];
    if (f < b) {
      const [a, s0, x0, y0] = keys[i - 1];
      const t = punchEase((f - a) / Math.max(1, b - a));
      return [s0 + (s1 - s0) * t, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t];
    }
  }
  const last = keys[keys.length - 1];
  return [last[1], last[2], last[3]];
};

// Only two kinds of visuals exist: a clip or a photo from the user's library.
// Video clips arrive pre-cut, pre-cropped and pre-timed at 9:16 by the CLI
// (ffmpeg, 1080x1920 or larger for punch-ins), so the template only lays them on the timeline.
// Cutaways are more of the same, laid over the segment: the picture changes, the segment's
// audio (its voice) carries on underneath, and text/captions stay on top of everything.
export const SegmentView: React.FC<{ seg: Segment }> = ({ seg }) => {
  const base = seg.kind === 'video' ? <VideoView seg={seg} /> : <PhotoView seg={seg} />;
  if (!seg.cutaways?.length) {
    return base;
  }
  return (
    <AbsoluteFill>
      {base}
      {seg.cutaways.map((c, i) => (
        <Sequence key={`cutaway-${i}`} from={c.startFrame} durationInFrames={c.durationInFrames}>
          <CutawayView cut={c} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};

const CutawayView: React.FC<{ cut: Cutaway }> = ({ cut }) => {
  if (cut.kind === 'video') {
    return (
      <OffthreadVideo src={editorStaticFile(cut.src)} muted style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
    );
  }
  return <PhotoView seg={cut} />;
};

/** A video segment's own sound: whether it plays, and its volume (ride-sound "nat" moments follow keyframes,
 * frame from the segment start). Shared with the audio-only render (SegmentAudio) so both mix the same. */
export const clipVolume = (seg: Segment): { audible: boolean; volume: number | ((f: number) => number) } => {
  const keys = seg.volumeKeys;
  if (keys && keys.length > 1) {
    return {
      audible: keys.some(([, v]) => v > 0),
      volume: (f: number) => interpolate(f, keys.map(([k]) => k), keys.map(([, v]) => v), clamp),
    };
  }
  return { audible: seg.volume > 0, volume: seg.volume };
};

/** The segment's sound without its picture, for the audio-only render. */
export const SegmentAudio: React.FC<{ seg: Segment }> = ({ seg }) => {
  if (seg.kind !== 'video') {
    return null;
  }
  const { audible, volume } = clipVolume(seg);
  return audible ? <Audio src={editorStaticFile(seg.src)} volume={volume} /> : null;
};

const VideoView: React.FC<{ seg: Segment }> = ({ seg }) => {
  const frame = useCurrentFrame();
  const { audible, volume } = clipVolume(seg);
  const style: React.CSSProperties = { width: '100%', height: '100%', objectFit: 'cover' };
  if (seg.punch && seg.punch.length) {
    // Punch-ins zoom only the footage; text, captions and pills sit on top, unscaled.
    const [scale, x, y] = punchAt(seg.punch, frame);
    if (scale !== 1) {
      style.transform = `scale(${scale})`;
      style.transformOrigin = `${x * 100}% ${y * 100}%`;
    }
  }
  const main = <OffthreadVideo src={editorStaticFile(seg.src)} volume={volume} muted={!audible} style={style} />;
  const patches = (seg.blur ?? []).filter((b) => frame >= b.keys[0][0] && frame <= b.keys[b.keys.length - 1][0]);
  if (!patches.length) {
    return main;
  }
  // Blur patches: a blurred copy of the same clip, clipped to a soft-edged ellipse that moves along the keys.
  // It gets the same transform as the footage, so it follows punch-ins.
  return (
    <AbsoluteFill>
      {main}
      {patches.map((b, i) => {
        const at = (n: number) => interpolate(frame, b.keys.map((k) => k[0]), b.keys.map((k) => k[n]), clamp);
        const [x, y, w, h] = [at(1), at(2), at(3), at(4)];
        const mask = `radial-gradient(ellipse ${w * 50}% ${h * 50}% at ${x * 100}% ${y * 100}%, #000 78%, transparent 100%)`;
        return (
          <AbsoluteFill key={`blur-${i}`} style={{ transform: style.transform, transformOrigin: style.transformOrigin }}>
            <AbsoluteFill style={{ maskImage: mask, WebkitMaskImage: mask }}>
              <OffthreadVideo
                src={editorStaticFile(seg.src)}
                muted
                style={{ width: '100%', height: '100%', objectFit: 'cover', filter: `blur(${b.strength}px)` }}
              />
            </AbsoluteFill>
          </AbsoluteFill>
        );
      })}
    </AbsoluteFill>
  );
};

const PhotoView: React.FC<{ seg: Still }> = ({ seg }) => {
  const frame = useCurrentFrame();
  const p = interpolate(frame, [0, Math.max(1, seg.durationInFrames - 1)], [0, 1], clamp);
  const t = Easing.bezier(0.33, 0, 0.67, 1)(p);
  const fx = seg.focusX ?? 0.5;
  const fy = seg.focusY ?? 0.5;

  let scale = 1;
  let tx = 0;
  let ty = 0;
  switch (seg.motion ?? 'zoom-in') {
    case 'zoom-in':
      scale = 1 + 0.12 * t;
      break;
    case 'zoom-out':
      scale = 1.12 - 0.12 * t;
      break;
    case 'pan-left':
      scale = 1.12;
      tx = 4 - 8 * t;
      break;
    case 'pan-right':
      scale = 1.12;
      tx = -4 + 8 * t;
      break;
    case 'pan-up':
      scale = 1.12;
      ty = 4 - 8 * t;
      break;
    case 'pan-down':
      scale = 1.12;
      ty = -4 + 8 * t;
      break;
    default:
      scale = 1;
  }

  const src = editorStaticFile(seg.src);
  const origin = `${fx * 100}% ${fy * 100}%`;
  const transform = `translate(${tx}%, ${ty}%) scale(${scale})`;

  if (seg.fit === 'blurfill') {
    // The blurred backdrop is a blurred copy of the same photo, pre-rendered by
    // the CLI next to it (<name>.bg.jpg) because CSS blur is slow headless.
    const bg = editorStaticFile(seg.src.replace(/\.jpg$/i, '.bg.jpg'));
    return (
      <div style={{ position: 'absolute', inset: 0 }}>
        <Img src={bg} style={{ position: 'absolute', width: '100%', height: '100%', objectFit: 'cover' }} />
        <Img
          src={src}
          style={{
            position: 'absolute',
            width: '100%',
            height: '100%',
            objectFit: 'contain',
            transform,
            transformOrigin: origin,
          }}
        />
      </div>
    );
  }

  return (
    <Img
      src={src}
      style={{
        width: '100%',
        height: '100%',
        objectFit: 'cover',
        objectPosition: origin,
        transform,
        transformOrigin: origin,
      }}
    />
  );
};
