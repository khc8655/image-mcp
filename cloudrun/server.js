import express from "express";
import cors from "cors";
import bodyParser from "body-parser";
import dotenv from "dotenv";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { SSEServerTransport } from "@modelcontextprotocol/sdk/server/sse.js";
import { z } from "zod";

// Load local .env (if present for local dev testing)
dotenv.config();

const app = express();
const port = process.env.PORT || 8080;

app.use(cors());
app.use(bodyParser.json({ limit: "50mb" }));
app.use(bodyParser.urlencoded({ limit: "50mb", extended: true }));

// Static configuration
const SUPPORTED_IMAGE_MODELS = [
  "google/gemini-2.5-flash-image",
  "google/gemini-3.1-flash-image",
  "agnes/agnes-image-2.1-flash",
  "minimax/image-01",
  "sensenova/sensenova-u1-fast"
];

const AGNES_IMAGE_MODELS = ["agnes/agnes-image-2.1-flash"];
const MINIMAX_IMAGE_MODELS = ["minimax/image-01"];
const SENSENOVA_IMAGE_MODELS = ["sensenova/sensenova-u1-fast"];
const AGNES_VIDEO_MODELS = ["agnes/agnes-video-v2.0"];

const DEFAULT_IMAGE_MODEL = "google/gemini-2.5-flash-image";
const DEFAULT_VIDEO_MODEL = "agnes/agnes-video-v2.0";

// Helper utilities
function parseModels(str) {
  if (!str) return [];
  return str
    .split(",")
    .map(s => s.trim())
    .filter(Boolean)
    .map(m => (m.startsWith("google/") ? m : "google/" + m));
}

function stripPrefix(m) {
  return String(m || "").replace(/^google\//, "").trim();
}

let _cache = null;
function getCache(env) {
  if (_cache) return _cache;
  const userTextModels = parseModels(env.TEXT_MODELS);
  const displayModels = userTextModels.length > 0 
    ? userTextModels 
    : ["google/gemini-3.1-flash-lite"];

  _cache = {
    textSet: new Set(userTextModels),
    imageSet: new Set(SUPPORTED_IMAGE_MODELS),
    textModelsList: displayModels.map(id => ({ id, object: "model", created: 1720000000, owned_by: "google" })),
  };
  return _cache;
}

function apiHost(location) {
  return location === "global"
    ? "aiplatform.googleapis.com"
    : `${location}-aiplatform.googleapis.com`;
}

function supportsReasoning(model) {
  const m = stripPrefix(model).toLowerCase();
  return /^gemini-(2\.5|[3-9])/.test(m);
}

// Auth Helper
function checkAuth(req, env) {
  const expectedToken = (env.CLIENT_AUTH_TOKEN || "").trim();
  if (!expectedToken) return true;

  // Check URL query param first
  const queryToken = req.query.token;
  if (queryToken === expectedToken) return true;

  // Check Bearer Token
  const authHeader = req.headers.authorization || "";
  const match = authHeader.match(/^Bearer\s+(.+)$/i);
  if (match && match[1].trim() === expectedToken) return true;

  return false;
}

// ---------------------------------------------------------------------------
// Route Handlers
// ---------------------------------------------------------------------------

// 1. Health check & index
app.all("/", (req, res) => {
  const env = process.env;
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const { textSet, imageSet } = getCache(env);
  res.json({
    status: "ok",
    project,
    location,
    text_models: [...textSet],
    image_models: [...imageSet],
  });
});

app.all("/health", (req, res) => {
  const env = process.env;
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const { textSet, imageSet } = getCache(env);
  res.json({
    status: "ok",
    project,
    location,
    text_models: [...textSet],
    image_models: [...imageSet],
  });
});

// Auth middleware for proxy and MCP routes
app.use((req, res, next) => {
  const path = req.path.replace(/\/+$/, "") || "/";
  if (path === "/" || path === "/health" || req.method === "OPTIONS") {
    return next();
  }
  if (!checkAuth(req, process.env)) {
    return res.status(401).json({
      error: {
        message: "Unauthorized: Invalid or missing CLIENT_AUTH_TOKEN",
        type: "proxy_error",
        code: 401
      }
    });
  }
  next();
});

// 2. OpenAI v1/models
app.get("/v1/models", (req, res) => {
  const { textModelsList } = getCache(process.env);
  res.json({ object: "list", data: textModelsList });
});

// 3. OpenAI v1/chat/completions
app.post(["/v1/chat/completions", "/chat/completions"], async (req, res) => {
  const env = process.env;
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const apiKey = (env.VERTEX_API_KEY || "").trim();

  if (!project) return res.status(500).json({ error: { message: "VERTEX_PROJECT not configured", type: "proxy_error", code: 500 } });
  if (!apiKey) return res.status(500).json({ error: { message: "VERTEX_API_KEY not configured on backend", type: "proxy_error", code: 500 } });

  const { textSet } = getCache(env);
  const body = req.body;

  if (body.model && !body.model.startsWith("google/")) {
    body.model = "google/" + body.model;
  }

  if (textSet.size > 0 && !textSet.has(body.model)) {
    return res.status(400).json({ error: { message: `unsupported model: ${body.model}`, type: "proxy_error", code: 400 } });
  }

  if (body.reasoning_effort !== undefined) {
    if (!supportsReasoning(body.model)) {
      delete body.reasoning_effort;
    } else {
      const effortStr = String(body.reasoning_effort).toLowerCase().trim();
      if (effortStr === "none") {
        delete body.reasoning_effort;
      } else {
        const effortMap = { low: "low", minimal: "low", medium: "medium", high: "high", xhigh: "high" };
        body.reasoning_effort = effortMap[effortStr] || "medium";
      }
    }
  }

  if (body.max_completion_tokens !== undefined && body.max_tokens === undefined) {
    body.max_tokens = body.max_completion_tokens;
    delete body.max_completion_tokens;
  }

  const host = apiHost(location);
  const upstreamUrl = `https://${host}/v1/projects/${project}/locations/${location}/endpoints/openapi/chat/completions`;

  try {
    const upstreamRes = await fetch(upstreamUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
      body: JSON.stringify(body),
    });

    res.status(upstreamRes.status);
    const contentType = upstreamRes.headers.get("Content-Type") || "application/json";
    res.setHeader("Content-Type", contentType);

    if (body.stream) {
      res.setHeader("Cache-Control", "no-cache");
      res.setHeader("Connection", "keep-alive");
      
      const reader = upstreamRes.body.getReader();
      const decoder = new TextDecoder();
      
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        res.write(value);
      }
      res.end();
    } else {
      const data = await upstreamRes.json();
      res.json(data);
    }
  } catch (e) {
    res.status(502).json({ error: { message: "Upstream error: " + e.message, type: "proxy_error", code: 502 } });
  }
});

