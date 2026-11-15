import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Player, type PlayerRef } from '@remotion/player';
import { CyclingReel } from './src/CyclingReel';
import type { ReelProps } from './src/types';
import './editor-preview.css';

export type EditorPreviewCommand =
  | { type: 'loadRevision'; revision: string; props: ReelProps; staticBaseUrl: string; startFrame?: number }
  | { type: 'seek'; revision: string; frame: number }
  | { type: 'play'; revision: string }
  | { type: 'pause'; revision: string }
  | { type: 'playbackRate'; revision: string; rate: number };

export type EditorPreviewEvent =
  | { type: 'ready'; revision: string; durationInFrames: number }
  | { type: 'frame'; revision: string; frame: number }
  | { type: 'error'; revision: string | null; message: string };

type LoadedRevision = {
  revision: string;
  props: ReelProps;
  staticBaseUrl: string;
  startFrame: number;
};

declare global {
  interface Window {
    webkit?: { messageHandlers?: { reelsPreview?: { postMessage: (event: EditorPreviewEvent) => void } } };
  }
}

const send = (event: EditorPreviewEvent): void => {
  const handler = window.webkit?.messageHandlers?.reelsPreview;
  if (handler) {
    handler.postMessage(event);
  } else if (window.parent !== window) {
    window.parent.postMessage({ channel: 'cycling-reel-preview', ...event }, window.location.origin);
  }
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const validRevision = (value: unknown): value is string =>
  typeof value === 'string' && value.length > 0 && value.length <= 256;

const validBaseUrl = (value: unknown): value is string => {
  if (typeof value !== 'string') return false;
  try {
    const url = new URL(value, window.location.href);
    return url.origin === window.location.origin && url.pathname.endsWith('/');
  } catch {
    return false;
  }
};

const EditorPreview: React.FC = () => {
  const playerRef = useRef<PlayerRef>(null);
  const [loaded, setLoaded] = useState<LoadedRevision | null>(null);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);

  const handleCommand = useCallback(
    (message: unknown) => {
      if (!isRecord(message) || !validRevision(message.revision)) return;
      const revision = message.revision;
      switch (message.type) {
        case 'loadRevision': {
          if (!isRecord(message.props) || !validBaseUrl(message.staticBaseUrl)) {
            send({ type: 'error', revision, message: 'Invalid revision props or static media mapping.' });
            return;
          }
          const props = message.props as unknown as ReelProps;
          if (
            !Number.isFinite(props.fps) ||
            props.fps <= 0 ||
            !Number.isFinite(props.totalFrames) ||
            props.totalFrames < 1 ||
            !Array.isArray(props.segments)
          ) {
            send({ type: 'error', revision, message: 'Revision props do not satisfy the CyclingReel contract.' });
            return;
          }
          window.__REELS_PREVIEW_STATIC_BASE__ = message.staticBaseUrl;
          playerRef.current?.pause();
          setPlaying(false);
          setRate(1);
          setLoaded({
            revision,
            props,
            staticBaseUrl: message.staticBaseUrl,
            startFrame: Number.isFinite(message.startFrame) ? Number(message.startFrame) : 0,
          });
          return;
        }
        case 'seek':
          if (loaded?.revision === revision && Number.isFinite(message.frame)) {
            playerRef.current?.seekTo(
              Math.max(0, Math.min(loaded.props.totalFrames - 1, Math.round(Number(message.frame)))),
            );
          }
          return;
        case 'play':
          if (loaded?.revision === revision) {
            playerRef.current?.play();
            setPlaying(true);
          }
          return;
        case 'pause':
          if (loaded?.revision === revision) {
            playerRef.current?.pause();
            setPlaying(false);
          }
          return;
        case 'playbackRate':
          if (loaded?.revision === revision && typeof message.rate === 'number' && Number.isFinite(message.rate)) {
            setRate(Math.max(0.25, Math.min(4, message.rate)));
          }
          return;
        default:
          return;
      }
    },
    [loaded],
  );

  useEffect(() => {
    const onMessage = (event: MessageEvent<unknown>) => {
      if (event.origin !== window.location.origin) return;
      if (event.source !== window && event.source !== window.parent) return;
      const payload = isRecord(event.data) && event.data.channel === 'cycling-reel-preview' ? event.data.command : event.data;
      handleCommand(payload);
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [handleCommand]);

  useEffect(() => {
    const player = playerRef.current;
    if (!player || !loaded) return;
    const onFrame = (event: { detail: { frame: number } }) => {
      if (Number.isFinite(event.detail.frame)) {
        send({ type: 'frame', revision: loaded.revision, frame: event.detail.frame });
      }
    };
    const onError = (event: { detail: { error: Error } }) => {
      send({ type: 'error', revision: loaded.revision, message: event.detail.error.message });
    };
    player.addEventListener('frameupdate', onFrame);
    player.addEventListener('error', onError);
    return () => {
      player.removeEventListener('frameupdate', onFrame);
      player.removeEventListener('error', onError);
    };
  }, [loaded]);

  useEffect(() => {
    if (!loaded || !playerRef.current) return;
    const frame = Math.max(0, Math.min(loaded.props.totalFrames - 1, Math.round(loaded.startFrame)));
    playerRef.current.seekTo(frame);
    const animationFrame = window.requestAnimationFrame(() => {
      send({ type: 'ready', revision: loaded.revision, durationInFrames: loaded.props.totalFrames });
      send({ type: 'frame', revision: loaded.revision, frame });
    });
    return () => window.cancelAnimationFrame(animationFrame);
  }, [loaded]);

  return loaded ? (
    <main className="preview-shell">
      <Player
        ref={playerRef}
        component={CyclingReel}
        inputProps={loaded.props}
        durationInFrames={Math.max(1, Math.round(loaded.props.totalFrames))}
        compositionWidth={loaded.props.width}
        compositionHeight={loaded.props.height}
        fps={loaded.props.fps}
        playbackRate={rate}
        autoPlay={false}
        loop={false}
        controls={false}
        clickToPlay={false}
        spaceKeyToPlayOrPause={false}
        style={{ width: '100%', height: '100%' }}
      />
      <span className="sr-only" aria-live="polite">
        {playing ? 'Playing' : 'Paused'}
      </span>
    </main>
  ) : (
    <main className="preview-empty" aria-label="Program preview waiting for a revision">
      Waiting for preview…
    </main>
  );
};

const element = document.getElementById('editor-preview-root');
if (!element) throw new Error('Missing editor preview root element.');
createRoot(element).render(<EditorPreview />);
