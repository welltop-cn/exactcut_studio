# Compact clip inspector

Local development UI only. Time inputs share one row; numbered region chips and add/remove controls precede position mode. Precision coordinates, current-frame time shortcuts, and frame recognition live under collapsed More tools. The existing sidebar layout retains preview height.

Save modifications commits the time range and edited/new candidate boxes together. Region switching retains draft geometry across different reference frames. A time-only change to an untouched AI candidate does not adopt it. Region removal retains the existing immediate persistence behavior.

Numeric coordinate changes update the overlay without rebuilding the region buttons: rebuilding on input blur could consume the subsequent chip click.

Validation: all 59 tests in source/tests/test_mosaic_ui.py passed using the prescribed Python 3.12 interpreter and real Chromium with synthetic API fixtures. Added compact-tools visibility and two-box-plus-time atomic-save coverage. JavaScript syntax and git diff whitespace checks passed. Screenshot inspected at /tmp/compact-inspector.png. No backend or service restart required; user draft data was not modified by tests.