// 4. OpenAI v1/images/generations
app.post("/v1/images/generations", async (req, res) => {
  const env = process.env;
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const apiKey = (env.VERTEX_API_KEY || "").trim();

  if (!project) return res.status(500).json({ error: { message: "VERTEX_PROJECT not configured", type: "proxy_error", code: 500 } });
  if (!apiKey) return res.status(500).json({ error: { message: "VERTEX_API_KEY not configured on backend", type: "proxy_error", code: 500 } });

  const { imageSet } = getCache(env);
  const body = req.body;

  if (!body.model) return res.status(400).json({ error: { message: "model is required", type: "proxy_error", code: 400 } });

  const fullModel = body.model.startsWith("google/") ? body.model : "google/" + body.model;
  if (imageSet.size > 0 && !imageSet.has(fullModel)) {
    return res.status(400).json({ error: { message: `unsupported image model: ${body.model}`, type: "proxy_error", code: 400 } });
  }

  if (!body.prompt || typeof body.prompt !== "string") {
    return res.status(400).json({ error: { message: "prompt is required", type: "proxy_error", code: 400 } });
  }

  const SIZE_TO_ASPECT = {
    "1024x1024": "1:1", "1792x1024": "16:9", "1024x1792": "9:16",
    "1280x896": "4:3", "896x1280": "3:4",
  };

  let finalPrompt = body.prompt;
  if (body.size && SIZE_TO_ASPECT[body.size]) {
    finalPrompt = `${finalPrompt}, ${SIZE_TO_ASPECT[body.size]} aspect ratio`;
  }

  const chatBody = {
    model: fullModel,
    messages: [{ role: "user", content: finalPrompt }],
    modalities: ["image"],
  };

  if (body.n && body.n > 1) chatBody.n = body.n;

  const host = apiHost(location);
  const upstreamUrl = `https://${host}/v1/projects/${project}/locations/${location}/endpoints/openapi/chat/completions`;

  try {
    const upstreamRes = await fetch(upstreamUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
      body: JSON.stringify(chatBody),
    });

    if (!upstreamRes.ok) {
      const errText = await upstreamRes.text();
      return res.status(upstreamRes.status).send(errText);
    }

    const data = await upstreamRes.json();
    const images = [];

    for (const choice of (data.choices || [])) {
      const audio = choice.message?.audio;
      if (audio?.data) {
        const mime = audio.extra_content?.google?.mime_type || "image/png";
        images.push({ url: `data:${mime};base64,${audio.data}`, b64_json: audio.data });
      }
    }

    if (images.length === 0) {
      const reason = data.choices?.[0]?.finish_reason || "unknown";
      return res.status(400).json({ error: { message: `Image generation failed. Finish reason: ${reason}`, type: "proxy_error", code: 400 } });
    }

    res.json({ created: Math.floor(Date.now() / 1000), data: images });
  } catch (e) {
    res.status(502).json({ error: { message: "Upstream error: " + e.message, type: "proxy_error", code: 502 } });
  }
});

// 5. MCP SSE Endpoint setup
const server = new McpServer({
  name: "vertex-unified-image",
  version: "1.0.0",
});

registerTools(server, process.env);

// Active transports mapping to trace connections
const activeTransports = new Map();

