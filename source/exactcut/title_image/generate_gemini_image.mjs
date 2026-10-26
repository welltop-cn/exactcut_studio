import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const DEFAULT_MODEL = "gemini-3.1-flash-image";

function loadDotEnv(filePath = ".env") {
  if (!existsSync(filePath)) return;

  const lines = readFileSync(filePath, "utf8").split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const match = trimmed.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!match) continue;

    const [, key, rawValue] = match;
    if (process.env[key]) continue;

    let value = rawValue.trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    process.env[key] = value;
  }
}

function parseArgs(argv) {
  const options = { references: [] };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith("--")) {
      options._ = options._ ?? [];
      options._.push(arg);
      continue;
    }

    const [rawKey, inlineValue] = arg.slice(2).split(/=(.*)/s);
    const key = rawKey.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    const value = inlineValue ?? argv[index + 1];

    if (inlineValue === undefined && (value === undefined || value.startsWith("--"))) {
      options[key] = true;
      continue;
    }
    if (inlineValue === undefined) index += 1;

    if (key === "reference") {
      options.references.push(...String(value).split(",").map((item) => item.trim()).filter(Boolean));
    } else {
      options[key] = value;
    }
  }

  return options;
}

function usage() {
  return `Usage:
  node generate_gemini_image.mjs --prompt-file path/to/prompt.txt --output output.png
  node generate_gemini_image.mjs --prompt "Create a cinematic novel banner..." --aspect-ratio 4:3 --image-size 2K --output banner.png
  node generate_gemini_image.mjs --prompt-file banner.prompt.txt --reference cover.jpg --aspect-ratio 4:3 --output banner.png

Options:
  --prompt          Prompt text.
  --prompt-file     UTF-8 text file containing the prompt.
  --reference       Local reference image path. Repeat or comma-separate for multiple images.
  --output          Output image path. Default: gemini-output.png
  --model           Gemini image model. Default: GEMINI_IMAGE_MODEL or ${DEFAULT_MODEL}
  --aspect-ratio    Image aspect ratio, such as 2:3 or 4:3. Default: 2:3
  --image-size      512, 1K, 2K, or 4K. Default: 2K for Gemini 3 image models.
  --retries         Total attempts for transient API failures. Default: 5
  --retry-delay-ms  Initial retry delay in milliseconds. Default: 1000
  --timeout-ms      Request timeout in milliseconds. Default: 240000
  --metrics-file    Optional JSONL file to append request timing/usage metrics.
  --metrics-context Optional JSON object string merged into the metrics row.
  --help            Show this message.`;
}

function inferMimeType(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === ".jpg" || ext === ".jpeg") return "image/jpeg";
  if (ext === ".png") return "image/png";
  if (ext === ".webp") return "image/webp";
  if (ext === ".heic") return "image/heic";
  throw new Error(`Unsupported reference image type: ${filePath}`);
}

function extensionFromMime(mimeType) {
  if (mimeType === "image/jpeg") return ".jpg";
  if (mimeType === "image/webp") return ".webp";
  return ".png";
}

function outputPathForIndex(output, mimeType, index) {
  const parsed = path.parse(output);
  const mimeExt = extensionFromMime(mimeType);
  const ext = parsed.ext && parsed.ext.toLowerCase() === mimeExt ? parsed.ext : mimeExt;
  const suffix = index === 0 ? "" : `-${index + 1}`;
  return path.join(parsed.dir, `${parsed.name || "gemini-output"}${suffix}${ext}`);
}

function firstImageParts(responseJson) {
  const candidates = responseJson.candidates ?? [];
  const parts = candidates.flatMap((candidate) => candidate.content?.parts ?? []);

  return parts
    .map((part) => part.inlineData ?? part.inline_data)
    .filter((inlineData) => inlineData?.data);
}

function textParts(responseJson) {
  const candidates = responseJson.candidates ?? [];
  return candidates
    .flatMap((candidate) => candidate.content?.parts ?? [])
    .map((part) => part.text)
    .filter(Boolean);
}

loadDotEnv();

const configuredProxy =
  process.env.HTTPS_PROXY ||
  process.env.HTTP_PROXY ||
  process.env.ALL_PROXY ||
  process.env.https_proxy ||
  process.env.http_proxy ||
  process.env.all_proxy;

