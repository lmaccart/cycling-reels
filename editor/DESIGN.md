# Design system

## Surface

Native macOS editing workspace. Operate mode: precision, scanability, consistency, and density serve long editing sessions.

## Visual direction

- A Premiere-like dark workspace with familiar native panel and transport conventions.
- Type tokens live in `EditorTypography.swift` (`EditorType.*`, `PanelTitle`): compact SF Pro for UI, tracked uppercase panel titles ("Effect Controls", "Program", "Source"), SF Mono for timecode and numeric fields (big blue readout in the monitors). No ad-hoc `.font(.system(size:))` in panels.
- Restrained charcoal surfaces: the canvas is darkest, panel chrome slightly lighter, and dividers quiet but visible.
- Cool blue is reserved for selection, primary actions, and the playhead. Distinct, restrained lane hues distinguish picture, overlays, and audio; status colors retain their standard warning/error meanings.
- Inspector ("Effect Controls") sections carry a coloured accent bar and SF Symbol, chosen by `InspectorSection.classify`: Video/Motion blue, Speed & Timing orange, Colour pink, Text & Captions purple, Audio green, Overlays & B-roll teal, Post and Advanced grey. Sliders, switches and numeric fields pick up the section tint. Labels sit left, values right-aligned; numeric labels scrub on drag.
- Compact controls and tight information spacing; no dashboard cards, decorative gradients, ornamental meters, or oversized empty chrome.
- SF Symbols are used for standard editor actions, paired with accessible labels or tooltips.

## Layout and interaction

- Left project/library browser; center Source and Program monitors; right Effect Controls inspector (no chat panel).
- A useful, persistent AppKit timeline occupies the lower workspace.
- Split dividers resize and persist independently. At narrow widths, monitors may tab rather than collapse editing controls.
- Native keyboard focus, selection, text input, dialogs, and reduced-motion behavior.
- AI changes use a textual source label and visible highlight in addition to color.

## Finish criteria

At 1280×800, the user can see library context, both monitors, a usable inspector, undo/transport, and multiple timeline lanes without a web shell or placeholder dashboard.