// SSE Mount Endpoint: client connects here to start the EventSource stream
app.get("/mcp", async (req, res) => {
  const transport = new SSEServerTransport("/mcp/messages", res);
  const sessionId = Math.random().toString(36).substring(2, 15);
  activeTransports.set(sessionId, transport);

  // When connection closes, clean up
  req.on("close", () => {
    activeTransports.delete(sessionId);
  });

  // Attach session id to redirect / map subsequent POST messages
  res.setHeader("Mcp-Session-Id", sessionId);
  
  await server.connect(transport);
});

// Message endpoint: client sends JSON-RPC requests here
app.post("/mcp/messages", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"] || req.query.sessionId;
  if (!sessionId || !activeTransports.has(sessionId)) {
    return res.status(400).send("Missing or invalid Mcp-Session-Id");
  }
  const transport = activeTransports.get(sessionId);
  await transport.handlePostMessage(req, res);
});

// ---------------------------------------------------------------------------
// Tool Implementation Functions
// ---------------------------------------------------------------------------
function registerTools(server, env) {
  server.tool(
    "generate_image",
    "Generate images from a text description using Google Gemini, Agnes AI, MiniMax, or SenseNova. Auto-fallback (when model is empty) routes through Google -> Agnes -> MiniMax -> SenseNova.",
    {
      prompt: z.string().describe("Detailed description of the image to generate using prompt structures (e.g. [Subject] + [Scene] + [Style] + ...)."),
      model: z.string().optional().describe("Model name. Leave EMPTY for auto-fallback. Supported: google/gemini-2.5-flash-image (best quality/English text), google/gemini-3.1-flash-image, minimax/image-01 (best for portraits/characters), sensenova/sensenova-u1-fast (best for Chinese text/art), agnes/agnes-image-2.1-flash (fast baseline)."),
      size: z.string().optional().describe("Image size. Default is '1024x1024'. Supported: 1024x1024, 1792x1024, 1024x1792, 1280x896, 896x1280.")
    },
    async ({ prompt, model = "", size = "1024x1024" }) => {
      const actualEnv = process.env;
      if (model) {
        const targetModel = normalizeModel(model, "google");
        let images;
        if (targetModel.startsWith("google/")) {
          images = await callGoogleGenerateImage(targetModel, prompt, size, actualEnv);
        } else if (targetModel.startsWith("agnes/")) {
          images = await callAgnesGenerateImage(targetModel, prompt, size, actualEnv);
        } else if (targetModel.startsWith("sensenova/")) {
          images = await callSensenovaGenerateImage(targetModel, prompt, size, actualEnv);
        } else if (targetModel.startsWith("minimax/")) {
          images = await callMinimaxGenerateImage(targetModel, prompt, size, actualEnv);
        } else {
          throw new Error(`Unsupported model prefix: ${targetModel}`);
        }
        return formatMcpImages(images);
      }

      const failures = {};

      try {
        const images = await callGoogleGenerateImage(DEFAULT_IMAGE_MODEL, prompt, size, actualEnv);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.google = e.message || String(e);
      }

      const agnesModel = AGNES_IMAGE_MODELS[0] || "agnes/agnes-image-2.1-flash";
      try {
        const images = await callAgnesGenerateImage(agnesModel, prompt, size, actualEnv);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.agnes = e.message || String(e);
      }

      const minimaxModel = MINIMAX_IMAGE_MODELS[0] || "minimax/image-01";
      try {
        const images = await callMinimaxGenerateImage(minimaxModel, prompt, size, actualEnv);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.minimax = e.message || String(e);
      }

      const sensenovaModel = SENSENOVA_IMAGE_MODELS[0] || "sensenova/sensenova-u1-fast";
      try {
        const images = await callSensenovaGenerateImage(sensenovaModel, prompt, size, actualEnv);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.sensenova = e.message || String(e);
      }

      throw new Error(
        `All image generation attempts failed:\n` +
        `- Google: ${failures.google}\n` +
        `- Agnes: ${failures.agnes}\n` +
        `- MiniMax: ${failures.minimax}\n` +
        `- SenseNova: ${failures.sensenova}`
      );
    }
  );

  server.tool(
    "edit_image",
    "Edit or inpaint an existing image using Google Gemini (prompt-based), Agnes AI (composition-based image-to-image), or MiniMax (subject-reference character consistency).",
    {
      image: z.string().describe("The original image to edit. Can be a base64-encoded string, local file path, or public HTTP/HTTPS URL."),
      prompt: z.string().describe("Description of the editing task or modifications (specify what to change and what to preserve)."),
      mask: z.string().optional().describe("Optional mask image. Can be a base64-encoded string, local file path, or public HTTP/HTTPS URL (for Google Gemini)."),
      model: z.string().optional().describe("Model name. Leave EMPTY for auto-fallback (Google -> Agnes -> MiniMax). Supported: google/gemini-2.5-flash-image, google/gemini-3.1-flash-image, agnes/agnes-image-2.1-flash, minimax/image-01.")
    },
    async ({ image, prompt, mask = "", model = "" }) => {
      const image_base64 = await resolveImageToBase64(image);
      const mask_base64 = mask ? await resolveImageToBase64(mask) : "";
      const actualEnv = process.env;

      if (model) {
        const targetModel = normalizeModel(model, "google");
        let images;
        if (targetModel.startsWith("google/")) {
          images = await callGoogleEditImage(targetModel, image_base64, prompt, mask_base64, actualEnv);
        } else if (targetModel.startsWith("agnes/")) {
          images = await callAgnesEditImage(targetModel, image_base64, prompt, actualEnv);
        } else if (targetModel.startsWith("minimax/")) {
          images = await callMinimaxEditImage(targetModel, image_base64, prompt, actualEnv);
        } else {
          throw new Error(`Unsupported edit model: ${targetModel}`);
        }
        return formatMcpImages(images);
      }

      const failures = {};

      try {
        const images = await callGoogleEditImage(DEFAULT_IMAGE_MODEL, image_base64, prompt, mask_base64, actualEnv);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.google = e.message || String(e);
      }

      const agnesModel = AGNES_IMAGE_MODELS[0] || "agnes/agnes-image-2.1-flash";
      try {
        const images = await callAgnesEditImage(agnesModel, image_base64, prompt, actualEnv);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.agnes = e.message || String(e);
      }

      const minimaxModel = MINIMAX_IMAGE_MODELS[0] || "minimax/image-01";
      try {
        const images = await callMinimaxEditImage(minimaxModel, image_base64, prompt, actualEnv);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.minimax = e.message || String(e);
      }

      throw new Error(
        `All image editing attempts failed:\n` +
        `- Google: ${failures.google}\n` +
        `- Agnes: ${failures.agnes}\n` +
        `- MiniMax: ${failures.minimax}`
      );
    }
  );

  server.tool(
    "generate_video",
    "Generate a short video from a text description using Agnes AI. This is an asynchronous tool. It will poll for 15s to return the video URL if fast, or return a task_id immediately if it takes longer.",
    {
      prompt: z.string().describe("Detailed description of the video to generate."),
      model: z.string().optional().describe("Model name. Leave EMPTY for default (agnes/agnes-video-v2.0).")
    },
    async ({ prompt, model = "" }) => {
      const actualEnv = process.env;
      let useModel = model ? normalizeModel(model, "agnes") : DEFAULT_VIDEO_MODEL;

      if (!useModel.includes("agnes")) {
        throw new Error(`Unsupported video model: ${useModel}`);
      }

      const agnesApiKey = (actualEnv.AGNES_API_KEY || "").trim();
      if (!agnesApiKey) throw new Error("AGNES_API_KEY is not configured");

      const url = "https://apihub.agnes-ai.com/v1/videos";
      const payload = {
        model: useModel.replace("agnes/", ""),
        prompt: prompt
      };

      const resp = await fetch(url, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${agnesApiKey}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
      });

      if (resp.status !== 200) {
        throw new Error(`Agnes AI video creation failed (${resp.status}): ${await resp.text()}`);
      }

      const data = await resp.json();
      const taskId = data.id;
      if (!taskId) throw new Error(`Agnes AI returned no task ID: ${JSON.stringify(data)}`);

      const pollUrl = `https://apihub.agnes-ai.com/v1/videos/${taskId}`;
      for (let i = 0; i < 3; i++) {
        await new Promise(resolve => setTimeout(resolve, 5000));
        let pollSucceeded = false;
        try {
          const pollResp = await fetch(pollUrl, {
            headers: { "Authorization": `Bearer ${agnesApiKey}` }
          });
          if (pollResp.status === 200) {
            const pollData = await pollResp.json();
            const status = pollData.status;
            if (status === "completed") {
              const videoUrl = pollData.video_url || pollData.url || pollData.remixed_from_video_id;
              if (videoUrl) {
                return {
                  content: [{ type: "text", text: `Video generated successfully. URL: ${videoUrl}` }]
                };
              }
            } else if (status === "failed" || status === "error") {
              throw new Error(`Agnes AI video generation failed: ${JSON.stringify(pollData)}`);
            }
            pollSucceeded = true;
          }
        } catch (e) {
          if (e.message && e.message.startsWith("Agnes AI video generation failed")) throw e;
          if (!pollSucceeded) console.warn(`Fast polling attempt ${i + 1} failed:`, e);
        }
      }

      return {
        content: [
          {
            type: "text",
            text: `SUCCESS_SUBMITTED: Video generation task submitted successfully. The video is still generating (task_id: ${taskId}). Please check the progress using check_video_status tool with this task_id.`
          }
        ]
      };
    }
  );

  server.tool(
    "check_video_status",
    "Check the status of a video generation task using the task ID.",
    {
      task_id: z.string().describe("The ID of the video generation task."),
      model: z.string().optional().describe("Model name. Default is agnes/agnes-video-v2.0.")
    },
    async ({ task_id, model = "agnes/agnes-video-v2.0" }) => {
      const actualEnv = process.env;
      if (!model.includes("agnes")) {
        throw new Error(`Unsupported video model: ${model}`);
      }

      const agnesApiKey = (actualEnv.AGNES_API_KEY || "").trim();
      if (!agnesApiKey) throw new Error("AGNES_API_KEY is not configured");

      const pollUrl = `https://apihub.agnes-ai.com/v1/videos/${task_id}`;
      const resp = await fetch(pollUrl, {
        headers: { "Authorization": `Bearer ${agnesApiKey}` }
      });

      if (resp.status !== 200) {
        throw new Error(`Poll Agnes AI task failed (${resp.status}): ${await resp.text()}`);
      }

      const data = await resp.json();
      const status = data.status;
      if (status === "completed") {
        const videoUrl = data.video_url || data.url || data.remixed_from_video_id;
        if (videoUrl) {
          return {
            content: [{ type: "text", text: `SUCCESS: Video generated successfully. URL: ${videoUrl}` }]
          };
        }
        throw new Error(`Agnes AI task completed but returned no video URL: ${JSON.stringify(data)}`);
      } else if (status === "failed" || status === "error") {
        throw new Error(`Agnes AI video generation failed: ${JSON.stringify(data)}`);
      } else {
        return {
          content: [{ type: "text", text: `PROCESSING: Video is still generating. Current status: ${status}. Please try checking again in 10-20 seconds.` }]
        };
      }
    }
  );

  server.tool(
    "list_models",
    "List all available image and video models and their capabilities.",
    {},
    async () => {
      return {
        content: [
          {
            type: "text",
            text: getModelsMatrixText()
          }
        ]
      };
    }
  );

  server.tool(
    "enhance_prompt",
    "Enhance a simple user description into a professional, highly detailed prompt optimized for text-to-image models.",
    {
      prompt: z.string().describe("The simple user prompt or concept to enhance."),
      style_preset: z.string().optional().describe("Artistic style preset. Options: realistic (standard photography/35mm), anime (illustration/cel-shading), minimalist (negative space/clean), chinese (traditional ink/gongbi), cyberpunk (neon/dark rain), infographic (structured layouts/data flow diagrams). Default is 'realistic'.")
    },
    async ({ prompt, style_preset = "realistic" }) => {
      const actualEnv = process.env;
      const enhanced = await callGoogleEnhancePrompt(prompt, style_preset, actualEnv);
      return {
        content: [{ type: "text", text: enhanced }]
      };
    }
  );
}

