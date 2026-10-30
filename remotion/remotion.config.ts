import { Config } from '@remotion/cli/config';

// Rendering defaults for the reels CLI. The CLI passes props, codec and
// quality flags explicitly; these only matter when rendering by hand.
Config.setVideoImageFormat('jpeg');
Config.setJpegQuality(92);
Config.setOverwriteOutput(true);
