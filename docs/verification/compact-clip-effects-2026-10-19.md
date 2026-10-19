# Compact clip effect entry

Removed top-of-list context-dependent effect controls. Each adopted clip exposes one compact 效果 button, adjacent to its delete/mosaic actions. Opening captures that clip key; settings are staged until 保存. 取消 and Escape discard changes. Dialog title identifies its time range; only style, strength, expansion and two actions are displayed. No card effect summary or default-effect editor. Old persisted fallback settings remain usable.

38 browser tests passed including clip-local persistence/render payload, cancel/Escape rollback, popup height under 350 px, and generation edit locks. Synthetic browser screenshots visually inspected: 360 × 241 popup at 1440 × 900; compact card has no top settings panel. UI-only change: existing 8801 reads HTML/JS from disk and versions script URLs by content hash, so refresh loads this revision without interrupting jobs. Anonymous HTTP check returns 401 as expected; no authenticated user state changed.