function validateUrlSafety(urlStr) {
  const url = new URL(urlStr);
  const hostname = url.hostname.toLowerCase();
  const privateIpPatterns = [
    /^localhost$/,
    /^127\./,
    /^10\./,
    /^172\.(1[6-9]|2[0-9]|3[0-1])\./,
    /^192\.168\./,
    /^169\.254\./
  ];
  for (const pattern of privateIpPatterns) {
    if (pattern.test(hostname)) {
      throw new Error(`SSRF Blocked: URL accesses private or restricted hostname/IP: ${hostname}`);
    }
  }
}

async function resolveImageToBase64(imageStr) {
  imageStr = (imageStr || "").trim();
  if (!imageStr) throw new Error("Image source is empty");

  if (imageStr.startsWith("data:image/")) {
    const parts = imageStr.split(",");
    if (parts.length > 1) return parts[1];
    return parts[0];
  }

  if (
    (!imageStr.startsWith("http://") && !imageStr.startsWith("https://") && !imageStr.includes("/") && !imageStr.includes("\\") && imageStr.length > 100) ||
    (imageStr.length > 1000 && !imageStr.startsWith("http://") && !imageStr.startsWith("https://"))
  ) {
    return imageStr;
  }

  if (imageStr.startsWith("http://") || imageStr.startsWith("https://")) {
    validateUrlSafety(imageStr);
    return await downloadImageAsBase64(imageStr);
  }

  throw new Error("Local filesystem paths are not supported directly in the cloud environment. Please provide a web URL or Base64 string.");
}

