// Run in functions.exec, NOT node. Input: jobs from manifest.json.
// First inspect each input with view_image. Each invocation processes one job so
// the operator can review the output and preserve its returned path immediately.
// The tools contract is the actual built-in imagegen contract, not a public API.
async function extractTitle(job) {
  const result = await tools.image_gen__imagegen({
    referenced_image_paths: [job.source],
    transparent_background: true,
    prompt: job.prompt,
  });
  generatedImage(result);
}
// Example: await extractTitle(jobs[0]);
