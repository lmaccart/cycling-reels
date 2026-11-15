# Product

<!-- impeccable:product-schema 1 -->

## Platform

macOS

## Users

One cycling creator editing their own short-form ride videos.

## Product Purpose

A personal native editor for shaping the repository's existing reel projects, reviewing source and rendered footage, and applying undoable edits to the same project from either direct manipulation or Claude Code.

## Operating Context

The user works locally with private project data and a shared, read-only original-media library. The editor runs at a desk, supports dense sustained editing, and delegates project semantics, timing, and persistence to the local editing service.

## Capabilities and Constraints

- Native SwiftUI and AppKit macOS app, macOS 14+, Swift 6.
- Edit the existing reel format: source ranges, primary segments, cutaways, captions/text, audio, effects, preview, history, and export.
- Timeline and inspector operations use the service's version-1 NDJSON protocol and expected revisions. The app must not normalize or replace the service-owned plan.
- Interactive editing is local and single-user. No publishing, scheduling, pruning, or production-target export controls.
- At 1280×800, keep transport, undo, and a useful timeline visible. Persist resizable panel dividers.
- Claude edits and manual edits share the same project and transaction system; manual conflicts must not silently disappear.

## Product Principles

1. Keep the editor native, direct, and familiar to a category-fluent video editor.
2. Show the timing and layered structure of the actual reel format instead of pretending it is an arbitrary-track NLE.
3. Make revision, preview readiness, and edit provenance visible.
4. Preserve the last committed project and working preview when work is pending or fails.
