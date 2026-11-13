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

/** B-roll laid over a video segment for a moment: the segment's own audio (the voice, ride sounds),
 * captions and text keep going underneath. Always a clip or photo from the user's own library,
 * pre-cut and pre-framed at 9:16 by the CLI; video cutaways are silent. */
export type Cutaway = {
  kind: 'video' | 'photo';
  /** Path relative to the render's public dir. */
  src: string;
  /** Frames from the segment start. */
  startFrame: number;
  durationInFrames: number;
  /** Photo only: slow move and framing, as on photo segments. */
  motion?: Motion;
  fit?: 'cover' | 'blurfill';
  focusX?: number;
  focusY?: number;
};

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
  /** Optional B-roll over this segment (video segments only); its audio keeps playing. */
  cutaways?: Cutaway[];
  /** Optional blur patches over the footage (video only): keys are [frame from the segment start, centreX, centreY,
   * width, height] as fractions of the picture, linear between; shown from the first key to the last. */
  blur?: { strength: number; keys: [number, number, number, number, number][] }[];
  /** Photo only: slow move and framing. */
  motion?: Motion;
  fit?: 'cover' | 'blurfill';
  focusX?: number;
  focusY?: number;
};

export type CaptionWord = { text: string; startFrame: number; endFrame: number };
export type CaptionPage = {
  startFrame: number;
  endFrame: number;
  words: CaptionWord[];
  /** Pixels from the bottom edge to the text's bottom (the CLI moves captions clear of an exhibit); default 560. */
  offsetBottom?: number;
};

export type TextBeat = {
  text: string;
  startFrame: number;
  endFrame: number;
  position: 'top' | 'center' | 'lower';
  style: TextStyle;
  /** 'top' only: pixels from the top edge (the CLI pushes text below stat pills and clear of exhibits). */
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
  /** Where a data value comes from ('strava'); absent for typed values and GoPro GPS. Not shown. */
  source?: string;
};

/** GPS track of the recording (lines only where the camera had a fix), 0..1 box coords, north up. */
export type RouteMap = {
  startFrame: number;
  endFrame: number;
  paths: [number, number][][];
  /** Current position for every frame from startFrame. */
  dots: [number, number][];
};

/** Series progress (road to 5 W/kg), built from library/progress.json by the CLI. */
export type ProgressBar = {
  startFrame: number;
  endFrame: number;
  label: string;
  value: number;
  prevValue: number;
  /** 0..1 from the series' first entry to the goal. */
  fill: number;
  prevFill: number;
  goalText: string;
  unit: string;
  note: string;
  /** 0 = no fill animation (first entry). */
  fillFrames: number;
  asOf: string;
};

/** A chart drawn by the template. Numbers are the user's own (typed in the plan) or from their Strava ride. */
export type Chart = {
  type: 'line' | 'bar';
  theme: 'light' | 'dark';
  title: string;
  unit: string;
  xLabel: string;
  /** 'typed' or 'strava'. Not shown. */
  source: string;
  xs: number[];
  ys: number[];
  yMin: number;
  yMax: number;
  xTicks: { v: number; label: string }[];
  yTicks: { v: number; label: string }[];
  /** One labelled point (line) or bar. */
  mark?: { x: number; y: number; label: string };
  /** Line draws itself on / bars grow. */
  draw: boolean;
  /** Series colour; default per theme (dark uses the brand accent). */
  color?: string;
};

/** A study screenshot, diagram or chart shown over the footage for the rider to point at. Images come
 * only from the user's exhibits/ folder; the CLI sizes and places them (typed, or from the fingertip). */
export type Exhibit = {
  startFrame: number;
  endFrame: number;
  kind: 'image' | 'chart';
  /** Image: path relative to the render's public dir (PNG, alpha kept). */
  src?: string;
  chart?: Chart;
  /** Centre and size in frame pixels. */
  x: number;
  y: number;
  width: number;
  height: number;
  /** Optional: the centre follows the hand: [frame from startFrame, x, y], linear between, held after. */
  path?: [number, number, number][];
  /** Degrees. */
  tilt: number;
  /** card = rectangle with a soft shadow (and a backing colour for see-through images); cutout = the image's own shape. */
  style: 'card' | 'cutout';
  background?: string | null;
  padding: number;
  shadow: boolean;
  /** behind = drawn under the rider's cut-out (a Foreground covers the same frames). */
  depth: 'front' | 'behind';
  enter: 'pop' | 'fade' | 'slide' | 'none';
  exit: 'pop' | 'fade' | 'slide' | 'none';
};

/** The rider cut out of a segment's own staged clip (video with alpha), laid over 'behind' exhibits. */
export type Foreground = {
  src: string;
  /** Absolute reel frame. */
  startFrame: number;
  durationInFrames: number;
  /** Frames into the segment where this stretch starts (for the punch keys). */
  offset: number;
  /** Segment index. */
  seg: number;
  /** The segment's punch-in keys, so the cut-out zooms with the footage. */
  punch?: [number, number, number, number][];
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

/** Series title board (generated; only for series the user OK'd), hung over the first shot. */
export type TitleCard = {
  title: string;
  /** "Ep 1." or "" */
  episode: string;
  subtitle: string;
  startFrame: number;
  endFrame: number;
  /** Frame the nail leaves, hits the board, and the board starts swinging (relative to startFrame). */
  nailFrame: number;
  hitFrame: number;
  swingFrame: number;
  fadeFrames: number;
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
  /** Optional; absent in reels without a progress bar. */
  progress?: ProgressBar[];
  /** Optional; absent in reels without a series title board. */
  titleCard?: TitleCard;
  /** Optional; absent in reels without exhibits. */
  exhibits?: Exhibit[];
  /** Optional; the rider in front of 'behind' exhibits. */
  foregrounds?: Foreground[];
  music: Music | null;
  /** Optional; absent in reels without sound effects. */
  sfx?: SfxCue[];
  endCard: EndCard | null;
  /** Studio-only guide showing where Instagram's UI covers the frame. */
  safeArea: boolean;
};