async function downloadImageAsBase64(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`Failed to download image from ${url}, status=${resp.status}`);
  }
  const contentLength = resp.headers.get("Content-Length");
  if (contentLength && parseInt(contentLength, 10) > 10 * 1024 * 1024) {
    throw new Error("Image size exceeds the 10MB limit to protect server memory.");
  }
  const buffer = await resp.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

async function callGoogleEnhancePrompt(prompt, style_preset, env) {
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const apiKey = (env.VERTEX_API_KEY || "").trim();

  if (!project) throw new Error("VERTEX_PROJECT is not configured");
  if (!apiKey) throw new Error("VERTEX_API_KEY is not configured");

  const host = location === "global" ? "aiplatform.googleapis.com" : `${location}-aiplatform.googleapis.com`;
  const url = `https://${host}/v1/projects/${project}/locations/${location}/publishers/google/models/gemini-3.1-flash-lite:generateContent`;

  const systemInstructions = 
    "You are an expert prompt engineer for AI image generators (such as Midjourney, Stable Diffusion, and Google Imagen).\n" +
    "Your task is to take a simple user concept and expand it into a detailed, descriptive, high-quality prompt optimized for text-to-image models.\n\n" +
    "Follow these strict prompt design rules:\n" +
    "1. Structure the output into: [Subject description], [Environment/background], [Style/Medium], [Lighting], [Composition/Angle], [Quality keywords].\n" +
    "2. Avoid generic buzzwords like 'photorealistic', 'hyperrealistic', 'ultra-detailed'. Instead, use specific descriptors (e.g. 'shot on 35mm lens, f/1.8', 'cinematic volumetric lighting', 'rich textures').\n" +
    "3. Align the prompt expansion with the requested style aesthetic:\n" +
    "   - 'realistic': focus on camera gear, aperture, natural/studio lighting, real-world textures.\n" +
    "   - 'anime': focus on hand-drawn style, cel-shading, vibrant colors, line art.\n" +
    "   - 'minimalist': focus on simple composition, negative space, clean lines, monochrome or limited palette.\n" +
    "   - 'chinese': focus on traditional ink wash, gongbi style, soft watercolor, scroll painting, oriental motifs.\n" +
    "   - 'cyberpunk': focus on neon lights, rain-soaked streets, futuristic tech, high visual density, dark moody atmosphere.\n" +
    "   - 'infographic': design a clean, structured infographic layout (e.g., flowcharts, sections, grids, or comparison cards) dynamically suited to the topic. Focus on crisp vector graphics, flat/isometric styling, clean non-distracting background, logical flow indicated by spatial layout or thin dashed lines, labeled components (with labels enclosed in double quotes), and a target aspect ratio of 16:9.\n" +
    "4. Output ONLY the final enhanced prompt string. Do NOT include any introductory or concluding text, no conversational filler, no explanation, and do NOT wrap it in markdown code blocks. Output only the raw prompt.";

  const payload = {
    contents: {
      role: "USER",
      parts: [
        { text: `${systemInstructions}\n\nUser Concept: ${prompt}\nStyle Preset: ${style_preset}\nEnhanced Prompt:` }
      ]
    },
    generationConfig: {
      responseModalities: ["TEXT"]
    }
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey
    },
    body: JSON.stringify(payload)
  });

  if (resp.status !== 200) {
    throw new Error(`Google Vertex AI prompt enhancement error (${resp.status}): ${await resp.text()}`);
  }

  const data = await resp.json();
  const enhancedPrompt = data.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!enhancedPrompt) {
    throw new Error(`Google Vertex AI returned an empty or invalid response: ${JSON.stringify(data)}`);
  }
  return enhancedPrompt.trim();
}

