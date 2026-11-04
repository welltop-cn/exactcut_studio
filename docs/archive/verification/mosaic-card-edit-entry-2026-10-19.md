# Clip-local edit entry and region coordinates

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

Each clip card exposes Edit beside the conditional Effect action. Edit selects that clip, seeks its reference frame, and opens the existing inspector. The previous preview-toolbar adjustment entry is hidden. Both event-backed clips and legacy standalone targets have edit entries; processing locks cover them.

X/Y/width/height now sit directly beneath the region chips, before position mode. More tools retains frame recognition. Existing coordinate handlers and save behavior are unchanged.

Validation: all 61 Chromium UI tests passed with synthetic APIs. Added direct card-edit navigation, coordinate placement/visibility, and unchanged preview-height checks. JavaScript syntax and whitespace checks passed. Layout screenshots inspected at /tmp/card-edit-list.png and /tmp/card-edit-region.png. No backend changes or service restart.
