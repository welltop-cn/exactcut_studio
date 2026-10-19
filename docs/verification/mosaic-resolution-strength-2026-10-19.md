# Resolution-relative mosaic strength

The clip preview scales to a 640px long edge before composition, while full output preserves source dimensions. Fixed pixel blocks and blur sigma therefore made full output weaker at higher resolutions.

Scale both parameters by max(width, height) / 640. Keep the existing strength choices and 640px appearance. Bump render and legacy tracking-output identities so newly requested jobs do not reuse old rendered files. Source data and existing output files are unchanged.

Validation: the new resolution regression first failed for all nine non-640 cases, then passed after the fix. test_mosaic_effects.py, test_mosaic_render.py and test_mosaic_tracking.py all passed, including real FFmpeg rendering. git diff --check passed. No service deployment or real-user-video visual acceptance performed in this change.

## Test instance deployment

Deployed code 1f3a461 to http://8.221.122.75/test/ with release test-mosaic-strength-1f3a461. Full local pytest completed successfully (10 existing fork warnings). Linux isolated effect/render/tracking checks: 49 passed in 20.29s (one read-only cache warning). Existing SAM CPU image, model mount, runtime configuration and business data retained.

Public health reports ok and worker_alive=true. Runtime verification gives the same 12x21 mosaic grid for medium strength at both 360x640 and 1080x1920. Production health remained ok and container start time unchanged. User-video visual comparison was not performed during deployment; existing output must be regenerated.

Package SHA256: 7b33dd5ed0fb41f65ff70a2d883941400026acd6e9b3d69d15b416a15057cf7a. Server release record: /srv/exactcut-test/deployments/strength-1f3a461/release.json. Rollback code: /srv/exactcut-test/code-strength-1f3a461.previous. Health-failure rollback preserves data.