async function callGoogleGenerateImage(model, prompt, size, env) {
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const apiKey = (env.VERTEX_API_KEY || "").trim();

  if (!project) throw new Error("VERTEX_PROJECT is not configured");
  if (!apiKey) throw new Error("VERTEX_API_KEY is not configured");

  const host = location === "global" ? "aiplatform.googleapis.com" : `${location}-aiplatform.googleapis.com`;
  const modelName = model.replace("google/", "");
  const url = `https://${host}/v1/projects/${project}/locations/${location}/publishers/google/models/${modelName}:generateContent`;

  const SIZE_TO_ASPECT = {
    "1024x1024": "1:1",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
    "1280x896": "4:3",
    "896x1280": "3:4",
  };
  const aspect_ratio = SIZE_TO_ASPECT[size] || "1:1";

  const payload = {
    contents: {
      role: "USER",
      parts: [{ text: prompt }]
    },
    generationConfig: {
      responseModalities: ["TEXT", "IMAGE"],
      imageConfig: {
        aspectRatio: aspect_ratio
      }
    }
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey
    },
    body: JSON.stringify(payload)
  });

  if (resp.status !== 200) {
    throw new Error(`Google Vertex AI error (${resp.status}): ${await resp.text()}`);
  }

  const data = await resp.json();
  const images = [];
  for (const candidate of (data.candidates || [])) {
    const finishReason = candidate.finishReason || "";
    if (finishReason === "SAFETY" || finishReason === "RECITATION") {
      throw new Error(`Generation blocked by safety/recitation filter (finishReason=${finishReason})`);
    }
    const parts = candidate.content?.parts || [];
    for (const part of parts) {
      if (part.inlineData && part.inlineData.data) {
        images.push({
          b64: part.inlineData.data,
          mime: part.inlineData.mimeType || "image/png"
        });
      }
    }
  }
  return images;
}