if (configuredProxy && process.env.NODE_USE_ENV_PROXY !== "1" && process.env.GEMINI_PROXY_REEXEC !== "1") {
  const child = spawnSync(process.execPath, process.argv.slice(1), {
    cwd: process.cwd(),
    env: {
      ...process.env,
      NODE_USE_ENV_PROXY: "1",
      GEMINI_PROXY_REEXEC: "1",
      HTTPS_PROXY: process.env.HTTPS_PROXY || configuredProxy,
      HTTP_PROXY: process.env.HTTP_PROXY || configuredProxy,
      ALL_PROXY: process.env.ALL_PROXY || configuredProxy,
    },
    stdio: "inherit",
  });
  process.exit(child.status ?? 1);
}

const options = parseArgs(process.argv.slice(2));
if (options.help) {
  console.log(usage());
  process.exit(0);
}

const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
if (!apiKey) {
  console.error("ERROR: GEMINI_API_KEY is empty. Put your official Gemini key in .env first.");
  process.exit(1);
}

const basePrompt = options.promptFile
  ? readFileSync(options.promptFile, "utf8")
  : options.prompt;

if (!basePrompt?.trim()) {
  console.error("ERROR: Provide --prompt or --prompt-file.\n");
  console.error(usage());
  process.exit(1);
}

const model = (options.model || process.env.GEMINI_IMAGE_MODEL || DEFAULT_MODEL).replace(/^models\//, "");
const output = options.output || "gemini-output.png";
const aspectRatio = options.aspectRatio || "2:3";
const imageSize = options.imageSize ?? (model.includes("3") ? "2K" : "");
const prompt = [
  basePrompt.trim(),
  "",
  `Output image requirements: aspect ratio ${aspectRatio}.`,
  imageSize ? `Target image size/detail level: ${imageSize}.` : "",
].filter(Boolean).join("\n");

const parts = [{ text: prompt }];
for (const reference of options.references) {
  if (!existsSync(reference)) {
    console.error(`ERROR: Reference image not found: ${reference}`);
    process.exit(1);
  }

  parts.push({
    inline_data: {
      mime_type: inferMimeType(reference),
      data: readFileSync(reference).toString("base64"),
    },
  });
}

const requestBody = {
  contents: [{ role: "user", parts }],
};

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function positiveInt(value, fallback, label) {
  if (value === undefined || value === true || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) {
    console.error(`ERROR: ${label} must be a positive integer.`);
    process.exit(1);
  }
  return parsed;
}

function isTransientStatus(status) {
  return [408, 409, 429, 500, 502, 503, 504].includes(Number(status));
}

function parseResponseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function retryDelayMs(attempt, baseDelayMs) {
  const exponential = baseDelayMs * 2 ** Math.max(0, attempt - 1);
  const jitter = Math.floor(Math.random() * Math.min(1000, baseDelayMs));
  return Math.min(exponential + jitter, 30000);
}

function appendJsonl(filePath, value) {
  if (!filePath || filePath === true) return;
  mkdirSync(path.dirname(path.resolve(filePath)), { recursive: true });
  appendFileSync(filePath, `${JSON.stringify(value)}\n`, "utf8");
}

function parseMetricsContext(raw) {
  if (!raw || raw === true) return {};
  try {
    return JSON.parse(String(raw));
  } catch {
    return { metricsContext: String(raw) };
  }
}

const defaultApiVersion = /(^gemini-3-pro-image|^nano-banana-pro-preview|preview)/.test(model)
  ? "v1beta"
  : "v1";
const baseUrl = process.env.GEMINI_API_BASE_URL || `https://generativelanguage.googleapis.com/${defaultApiVersion}`;
const url = `${baseUrl}/models/${model}:generateContent`;
const maxAttempts = positiveInt(options.retries ?? process.env.GEMINI_RETRIES, 5, "--retries");
const baseRetryDelayMs = positiveInt(
  options.retryDelayMs ?? process.env.GEMINI_RETRY_DELAY_MS,
  1000,
  "--retry-delay-ms",
);
const requestTimeoutMs = positiveInt(
  options.timeoutMs ?? process.env.GEMINI_TIMEOUT_MS,
  240000,
  "--timeout-ms",
);

function curlGenerate() {
  const args = [
    "-sS",
    "--show-error",
    "--connect-timeout",
    "30",
    "--max-time",
    String(Math.ceil(requestTimeoutMs / 1000)),
    "-H",
    "Content-Type: application/json",
    "-H",
    `x-goog-api-key: ${apiKey}`,
    "-X",
    "POST",
    url,
    "--data-binary",
    "@-",
  ];

  if (process.env.GEMINI_CURL_PROXY) {
    args.splice(2, 0, "--proxy", process.env.GEMINI_CURL_PROXY);
  }

  const result = spawnSync("curl", args, {
    input: JSON.stringify(requestBody),
    encoding: "utf8",
    maxBuffer: 1024 * 1024 * 80,
  });

  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || `curl exited ${result.status}`).trim());
  }
  return result.stdout;
}

