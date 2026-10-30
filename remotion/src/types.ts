// Props contract between the reels CLI (scripts/reels/render.py) and this
// template. Every time value is an absolute frame on the reel's timeline
// unless the field name says otherwise. Keep both sides in sync.

/** TikTok's text looks: white box + black text, black box + white text, or plain white with an outline. */
export type TextStyle = 'light' | 'dark' | 'plain';

export type Brand = {
  handle: string;
  accent: string;
  /** native = whole phrase in plain white (TikTok auto-captions); highlight = spoken word in accent colour. */
  captionStyle: 'native' | 'highlight';
  textStyle: TextStyle;
};

export type Motion =
  | 'none'
  | 'zoom-in'
  | 'zoom-out'
  | 'pan-left'
  | 'pan-right'
  | 'pan-up'
  | 'pan-down';

/** Every segment is a clip or photo from the user's own footage library — nothing else can be shown. */
export type Segment = {
  kind: 'video' | 'photo';
  /** Path relative to the render's public dir. */
  src: string;
  durationInFrames: number;
  /** Crossfade from the previous segment; 0 = hard cut. */
  transitionInFrames: number;
  /** Clip audio volume, 0..1 (video only). */
  volume: number;
  /** Optional per-frame clip volume: [frame from the segment start, volume] keyframes, linear between
   * (ride-sound "nat" moments). Overrides `volume` when present. */
  volumeKeys?: [number, number][];
  /** Optional punch-in zooms (video only): [frame from the segment start, scale, originX 0..1, originY 0..1]
   * keyframes, eased between. Only the footage zooms; the clip may be staged larger than the frame. */
  punch?: [number, number, number, number][];
  /** Photo only: slow move and framing. */
  motion?: Motion;
  fit?: 'cover' | 'blurfill';
  focusX?: number;
  focusY?: number;
};

export type CaptionWord = { text: string; startFrame: number; endFrame: number };
export type CaptionPage = { startFrame: number; endFrame: number; words: CaptionWord[] };

export type TextBeat = {
  text: string;
  startFrame: number;
  endFrame: number;
  position: 'top' | 'center' | 'lower';
  style: TextStyle;
  /** 'top' only: pixels from the top edge (the CLI pushes text below stat pills). */
  offsetTop?: number;
};

export type StatChip = {
  label: string;
  value: string;
  startFrame: number;
  endFrame: number;
  /** Shown lighter after the value ("mph"). */
  unit?: string;
  /** GPS speed: values[i] is shown from startFrame + i * everyFrames (whole units, real telemetry only). */
  live?: { everyFrames: number; values: string[] };
};

/** GPS track of the recording (lines only where the camera had a fix), 0..1 box coords, north up. */
export type RouteMap = {
  startFrame: number;
  endFrame: number;
  paths: [number, number][][];
  /** Current position for every frame from startFrame. */
  dots: [number, number][];
};

export type Hook = {
  text: string;
  startFrame: number;
  endFrame: number;
  style: TextStyle;
  position: 'top' | 'center';
  offsetTop?: number;
};

export type EndCard = { handle: string; cta: string; startFrame: number; endFrame: number; style: TextStyle };

/** A stretch where the music sits at its own level (× volume): ducked under a ride-sound moment, or 0 for a drop.
 * Ramps down over rampIn frames before startFrame and back up over rampOut frames after endFrame. */
export type MusicLevel = { startFrame: number; endFrame: number; level: number; rampIn: number; rampOut: number };

export type Music = {
  src: string;
  volume: number;
  /** Frames to skip at the start of the track. */
  trimBefore: number;
  /** [start, end) frame ranges where the music ducks under speech. */
  duckRanges: [number, number][];
  duckVolume: number;
  /** 0 = no fade out (loop reels). */
  fadeOutFrames: number;
  /** Optional; default 8. 0 = no fade in (loop reels). */
  fadeInFrames?: number;
  /** Optional; nat ducks and drops on top of the speech duck. */
  levels?: MusicLevel[];
};

/** One sound effect from the project's sfx/ library (CC0, human-made). */
export type SfxCue = {
  /** Path relative to the render's public dir (sfx/<slug>.wav). */
  src: string;
  startFrame: number;
  durationInFrames: number;
  volume: number;
  /** Frames cut off the sound's start (a cue that would start before frame 0). */
  trimBefore?: number;
};

export type ReelProps = {
  width: number;
  height: number;
  fps: number;
  totalFrames: number;
  brand: Brand;
  segments: Segment[];
  hook: Hook | null;
  captions: CaptionPage[];
  beats: TextBeat[];
  stats: StatChip[];
  /** Optional; absent in reels without a route map. */
  routes?: RouteMap[];
  music: Music | null;
  /** Optional; absent in reels without sound effects. */
  sfx?: SfxCue[];
  endCard: EndCard | null;
  /** Studio-only guide showing where Instagram's UI covers the frame. */
  safeArea: boolean;
};
