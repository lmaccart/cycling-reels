import React from 'react';
import { Easing, Img, OffthreadVideo, interpolate, staticFile, useCurrentFrame } from 'remotion';
import type { Segment } from '../types';

const clamp = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;

// Punch-in: a snappy zoom that settles (no overshoot).
const punchEase = Easing.bezier(0.25, 0.9, 0.3, 1);

const punchAt = (keys: [number, number, number, number][], f: number): [number, number, number] => {
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
export const SegmentView: React.FC<{ seg: Segment }> = ({ seg }) => {
  if (seg.kind === 'video') {
    return <VideoView seg={seg} />;
  }
  return <PhotoView seg={seg} />;
};

const VideoView: React.FC<{ seg: Segment }> = ({ seg }) => {
  const frame = useCurrentFrame();
  const keys = seg.volumeKeys;
  const audible = keys && keys.length > 1 ? keys.some(([, v]) => v > 0) : seg.volume > 0;
  // Ride-sound moments: the clip's volume follows keyframes (frame from the segment start).
  const volume =
    keys && keys.length > 1
      ? (f: number) => interpolate(f, keys.map(([k]) => k), keys.map(([, v]) => v), clamp)
      : seg.volume;
  const style: React.CSSProperties = { width: '100%', height: '100%', objectFit: 'cover' };
  if (seg.punch && seg.punch.length) {
    // Punch-ins zoom only the footage; text, captions and pills sit on top, unscaled.
    const [scale, x, y] = punchAt(seg.punch, frame);
    if (scale !== 1) {
      style.transform = `scale(${scale})`;
      style.transformOrigin = `${x * 100}% ${y * 100}%`;
    }
  }
  return <OffthreadVideo src={staticFile(seg.src)} volume={volume} muted={!audible} style={style} />;
};

const PhotoView: React.FC<{ seg: Segment }> = ({ seg }) => {
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

  const src = staticFile(seg.src);
  const origin = `${fx * 100}% ${fy * 100}%`;
  const transform = `translate(${tx}%, ${ty}%) scale(${scale})`;

  if (seg.fit === 'blurfill') {
    // The blurred backdrop is a blurred copy of the same photo, pre-rendered by
    // the CLI next to it (<name>.bg.jpg) because CSS blur is slow headless.
    const bg = staticFile(seg.src.replace(/\.jpg$/i, '.bg.jpg'));
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