async function fetchGenerate() {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey,
    },
    body: JSON.stringify(requestBody),
    signal: AbortSignal.timeout(requestTimeoutMs),
  });
  return {
    ok: response.ok,
    status: response.status,
    statusText: response.statusText,
    text: await response.text(),
  };
}

let lastResponse = null;
let successfulAttempt = 0;
const startedAt = Date.now();
for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
  try {
    if (process.env.GEMINI_CURL_PROXY) {
      const text = curlGenerate();
      const json = parseResponseJson(text);
      const status = json?.error?.code ?? 200;
      lastResponse = {
        ok: !json?.error,
        status,
        statusText: json?.error?.status ?? "OK",
        text,
        json,
      };
    } else {
      const response = await fetchGenerate();
      lastResponse = {
        ...response,
        json: parseResponseJson(response.text),
      };
    }

    if (!lastResponse.json) {
      const delay = retryDelayMs(attempt, baseRetryDelayMs);
      const preview = (lastResponse.text || "empty response").slice(0, 200).replace(/\s+/g, " ").trim();
      if (attempt === maxAttempts) {
        console.error(`ERROR: Gemini API returned invalid JSON after ${maxAttempts} attempts: ${preview}`);
        process.exit(1);
      }
      console.error(`Gemini API returned invalid JSON; retrying ${attempt}/${maxAttempts} after ${delay}ms...`);
      await wait(delay);
      continue;
    }

    if (lastResponse.ok && !lastResponse.json.error) {
      successfulAttempt = attempt;
      break;
    }

    const errorStatus = lastResponse.json?.error?.code ?? lastResponse.status;
    const errorText = lastResponse.json?.error?.status ?? lastResponse.statusText;
    if (!isTransientStatus(errorStatus) || attempt === maxAttempts) {
      break;
    }

    const delay = retryDelayMs(attempt, baseRetryDelayMs);
    console.error(`Gemini API transient failure (${errorStatus} ${errorText}); retrying ${attempt}/${maxAttempts} after ${delay}ms...`);
    await wait(delay);
  } catch (error) {
    const cause = error.cause?.code ? ` (${error.cause.code})` : "";
    if (attempt === maxAttempts) {
      console.error(`ERROR: Could not connect to the Gemini API${cause}.`);
      console.error(error.message || "Check that this network can access https://generativelanguage.googleapis.com.");
      process.exit(1);
    }
    const delay = retryDelayMs(attempt, baseRetryDelayMs);
    console.error(`Gemini API connection failed${cause}; retrying ${attempt}/${maxAttempts} after ${delay}ms...`);
    await wait(delay);
  }
}

const responseText = lastResponse?.text ?? "";
const responseJson = lastResponse?.json ?? parseResponseJson(responseText);

if (!lastResponse?.ok || responseJson?.error) {
  const error = responseJson?.error;
  const status = error?.code ?? lastResponse?.status ?? "unknown";
  const statusText = error?.status ?? lastResponse?.statusText ?? "ERROR";
  console.error(`ERROR: Gemini API request failed (${status} ${statusText}).`);
  console.error(responseJson ? JSON.stringify(responseJson, null, 2) : responseText);
  process.exit(1);
}

const images = firstImageParts(responseJson);
if (images.length === 0) {
  console.error("ERROR: Gemini response did not include image data.");
  console.error(JSON.stringify(responseJson, null, 2));
  process.exit(1);
}

for (let index = 0; index < images.length; index += 1) {
  const inlineData = images[index];
  const mimeType = inlineData.mimeType ?? inlineData.mime_type ?? "image/png";
  const imageOutput = outputPathForIndex(output, mimeType, index);
  const outputDir = path.dirname(path.resolve(imageOutput));
  mkdirSync(outputDir, { recursive: true });
  writeFileSync(imageOutput, Buffer.from(inlineData.data, "base64"));
  console.log(`saved image: ${path.resolve(imageOutput)}`);
}

appendJsonl(options.metricsFile, {
  type: "image",
  model,
  aspectRatio,
  imageSize,
  output,
  promptFile: options.promptFile || "",
  hasReference: options.references.length > 0,
  referenceCount: options.references.length,
  imageCount: images.length,
  attempt: successfulAttempt || maxAttempts,
  elapsedMs: Date.now() - startedAt,
  usageMetadata: responseJson.usageMetadata || responseJson.usage_metadata || {},
  createdAt: new Date().toISOString(),
  ...parseMetricsContext(options.metricsContext),
});

const texts = textParts(responseJson);
if (texts.length > 0) {
  console.log("\nGemini text response:");
  console.log(texts.join("\n"));
}
