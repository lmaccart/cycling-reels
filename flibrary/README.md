# flibrary

A small native macOS footage browser for `library/`: thumbnail grid, hover-scrub, search/filters,
and drag-and-drop of the original files into Premiere. It never writes to `library/` or `plans/`;
all data comes from `./reels flibrary ...` (see `CONTRACT.md`).

    flibrary/flibrary setup   # xcodegen + build + install to ~/Applications/flibrary.app
    flibrary/flibrary open    # launch (or just open the app from Finder)

Dragging copies file references only (never moves). Flat clips and photos drag the original in
`library/media/`. A 360 clip drags its equirect export; without one, the app offers to make it.

Layout (`App/`): `FlibraryApp` (root discovery, window), `Backend` (CLI bridge), `Models`,
`LibraryStore`, `Media` (thumb/scrub loading), `DragSurface` (AppKit drag), `GridView`,
`DetailView`, `ToolsModel` (360 tools + jobs: extension point). Tests in `Tests/`.