async function callAgnesGenerateImage(model, prompt, size, env) {
  const apiKey = (env.AGNES_API_KEY || "").trim();
  if (!apiKey) throw new Error("AGNES_API_KEY is not configured");

  const url = "https://apihub.agnes-ai.com/v1/images/generations";
  const modelName = model.replace("agnes/", "");
  const payload = {
    model: modelName,
    prompt: prompt,
    n: 1,
    size: size
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (resp.status !== 200) {
    throw new Error(`Agnes AI error (${resp.status}): ${await resp.text()}`);
  }

  const data = await resp.json();
  const images = [];
  for (const item of (data.data || [])) {
    if (item.b64_json) {
      images.push({ b64: item.b64_json, mime: "image/png" });
    } else if (item.url) {
      const b64 = await downloadImageAsBase64(item.url);
      images.push({ b64: b64, mime: "image/png" });
    }
  }
  return images;
}

async function callSensenovaGenerateImage(model, prompt, size, env) {
  const apiKey = (env.SENSENOVA_API_KEY || "").trim();
  if (!apiKey) throw new Error("SENSENOVA_API_KEY is not configured");

  const url = "https://token.sensenova.cn/v1/images/generations";
  const modelName = model.replace("sensenova/", "");

  const SENSENOVA_SIZE_MAP = {
    "1024x1024": "2048x2048",
    "1792x1024": "2752x1536",
    "1024x1792": "1536x2752",
    "1280x896": "2272x1824",
    "896x1280": "1824x2272"
  };
  const useSize = SENSENOVA_SIZE_MAP[size] || "2048x2048";

  const payload = {
    model: modelName,
    prompt: prompt,
    n: 1,
    size: useSize
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (resp.status !== 200) {
    throw new Error(`SenseNova error (${resp.status}): ${await resp.text()}`);
  }

  const data = await resp.json();
  const images = [];
  for (const item of (data.data || [])) {
    if (item.b64_json) {
      images.push({ b64: item.b64_json, mime: "image/png" });
    } else if (item.url) {
      const b64 = await downloadImageAsBase64(item.url);
      images.push({ b64: b64, mime: "image/png" });
    }
  }
  return images;
}

async function callMinimaxGenerateImage(model, prompt, size, env) {
  const apiKey = (env.MINIMAX_API_KEY || "").trim();
  if (!apiKey) throw new Error("MINIMAX_API_KEY is not configured");

  const url = "https://api.minimaxi.com/v1/image_generation";
  const modelName = model.replace("minimax/", "");

  const SIZE_TO_ASPECT = {
    "1024x1024": "1:1",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
    "1280x896": "4:3",
    "896x1280": "3:4"
  };
  const aspect_ratio = SIZE_TO_ASPECT[size] || "1:1";

  const payload = {
    model: modelName,
    prompt: prompt,
    aspect_ratio: aspect_ratio,
    response_format: "base64"
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (resp.status !== 200) {
    throw new Error(`MiniMax error (${resp.status}): ${await resp.text()}`);
  }

  const data = await resp.json();
  if (data.base_resp && data.base_resp.status_code !== 0) {
    throw new Error(`MiniMax API logic error: ${data.base_resp.status_msg}`);
  }

  const images = [];
  const b64List = data.data?.image_base64 || [];
  for (const img_b64 of b64List) {
    images.push({ b64: img_b64, mime: "image/jpeg" });
  }
  return images;
}

async function callMinimaxEditImage(model, image_base64, prompt, env) {
  const apiKey = (env.MINIMAX_API_KEY || "").trim();
  if (!apiKey) throw new Error("MINIMAX_API_KEY is not configured");

  const url = "https://api.minimaxi.com/v1/image_generation";
  const modelName = model.replace("minimax/", "");

  const payload = {
    model: modelName,
    prompt: prompt,
    aspect_ratio: "1:1",
    response_format: "base64",
    subject_reference: [
      {
        type: "character",
        image_file: `data:image/png;base64,${image_base64}`
      }
    ]
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (resp.status !== 200) {
    throw new Error(`MiniMax I2I error (${resp.status}): ${await resp.text()}`);
  }

  const data = await resp.json();
  if (data.base_resp && data.base_resp.status_code !== 0) {
    throw new Error(`MiniMax API logic error: ${data.base_resp.status_msg}`);
  }

  const images = [];
  const b64List = data.data?.image_base64 || [];
  for (const img_b64 of b64List) {
    images.push({ b64: img_b64, mime: "image/jpeg" });
  }
  return images;
}

async function callGoogleEditImage(model, image_base64, prompt, mask_base64, env) {
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const apiKey = (env.VERTEX_API_KEY || "").trim();

  if (!project) throw new Error("VERTEX_PROJECT is not configured");
  if (!apiKey) throw new Error("VERTEX_API_KEY is not configured");

  const host = location === "global" ? "aiplatform.googleapis.com" : `${location}-aiplatform.googleapis.com`;
  const modelName = model.replace("google/", "");
  const url = `https://${host}/v1/projects/${project}/locations/${location}/publishers/google/models/${modelName}:generateContent`;

  const parts = [];
  parts.push({
    inlineData: {
      mimeType: "image/png",
      data: image_base64
    }
  });
  if (mask_base64) {
    parts.push({
      inlineData: {
        mimeType: "image/png",
        data: mask_base64
      }
    });
  }
  parts.push({ text: prompt });

  const payload = {
    contents: {
      role: "USER",
      parts: parts
    },
    generationConfig: {
      responseModalities: ["TEXT", "IMAGE"]
    }
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey
    },
    body: JSON.stringify(payload)
  });

  if (resp.status !== 200) {
    throw new Error(`Google Vertex AI editing error (${resp.status}): ${await resp.text()}`);
  }

  const data = await resp.json();
  const images = [];
  for (const candidate of (data.candidates || [])) {
    const finishReason = candidate.finishReason || "";
    if (finishReason === "SAFETY" || finishReason === "RECITATION") {
      throw new Error(`Editing blocked by safety/recitation filter (finishReason=${finishReason})`);
    }
    const candidateParts = candidate.content?.parts || [];
    for (const part of candidateParts) {
      if (part.inlineData && part.inlineData.data) {
        images.push({
          b64: part.inlineData.data,
          mime: part.inlineData.mimeType || "image/png"
        });
      }
    }
  }
  return images;
}

async function callAgnesEditImage(model, image_base64, prompt, env) {
  const apiKey = (env.AGNES_API_KEY || "").trim();
  if (!apiKey) throw new Error("AGNES_API_KEY is not configured");

  const url = "https://apihub.agnes-ai.com/v1/images/generations";
  const modelName = model.replace("agnes/", "");
  const payload = {
    model: modelName,
    prompt: prompt,
    size: "1024x1024",
    image: `data:image/png;base64,${image_base64}`
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (resp.status !== 200) {
    throw new Error(`Agnes AI image-to-image error (${resp.status}): ${await resp.text()}`);
  }

  const data = await resp.json();
  const images = [];
  for (const item of (data.data || [])) {
    if (item.b64_json) {
      images.push({ b64: item.b64_json, mime: "image/png" });
    } else if (item.url) {
      const b64 = await downloadImageAsBase64(item.url);
      images.push({ b64: b64, mime: "image/png" });
    }
  }
  return images;
}

function formatMcpImages(images) {
  return {
    content: images.map(img => ({
      type: "image",
      data: img.b64,
      mimeType: img.mime
    }))
  };
}

function normalizeModel(model, defaultPrefix) {
  model = (model || "").trim();
  if (!model) return "";
  for (const prefix of ["google/", "agnes/", "sensenova/", "minimax/"]) {
    if (model.startsWith(prefix)) return model;
  }
  if (model.startsWith("google-")) return "google/" + model.substring(7);
  if (model.startsWith("agnes-")) return "agnes/" + model;
  if (model.startsWith("sensenova-")) return "sensenova/" + model;
  if (model.startsWith("minimax-")) return "minimax/" + model;
  return `${defaultPrefix}/${model}`;
}

function getModelsMatrixText() {
  const lines = [
    "### Available Image Generation Models:",
    `Default Image Model: ${DEFAULT_IMAGE_MODEL}`,
    "Models:"
  ];

  for (const m of SUPPORTED_IMAGE_MODELS) {
    let desc = "";
    if (m.startsWith("google/")) {
      desc = " (Google Imagen/Gemini, high-quality, supports native editing without masks)";
    } else if (m.startsWith("agnes/")) {
      desc = " (Agnes AI, OpenAI-compatible)";
    } else if (m.startsWith("minimax/")) {
      desc = " (MiniMax, best for character consistency and high-quality portraits)";
    } else if (m.startsWith("sensenova/")) {
      desc = " (SenseNova, excellent for Chinese text rendering and character styling)";
    }
    lines.push(`  - ${m}${desc}`);
  }

  lines.push(
    "",
    "### Available Video Generation Models:",
    `Default Video Model: ${DEFAULT_VIDEO_MODEL}`,
    "Models:"
  );

  for (const m of AGNES_VIDEO_MODELS) {
    let desc = "";
    if (m.startsWith("agnes/")) {
      desc = " (Agnes AI Video generation, asynchronous 15s fast-polled + status-polled check)";
    }
    lines.push(`  - ${m}${desc}`);
  }

  lines.push(
    "",
    "### Supported Image Sizes and Aspect Ratios:",
    "  - 1024x1024 (1:1)  — Square (best default)",
    "  - 1792x1024 (16:9) — Landscape (banners, wide scenes)",
    "  - 1024x1792 (9:16) — Portrait (phone wallpaper, poster)",
    "  - 1280x896  (4:3)  — Standard photo",
    "  - 896x1280  (3:4)  — Classic portrait",
    "",
    "### Vendor-Specific Capability Matrix:",
    "| Feature | Google (Gemini) | Agnes AI | MiniMax | SenseNova (商汤) |",
    "|---|---|---|---|---|",
    "| Image Gen | Yes | Yes | Yes | Yes |",
    "| Edit / Inpaint | Yes (Prompt-based) | Yes (Img2Img JSON URI) | Yes (Subject Reference) | No |",
    "| Video Gen | No | Yes | No | No |",
    "",
    "Note: If a model fails or encounters a 429 quota error, the server will automatically fallback (when no model is specified) or report details to let the Agent handle fallback/recovery."
  );

  return lines.join("\n");
}

app.listen(port, () => {
  console.log(`Server is running on port ${port}`);
});
