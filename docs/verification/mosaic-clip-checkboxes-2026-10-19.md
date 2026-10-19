# Reversible clip choices

The clip list now displays compact checkbox indicators for Delete and Mask instead of a shared segmented border. Checked choices retain their tinted background. Both choices remain mutually exclusive and can be deselected; the effect entry appears only for adopted masking regions. Existing generation locks and persistence handlers are retained.

Controls expose checkbox roles, explicit accessible names, and checked state; native button keyboard activation supports Space. Explicit names keep the visual check glyph out of the accessible label.

Validation uses synthetic API fixtures in real Chromium, including select/cancel, delete/mask exclusivity, effect visibility, keyboard cancellation, persistence and processing locks. Screenshot inspected: /tmp/clip-checkbox.png. No service restart or user draft mutation required.
