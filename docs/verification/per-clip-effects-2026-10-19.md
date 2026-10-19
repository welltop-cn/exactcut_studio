# Per-clip mosaic appearance

Additive schema-1 contract: render targets may contain `effect` with the existing style/strength/expansion object. Missing effect falls back to the job's global effect. Accepted styles: pixel, blur (legacy hard blur), soft_blur, solid. Defaults unchanged. Target validation retains and validates the optional effect. Engine 5 invalidates cached appearance artifacts.

Browser v3 draft adds clipEffects keyed by original start:end. Current clip edits only its entry; all its regions resolve the same effect. Old drafts fall back to their global effect. No selected clip edits the fallback. Rendering sends resolved effect per target; preview sends selected clip effect. Cancelled adoption retains settings. Existing rendering edit lock applies.

Composition groups identical settings, writes one lossless mask stream per group, and composites all groups in a single output encode. Each style samples original video. Later effect groups win on overlaps (group order follows validated target ordering). Audio remains copied. Deletions still apply after masking using source frame coordinates.

Tests cover soft edge core coverage, legacy hard edges, real FFmpeg four styles, mixed clip output and unchanged trailing frames, target validation, per-clip browser switching/reopen/submission, prior UI and stop controls. Existing old draft/results preserved.
