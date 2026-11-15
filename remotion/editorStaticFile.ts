import { staticFile as remotionStaticFile } from 'remotion';

declare global {
  interface Window {
    __REELS_PREVIEW_STATIC_BASE__?: string;
  }
}

/**
 * Use Remotion's normal public-directory resolver for renders and Studio. The
 * editor preview sets a revision-scoped HTTP directory; props still contain
 * only the relative names produced by render.build().
 */
export const editorStaticFile = (path: string): string => {
  const base = typeof window === 'undefined' ? undefined : window.__REELS_PREVIEW_STATIC_BASE__;
  if (!base) {
    return remotionStaticFile(path);
  }

  if (
    !path ||
    path.startsWith('/') ||
    path.includes('\\') ||
    path.split('/').some((part) => !part || part === '.' || part === '..')
  ) {
    throw new Error('Preview media paths must be relative names inside the staged revision.');
  }

  const root = new URL(base, window.location.href);
  if (root.origin !== window.location.origin || !root.pathname.endsWith('/')) {
    throw new Error('Preview media must be served from this editor preview origin.');
  }
  return new URL(path.split('/').map(encodeURIComponent).join('/'), root).toString();
};
