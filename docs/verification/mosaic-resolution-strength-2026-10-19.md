# Resolution-relative mosaic strength

The clip preview scales to a 640px long edge before composition, while full output preserves source dimensions. Fixed pixel blocks and blur sigma therefore made full output weaker at higher resolutions.

Scale both parameters by max(width, height) / 640. Keep the existing strength choices and 640px appearance. Bump render and legacy tracking-output identities so newly requested jobs do not reuse old rendered files. Source data and existing output files are unchanged.

Validation: the new resolution regression first failed for all nine non-640 cases, then passed after the fix. test_mosaic_effects.py, test_mosaic_render.py and test_mosaic_tracking.py all passed, including real FFmpeg rendering. git diff --check passed. No service deployment or real-user-video visual acceptance performed in this change.
