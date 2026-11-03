# Cover title extraction experiment

Uses Codex's built-in image editor to remove the cover background and return a
transparent title image. This is AI-assisted reconstruction, not pixel-exact
segmentation. The experiment does not alter rendering, dev services or projects.

`run-in-codex.js` records the tool invocation used in the experiment. It runs only
inside Codex functions.exec with the built-in tool available; it is **not** a
standalone API client. There is no assumed mapping to a public model/API.

For each unique cover, inspect the image, provide its path plus exact title and
preservation constraints in the prompt, call the tool, and record the returned
file path. Preserve red, white and silver typography: do not run the existing
gold-only `cut_title` filter on these outputs. Save sources/results outside Git.

Manifest format (local test data, not an API response model):

```json
[{"id":"example", "title":"Exact title", "source":"/absolute/cover.png",
  "result":"/absolute/generated.png", "prompt":"Background extraction instructions"}]
```

Inspect and package results with:

```sh
~/miniconda3/envs/py3.12/bin/python3.12 \
  source/experiments/title_extraction/report.py /absolute/path/to/manifest.json
```

This copies original transparent PNGs unchanged, deduplicates covers by SHA256,
checks actual alpha data, and creates light/dark inspection previews and an HTML
comparison page. Alpha presence does not prove correct spelling or clean edges;
all outputs retain `visual_approval: pending` until a person reviews them.

Before production integration, independently verify an authorized API contract,
credentials, transparent output behavior, cost and quality on these same cases.
No video production task is started by this experiment.
