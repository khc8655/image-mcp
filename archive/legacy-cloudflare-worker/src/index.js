/**
 * Vertex AI Unified Worker (OpenAI 兼容代理 + MCP Streamable HTTP 服务端)
 *
 * 1. OpenAI 兼容端点:
 *    - GET  /health 或 /
 *    - GET  /v1/models
 *    - POST /v1/chat/completions
 *    - POST /v1/images/generations
 *
 * 2. MCP (Model Context Protocol) 端点:
 *    - POST/GET /mcp (托管给官方 createMcpHandler)
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { createMcpHandler } from "agents/mcp";
import { z } from "zod";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
  "Access-Control-Max-Age": "86400",
};

// ---------------------------------------------------------------------------
// 静态硬编码的图像和视频模型定义 (无需用户环境变量管理)
// ---------------------------------------------------------------------------
const SUPPORTED_IMAGE_MODELS = [
  "google/gemini-3.1-flash-lite-image",
  "google/gemini-3.1-flash-image",
  "minimax/image-01",
  "sensenova/sensenova-u1-fast"
];

const GOOGLE_IMAGE_MODELS = ["google/gemini-3.1-flash-lite-image", "google/gemini-3.1-flash-image"];
const MINIMAX_IMAGE_MODELS = ["minimax/image-01"];
const SENSENOVA_IMAGE_MODELS = ["sensenova/sensenova-u1-fast"];

const DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-lite-image";

// 顶层统一定义，避免在多个函数中重复
const SIZE_TO_ASPECT = {
  "1024x1024": "1:1",
  "1792x1024": "16:9",
  "1024x1792": "9:16",
  "1280x896": "4:3",
  "896x1280": "3:4",
};

// SenseNova 尺寸映射（从标准尺寸到 SenseNova 支持的高分辨率尺寸），提取为模块级常量避免函数内重复创建
const SENSENOVA_SIZE_MAP = {
  "1024x1024": "2048x2048",
  "1792x1024": "2752x1536",
  "1024x1792": "1536x2752",
  "1280x896": "2272x1824",
  "896x1280": "1824x2272",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

function err(msg, status = 400) {
  return json({ error: { message: msg, type: "proxy_error", code: status } }, status);
}

function parseModels(str) {
  if (!str) return [];
  return str
    .split(",")
    .map(s => s.trim())
    .filter(Boolean)
    .map(m => {
      if (
        m.startsWith("google/") ||
        m.startsWith("minimax/") ||
        m.startsWith("sensenova/")
      ) {
        return m;
      }
      return "google/" + m;
    });
}

function stripPrefix(m) {
  return String(m || "").replace(/^google\//, "").trim();
}

// 用 Map 将缓存与具体的 env 实例绑定，防止多租户/多 env 场景下跨请求污染
const _cacheMap = new Map();
function getCache(env) {
  // 以 VERTEX_PROJECT 作为区分 env 的 key
  const cacheKey = (env.VERTEX_PROJECT || "") + "|" + (env.TEXT_MODELS || "");
  if (_cacheMap.has(cacheKey)) return _cacheMap.get(cacheKey);

  const userTextModels = parseModels(env.TEXT_MODELS);
  
  // 用于 v1/models 列表展示。如果用户没有配置，默认展示 gemini-3.5-flash-lite
  const displayModels = userTextModels.length > 0 
    ? userTextModels 
    : ["google/gemini-3.5-flash-lite"];

  const textModelsList = displayModels.map(id => {
    let owned_by = "google";
    if (id.startsWith("minimax/")) owned_by = "minimax";
    else if (id.startsWith("sensenova/")) owned_by = "sensenova";
    return { id, object: "model", created: 1720000000, owned_by };
  });

  const cache = {
    textSet: new Set(userTextModels), // 依然用于 chat/completions 的严格限制校验 (为空代表不限制)
    imageSet: new Set(SUPPORTED_IMAGE_MODELS),
    textModelsList,
  };
  if (_cacheMap.size > 20) _cacheMap.clear(); // 防止极端场景（如多租户/频繁部署）导致 Map 无限增长
  _cacheMap.set(cacheKey, cache);
  return cache;
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

function buildGoogleThinkingConfig(model, effortStr) {
  const m = stripPrefix(model).toLowerCase();
  if (!m.startsWith("gemini")) {
    return null;
  }

  if (effortStr === "none") {
    return { include_thoughts: false };
  }

  const thinkingConfig = { include_thoughts: true };

  // Gemini 2.5: include_thoughts only, no thinking_level
  if (m.startsWith("gemini-2.5-")) {
    return thinkingConfig;
  }

  let effort = effortStr;
  if (!["minimal", "low", "medium", "high", "xhigh"].includes(effort)) {
    effort = "medium";
  }

  // Gemini 3 / 3.1 / 3.5: support thinking_level with clamping
  if (m.startsWith("gemini-3") || m.startsWith("gemini-3.1") || m.startsWith("gemini-3.5")) {
    if (m.includes("flash")) {
      if (["minimal", "low"].includes(effort)) {
        thinkingConfig.thinking_level = "low";
      } else if (["high", "xhigh"].includes(effort)) {
        thinkingConfig.thinking_level = "high";
      } else {
        thinkingConfig.thinking_level = "medium";
      }
    } else if (m.includes("pro")) {
      thinkingConfig.thinking_level = ["high", "xhigh"].includes(effort) ? "high" : "low";
    }
  }

  return thinkingConfig;
}

// ---------------------------------------------------------------------------
// 统一安全身份认证逻辑 (支持 URL 参数 ?token= 与 Header Bearer Token)
// ---------------------------------------------------------------------------
function getRequestToken(request) {
  const url = new URL(request.url);
  const queryToken = url.searchParams.get("token");
  if (queryToken) return queryToken;

  const authHeader = request.headers.get("Authorization") || "";
  const match = authHeader.match(/^Bearer\s+(.+)$/i);
  if (match) return match[1].trim();

  return null;
}

function checkAuth(request, env) {
  const expectedToken = (env.CLIENT_AUTH_TOKEN || "").trim();
  if (!expectedToken) return true; // 如果后台未配置 CLIENT_AUTH_TOKEN，则默认放行（免密访问）
  const clientToken = getRequestToken(request);
  return clientToken === expectedToken;
}

// ---------------------------------------------------------------------------
// Worker entry point
// ---------------------------------------------------------------------------
export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    // ── 1. 统一进行客户端的身份认证 ──
    if (path !== "/health" && path !== "" && path !== "/") {
      if (!checkAuth(request, env)) {
        return err("Unauthorized: Invalid or missing CLIENT_AUTH_TOKEN", 401);
      }
    }

    // ── 2. OpenAI 兼容代理路由 ──
    if (path === "/v1/chat/completions" || path === "/chat/completions") {
      return handleChatCompletions(request, env);
    }
    if (path === "/v1/images/generations") {
      return handleImageGenerations(request, env);
    }
    if (path === "/v1/models") {
      const { textModelsList } = getCache(env);
      return json({ object: "list", data: textModelsList });
    }
    if (path === "/health" || path === "") {
      const project = (env.VERTEX_PROJECT || "").trim();
      const location = env.VERTEX_LOCATION || "global";
      const { textSet, imageSet } = getCache(env);
      return json({
        status: "ok",
        project,
        location,
        text_models: [...textSet],
        image_models: [...imageSet],
      });
    }

    // ── 3. MCP Endpoint (Streamable HTTP / SSE 托管给官方 SDK) ──
    if (path === "/mcp" || path.startsWith("/mcp/")) {
      const server = new McpServer({
        name: "vertex-unified-image",
        version: "1.0.0",
      });

      registerTools(server, env);

      const mcpHandler = createMcpHandler(server);
      // 避免路径末尾的斜杠 / 导致官方 SDK 报 404 错误
      let mcpRequest = request;
      if (url.pathname.endsWith("/")) {
        const normalizedUrl = new URL(request.url);
        normalizedUrl.pathname = normalizedUrl.pathname.replace(/\/+$/, "");
        mcpRequest = new Request(normalizedUrl.toString(), request);
      }
      return mcpHandler(mcpRequest, env, ctx);
    }

    return err("Not found: " + request.method + " " + path, 404);
  },
};

// ---------------------------------------------------------------------------
// OpenAI-Compatible Proxy Request Handlers
// ---------------------------------------------------------------------------
const THOUGHT_SIGNATURE_CACHE = new Map();
const COMPLETION_TO_TOOL_CALLS = new Map();
const COMPLETION_TO_SIGNATURE = new Map();

function updateSignatureCacheFromChunk(chunkStr) {
  const allIds = [...chunkStr.matchAll(/"id"\s*:\s*"([^"]+)"/g)].map(m => m[1]);
  let completionId = null;
  const toolCallIds = [];
  
  for (const id of allIds) {
    if (id.startsWith("chatcmpl-") || id.startsWith("req-") || id.startsWith("gen-")) {
      completionId = id;
    } else {
      toolCallIds.push(id);
    }
  }

  const sigMatch = chunkStr.match(/"thought_signature"\s*:\s*"([^"]+)"/);
  const sig = sigMatch ? sigMatch[1] : null;

  if (completionId) {
    if (toolCallIds.length > 0) {
      if (!COMPLETION_TO_TOOL_CALLS.has(completionId)) {
        COMPLETION_TO_TOOL_CALLS.set(completionId, new Set());
      }
      const tcSet = COMPLETION_TO_TOOL_CALLS.get(completionId);
      for (const tcId of toolCallIds) {
        tcSet.add(tcId);
        if (COMPLETION_TO_SIGNATURE.has(completionId)) {
          const signature = COMPLETION_TO_SIGNATURE.get(completionId);
          THOUGHT_SIGNATURE_CACHE.set(tcId, signature);
          console.log(`[Stream-Link] tcId=${tcId} -> sig=${signature}`);
        }
      }
    }

    if (sig) {
      COMPLETION_TO_SIGNATURE.set(completionId, sig);
      if (COMPLETION_TO_TOOL_CALLS.has(completionId)) {
        for (const tcId of COMPLETION_TO_TOOL_CALLS.get(completionId)) {
          THOUGHT_SIGNATURE_CACHE.set(tcId, sig);
          console.log(`[Stream-Link] tcId=${tcId} -> sig=${sig}`);
        }
      }
    }
  } else {
    if (toolCallIds.length > 0 && sig) {
      for (const tcId of toolCallIds) {
        THOUGHT_SIGNATURE_CACHE.set(tcId, sig);
        console.log(`[Chunk-Direct] tcId=${tcId} -> sig=${sig}`);
      }
    }
  }
}

function updateSignatureCacheFromJSON(data) {
  if (!data || typeof data !== "object") return;
  const choices = data.choices || [];
  for (const choice of choices) {
    const msg = choice.message || {};
    const tcs = msg.tool_calls || [];
    for (const tc of tcs) {
      const tcId = tc.id;
      const extra = tc.extra_content || {};
      const google = extra.google || {};
      const sig = google.thought_signature;
      if (tcId && sig) {
        if (THOUGHT_SIGNATURE_CACHE.size > 2000) {
          THOUGHT_SIGNATURE_CACHE.clear();
        }
        THOUGHT_SIGNATURE_CACHE.set(tcId, sig);
        console.log(`Cached thought_signature (JSON) for tool_call_id=${tcId}`);
      }
    }
  }
}

function sanitizeMessagesForToolCalls(messages) {
  if (!Array.isArray(messages)) return messages;
  const assistantToolCallIds = new Set();
  for (const msg of messages) {
    if (msg.role === "assistant" && Array.isArray(msg.tool_calls)) {
      for (const tc of msg.tool_calls) {
        if (tc.id) assistantToolCallIds.add(tc.id);
      }
    }
  }
  const toolResponseIds = new Set();
  for (const msg of messages) {
    if (msg.role === "tool" && msg.tool_call_id) {
      toolResponseIds.add(msg.tool_call_id);
    }
  }
  const sanitized = [];
  for (const msg of messages) {
    if (msg.role === "tool") {
      if (!msg.tool_call_id || !assistantToolCallIds.has(msg.tool_call_id)) {
        console.warn(`[Sanitize] Dropped orphaned tool response: ${msg.tool_call_id}`);
        continue;
      }
    }
    sanitized.push(msg);
    if (msg.role === "assistant" && Array.isArray(msg.tool_calls)) {
      const missingResponses = [];
      for (const tc of msg.tool_calls) {
        if (tc.id && !toolResponseIds.has(tc.id)) {
          missingResponses.push(tc.id);
        }
      }
      for (const tcId of missingResponses) {
        sanitized.push({
          role: "tool",
          tool_call_id: tcId,
          name: "placeholder",
          content: "[Result unavailable - pruned by client]"
        });
        console.warn(`[Sanitize] Injected placeholder response for orphaned tool call: ${tcId}`);
      }
    }
  }
  return sanitized;
}

async function handleChatCompletions(req, env) {
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const apiKey = (env.VERTEX_API_KEY || "").trim(); // 使用后台配置的 Vertex AI 密钥

  if (!project) return err("VERTEX_PROJECT not configured", 500);
  if (!apiKey) return err("VERTEX_API_KEY not configured on backend", 500);

  const { textSet } = getCache(env);
  let body;
  try {
    body = await req.json();
  } catch {
    return err("Invalid JSON body", 400);
  }

  // 1. 临时 OCI 极简分流 (20天后直接删除此 if 块与 handleOCICompletion 函数即可)
  if (body && body.model && body.model.startsWith("oci/")) {
    return await handleOCICompletion(req, body);
  }

  // Stateless thoughtSignature restoration & ID-decoding
  if (body.messages && Array.isArray(body.messages)) {
    for (const msg of body.messages) {
      if (msg.role === "assistant" && msg.tool_calls && Array.isArray(msg.tool_calls)) {
        for (const tc of msg.tool_calls) {
          if (tc.id && tc.id.includes(":::")) {
            const parts = tc.id.split(":::");
            const realId = parts[0];
            const sig = parts.slice(1).join(":::");
            tc.id = realId;
            if (!tc.extra_content) tc.extra_content = {};
            if (!tc.extra_content.google) tc.extra_content.google = {};
            tc.extra_content.google.thought_signature = sig;
            console.log(`[Stateless-Decode] Restored thought_signature for tool call ID ${realId}`);
          }
          const tcId = tc.id;
          if (tcId) {
            if (!tc.extra_content) tc.extra_content = {};
            if (!tc.extra_content.google) tc.extra_content.google = {};
            if (!tc.extra_content.google.thought_signature) {
              if (THOUGHT_SIGNATURE_CACHE.has(tcId)) {
                tc.extra_content.google.thought_signature = THOUGHT_SIGNATURE_CACHE.get(tcId);
                console.log(`[Cache-Fallback] Re-inserted thought_signature for tool_call_id=${tcId}`);
              } else {
                tc.extra_content.google.thought_signature = "skip_thought_signature_validator";
                console.log(`[Validator-Bypass] Injected skip_thought_signature_validator for tool_call_id=${tcId}`);
              }
            }
          }
        }
      }
      if (msg.role === "tool" && msg.tool_call_id && msg.tool_call_id.includes(":::")) {
        const parts = msg.tool_call_id.split(":::");
        const realId = parts[0];
        msg.tool_call_id = realId;
        console.log(`[Stateless-Decode] Restored tool_call_id in tool message to ${realId}`);
      }

      // 清洗 message 对象中的非标准扩展字段 (防止 Vertex AI 报 INVALID_ARGUMENT)
      if (msg && typeof msg === "object") {
        const ALLOWED_MSG_KEYS = new Set(["role", "content", "tool_calls", "tool_call_id", "name", "extra_content"]);
        for (const k of Object.keys(msg)) {
          if (!ALLOWED_MSG_KEYS.has(k)) {
            delete msg[k];
          }
        }
      }
      
      // 3. 规范化 tool 消息的 content，确保其为 string（Vertex AI 限制）
      if (msg.role === "tool") {
        let content = msg.content;
        if (Array.isArray(content)) {
          let textParts = [];
          for (const part of content) {
            if (part && typeof part === "object") {
              if (part.type === "text" && typeof part.text === "string") {
                textParts.push(part.text);
              } else if (typeof part.text === "string") {
                textParts.push(part.text);
              }
            } else if (typeof part === "string") {
              textParts.push(part);
            }
          }
          msg.content = textParts.join("\n");
        } else if (content && typeof content === "object") {
          if (content.type === "text" && typeof content.text === "string") {
            msg.content = content.text;
          } else {
            msg.content = JSON.stringify(content);
          }
        }
      }
    }
    
    // Clean up orphaned tool calls & responses
    body.messages = sanitizeMessagesForToolCalls(body.messages);
  }



  if (body.model && !body.model.startsWith("google/")) {
    body.model = "google/" + body.model;
  }

  if (textSet.size > 0 && !textSet.has(body.model)) {
    return err(`unsupported model: ${body.model}`, 400);
  }

  if (body.reasoning_effort !== undefined) {
    const effortStr = String(body.reasoning_effort).toLowerCase().trim();
    if (supportsReasoning(body.model)) {
      const thinkingConfig = buildGoogleThinkingConfig(body.model, effortStr);
      if (thinkingConfig) {
        if (!body.extra_body) body.extra_body = {};
        if (!body.extra_body.google) body.extra_body.google = {};
        body.extra_body.google.thinking_config = thinkingConfig;
      }
    }
    delete body.reasoning_effort;
  }

  if (body.max_completion_tokens !== undefined && body.max_tokens === undefined) {
    body.max_tokens = body.max_completion_tokens;
    delete body.max_completion_tokens;
  }

  const host = apiHost(location);
  const upstreamUrl = `https://${host}/v1/projects/${project}/locations/${location}/endpoints/openapi/chat/completions`;

  try {
    // Gemini 路由：最多重试 2 次，偶发 5xx 时自动延迟 1s 重试（Google 上游较稳定，2 次足够）
    let res;
    for (let i = 0; i < 2; i++) {
      try {
        res = await fetch(upstreamUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
          body: JSON.stringify(body),
        });
        if (res.ok || res.status < 500) break;
        if (i === 0) await new Promise(r => setTimeout(r, 1000));
      } catch (e) {
        if (i === 0) { await new Promise(r => setTimeout(r, 1000)); continue; }
        throw e;
      }
    }

    const contentType = res.headers.get("Content-Type") || "application/json";

    if (!res.ok) {
      return new Response(await res.text(), {
        status: res.status,
        headers: { "Content-Type": contentType, ...CORS },
      });
    }

    if (body.stream) {
      const { writable, readable } = new TransformStream({
        transform(chunk, controller) {
          try {
            let chunkStr = new TextDecoder("utf-8", { fatal: false }).decode(chunk);
            updateSignatureCacheFromChunk(chunkStr);
            
            // Encode signature in tool call ID downstream
            const idMatch = chunkStr.match(/"id"\s*:\s*"([^"]+)"/);
            const sigMatch = chunkStr.match(/"thought_signature"\s*:\s*"([^"]+)"/);
            if (idMatch && sigMatch) {
              const originalId = idMatch[1];
              const sig = sigMatch[1];
              if (originalId && sig && !originalId.includes(":::") && sig !== "skip_thought_signature_validator") {
                const newId = `${originalId}:::${sig}`;
                const escapedId = originalId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                chunkStr = chunkStr.replace(new RegExp(`"id"\\s*:\\s*"${escapedId}"`, 'g'), `"id":"${newId}"`);
                chunk = new TextEncoder().encode(chunkStr);
                console.log(`[Stateless-Encode] Encoded signature in tool call ID downstream: ${newId}`);
              }
            }
          } catch (e) {
            console.error("Error processing downstream stream chunk:", e);
          }
          controller.enqueue(chunk);
        }
      });
      res.body.pipeTo(writable);
      return new Response(readable, {
        status: res.status,
        headers: { "Content-Type": contentType, "Cache-Control": "no-cache", ...CORS },
      });
    } else {
      const text = await res.text();
      let returnText = text;
      try {
        const data = JSON.parse(text);
        updateSignatureCacheFromJSON(data);
        
        let modified = false;
        for (const choice of data.choices || []) {
          const msg = choice.message || {};
          for (const tc of msg.tool_calls || []) {
            const sig = tc.extra_content?.google?.thought_signature;
            if (tc.id && sig) {
              tc.id = `${tc.id}:::${sig}`;
              modified = true;
            }
          }
        }
        if (modified) {
          returnText = JSON.stringify(data);
          console.log("[Stateless-Encode] Encoded signature in JSON response downstream");
        }
      } catch (e) {
        console.error("Error processing downstream JSON response:", e);
      }
      return new Response(returnText, {
        status: res.status,
        headers: { "Content-Type": contentType, ...CORS },
      });
    }
  } catch (e) {
    return err("Upstream error: " + e.message, 502);
  }
}

async function handleImageGenerations(req, env) {
  const project = (env.VERTEX_PROJECT || "").trim();
  const location = env.VERTEX_LOCATION || "global";
  const apiKey = (env.VERTEX_API_KEY || "").trim();

  if (!project) return err("VERTEX_PROJECT not configured", 500);
  if (!apiKey) return err("VERTEX_API_KEY not configured on backend", 500);

  const { imageSet } = getCache(env);
  let body;
  try {
    body = await req.json();
  } catch {
    return err("Invalid JSON body", 400);
  }

  if (!body.model) return err("model is required");
  if (!body.prompt || typeof body.prompt !== "string") return err("prompt is required");

  const fullModel = body.model.startsWith("google/") ? body.model : "google/" + body.model;
  if (imageSet.size > 0 && !imageSet.has(fullModel)) {
    return err(`unsupported image model: ${body.model}`);
  }

  const size = body.size || "1024x1024";
  const aspect_ratio = SIZE_TO_ASPECT[size] || "1:1";
  const modelName = fullModel.replace("google/", "");
  const host = apiHost(location);
  // 与 MCP 工具保持一致：调用 native generateContent API，从 candidates[].inlineData 提取图片
  const upstreamUrl = `https://${host}/v1/projects/${project}/locations/${location}/publishers/google/models/${modelName}:generateContent`;

  const nativePayload = {
    contents: {
      role: "USER",
      parts: [{ text: body.prompt }]
    },
    generationConfig: {
      responseModalities: ["TEXT", "IMAGE"],
      imageConfig: { aspectRatio: aspect_ratio }
    }
  };

  try {
    const res = await fetch(upstreamUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
      body: JSON.stringify(nativePayload),
    });

    if (!res.ok) {
      return new Response(await res.text(), {
        status: res.status,
        headers: { "Content-Type": "application/json", ...CORS },
      });
    }

    const data = await res.json();
    const images = [];

    for (const candidate of (data.candidates || [])) {
      const finishReason = candidate.finishReason || "";
      if (finishReason === "SAFETY" || finishReason === "RECITATION") {
        return err(`Image generation blocked by safety/recitation filter (finishReason=${finishReason})`, 400);
      }
      for (const part of (candidate.content?.parts || [])) {
        if (part.inlineData?.data) {
          const mime = part.inlineData.mimeType || "image/png";
          images.push({
            url: `data:${mime};base64,${part.inlineData.data}`,
            b64_json: part.inlineData.data
          });
        }
      }
    }

    if (images.length === 0) {
      const reason = data.candidates?.[0]?.finishReason || "unknown";
      return err(`Image generation failed. Finish reason: ${reason}`, 400);
    }

    return json({ created: Math.floor(Date.now() / 1000), data: images });
  } catch (e) {
    return err("Upstream error: " + e.message, 502);
  }
}

// ---------------------------------------------------------------------------
// MCP Tool Definitions & Registrar
// ---------------------------------------------------------------------------
function registerTools(server, env) {
  server.tool(
    "generate_image",
    "Generate images from a text description using Google Gemini, MiniMax, or SenseNova. Auto-fallback (when model is empty) routes through Google -> MiniMax -> SenseNova.",
    {
      prompt: z.string().describe("Detailed description of the image to generate using prompt structures (e.g. [Subject] + [Scene] + [Style] + ...)."),
      model: z.string().optional().describe("Model name. Leave EMPTY for auto-fallback. Supported: google/gemini-3.1-flash-lite-image (best quality/English text), google/gemini-3.1-flash-image, minimax/image-01 (best for portraits/characters), sensenova/sensenova-u1-fast (best for Chinese text/art)."),
      size: z.string().optional().describe("Image size. Default is '1024x1024'. Supported: 1024x1024, 1792x1024, 1024x1792, 1280x896, 896x1280.")
    },
    async ({ prompt, model = "", size = "1024x1024" }) => {
      if (model) {
        const targetModel = normalizeModel(model, "google");
        let images;
        if (targetModel.startsWith("google/")) {
          images = await callGoogleGenerateImage(targetModel, prompt, size, env);
        } else if (targetModel.startsWith("sensenova/")) {
          images = await callSensenovaGenerateImage(targetModel, prompt, size, env);
        } else if (targetModel.startsWith("minimax/")) {
          images = await callMinimaxGenerateImage(targetModel, prompt, size, env);
        } else {
          throw new Error(`Unsupported model prefix: ${targetModel}`);
        }
        return formatMcpImages(images);
      }

      const failures = {};

      // 1. Google Fallback
      try {
        const images = await callGoogleGenerateImage(DEFAULT_IMAGE_MODEL, prompt, size, env);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.google = e.message || String(e);
      }

      // 2. MiniMax Fallback
      const minimaxModel = MINIMAX_IMAGE_MODELS[0] || "minimax/image-01";
      try {
        const images = await callMinimaxGenerateImage(minimaxModel, prompt, size, env);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.minimax = e.message || String(e);
      }

      // 3. SenseNova Fallback
      const sensenovaModel = SENSENOVA_IMAGE_MODELS[0] || "sensenova/sensenova-u1-fast";
      try {
        const images = await callSensenovaGenerateImage(sensenovaModel, prompt, size, env);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.sensenova = e.message || String(e);
      }

      throw new Error(
        `All image generation attempts failed:\n` +
        `- Google: ${failures.google}\n` +
        `- MiniMax: ${failures.minimax}\n` +
        `- SenseNova: ${failures.sensenova}`
      );
    }
  );

  server.tool(
    "edit_image",
    "Edit or inpaint an existing image using Google Gemini (prompt-based) or MiniMax (subject-reference character consistency).",
    {
      image: z.string().describe("The original image to edit. Can be a base64-encoded string, local file path, or public HTTP/HTTPS URL."),
      prompt: z.string().describe("Description of the editing task or modifications (specify what to change and what to preserve)."),
      mask: z.string().optional().describe("Optional mask image. Can be a base64-encoded string, local file path, or public HTTP/HTTPS URL (for Google Gemini)."),
      model: z.string().optional().describe("Model name. Leave EMPTY for auto-fallback (Google -> MiniMax). Supported: google/gemini-3.1-flash-lite-image, google/gemini-3.1-flash-image, minimax/image-01.")
    },
    async ({ image, prompt, mask = "", model = "" }) => {
      const image_base64 = await resolveImageToBase64(image);
      const mask_base64 = mask ? await resolveImageToBase64(mask) : "";

      if (model) {
        const targetModel = normalizeModel(model, "google");
        let images;
        if (targetModel.startsWith("google/")) {
          images = await callGoogleEditImage(targetModel, image_base64, prompt, mask_base64, env);
        } else if (targetModel.startsWith("minimax/")) {
          images = await callMinimaxEditImage(targetModel, image_base64, prompt, env);
        } else {
          throw new Error(`Unsupported edit model: ${targetModel}`);
        }
        return formatMcpImages(images);
      }

      const failures = {};

      // 1. Google Fallback
      try {
        const images = await callGoogleEditImage(DEFAULT_IMAGE_MODEL, image_base64, prompt, mask_base64, env);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.google = e.message || String(e);
      }

      // 2. MiniMax Fallback
      const minimaxModel = MINIMAX_IMAGE_MODELS[0] || "minimax/image-01";
      try {
        const images = await callMinimaxEditImage(minimaxModel, image_base64, prompt, env);
        if (images && images.length > 0) return formatMcpImages(images);
      } catch (e) {
        failures.minimax = e.message || String(e);
      }

      throw new Error(
        `All image editing attempts failed:\n` +
        `- Google: ${failures.google}\n` +
        `- MiniMax: ${failures.minimax}`
      );
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
      const enhanced = await callGoogleEnhancePrompt(prompt, style_preset, env);
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

  throw new Error("Local filesystem paths are not supported directly in the Cloudflare Worker cloud environment. Please run the local Python VM version to read local paths, or provide a web URL or Base64 string.");
}

async function downloadImageAsBase64(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`Failed to download image from ${url}, status=${resp.status}`);
  }
  // 限制图片大小最大为 10MB，防止大文件耗尽 Worker 内存导致 OOM
  const contentLength = resp.headers.get("Content-Length");
  if (contentLength && parseInt(contentLength, 10) > 10 * 1024 * 1024) {
    throw new Error("Image size exceeds the 10MB limit to protect server memory.");
  }
  const buffer = await resp.arrayBuffer();
  // 使用循环逐字节转换，避免 spread Uint8Array 在大图时爆 JS 调用栈
  // 同时使用 Worker 原生 btoa() 而非 Node.js Buffer，保证 Cloudflare 兼容性
  const bytes = new Uint8Array(buffer);
  // 分块处理，避免逐字节 += 触发 V8 O(n²) 字符串重分配，对大图（接近 10MB）性能差异显著
  const CHUNK = 8192;
  let binary = "";
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
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
  const apiKey = (env.VERTEX_API_KEY || "").trim(); // 从后台配置获取

  if (!project) throw new Error("VERTEX_PROJECT is not configured");
  if (!apiKey) throw new Error("VERTEX_API_KEY is not configured");

  const host = location === "global" ? "aiplatform.googleapis.com" : `${location}-aiplatform.googleapis.com`;
  const modelName = model.replace("google/", "");
  const url = `https://${host}/v1/projects/${project}/locations/${location}/publishers/google/models/${modelName}:generateContent`;

  // 使用顶层统一定义的 SIZE_TO_ASPECT 常量
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

async function callSensenovaGenerateImage(model, prompt, size, env) {
  const apiKey = (env.SENSENOVA_API_KEY || "").trim();
  if (!apiKey) throw new Error("SENSENOVA_API_KEY is not configured");

  const url = "https://token.sensenova.cn/v1/images/generations";
  const modelName = model.replace("sensenova/", "");

  const useSize = SENSENOVA_SIZE_MAP[size] || "2048x2048"; // 使用模块级 SENSENOVA_SIZE_MAP 常量

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

  // 使用顶层统一定义的 SIZE_TO_ASPECT 常量
  const aspect_ratio = SIZE_TO_ASPECT[size] || "1:1";

  const payload = {
    model: modelName,
    prompt: prompt,
    aspect_ratio: aspect_ratio,
    response_format: "base64",
    prompt_optimizer: true  // 开启 MiniMax 内置提示词自动优化，提升生成质量
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
    prompt_optimizer: true,  // 开启提示词优化
    subject_reference: [
      {
        type: "character",  // character=人物一致性, style=风格迁移
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
  const apiKey = (env.VERTEX_API_KEY || "").trim(); // 从后台配置获取

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

function formatMcpImages(images) {
  return {
    content: images.map(img => ({
      type: "image",
      data: img.b64,
      mimeType: img.mime
    }))
  };
}

// ---------------------------------------------------------------------------
// Model Name Normalization & Helpers
// ---------------------------------------------------------------------------
function normalizeModel(model, defaultPrefix) {
  model = (model || "").trim();
  if (!model) return "";
  for (const prefix of ["google/", "sensenova/", "minimax/"]) {
    if (model.startsWith(prefix)) return model;
  }
  // 去掉短横线前缀后再拼接供应商前缀
  if (model.startsWith("google-")) return "google/" + model.substring(7);
  if (model.startsWith("sensenova-")) return "sensenova/" + model.substring(10);
  if (model.startsWith("minimax-")) return "minimax/" + model.substring(8);
  return `${defaultPrefix}/${model}`;
}

function getNormalizedPool(poolStr, defaultPrefix) {
  return (poolStr || "")
    .split(",")
    .map(m => m.trim())
    .filter(Boolean)
    .map(m => normalizeModel(m, defaultPrefix));
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
    } else if (m.startsWith("minimax/")) {
      desc = " (MiniMax, best for character consistency and high-quality portraits)";
    } else if (m.startsWith("sensenova/")) {
      desc = " (SenseNova, excellent for Chinese text rendering and character styling)";
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
    "| Feature | Google (Gemini) | MiniMax | SenseNova (商汤) |",
    "|---|---|---|---|",
    "| Image Gen | Yes | Yes | Yes |",
    "| Edit / Inpaint | Yes (Prompt-based) | Yes (Subject Reference) | No |",
    "",
    "Note: If a model fails or encounters a 429 quota error, the server will automatically fallback (when no model is specified) or report details to let the Agent handle fallback/recovery."
  );

  return lines.join("\n");
}

// =========================================================================
// ⚠️ TEMPORARY OCI ADAPTER (To be removed after 20 days credit expiration)
// =========================================================================
async function handleOCICompletion(request, body) {
  let modelName = body.model || "";
  if (modelName.startsWith("oci/")) {
    modelName = modelName.slice(4);
  }

  const ociPayload = { ...body, model: modelName };

  const ociApiKey = env.OCI_API_KEY || "sk-oci-placeholder-key";
  const ociCompartment = env.OCI_COMPARTMENT_ID || "ocid1.tenancy.oc1..placeholder";
  const ociEndpoint = "https://inference.generativeai.us-phoenix-1.oci.oraclecloud.com/openai/v1/chat/completions";

  const headers = {
    "Authorization": `Bearer ${ociApiKey}`,
    "compartment-id": ociCompartment,
    "Content-Type": "application/json"
  };

  try {
    const resp = await fetch(ociEndpoint, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(ociPayload)
    });

    if (ociPayload.stream) {
      // 保证返回流式的 text/event-stream 响应
      return new Response(resp.body, {
        status: resp.status,
        headers: {
          "Content-Type": "text/event-stream; charset=utf-8",
          "Cache-Control": "no-cache",
          "Connection": "keep-alive"
        }
      });
    } else {
      const data = await resp.text();
      return new Response(data, {
        status: resp.status,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }
  } catch (err) {
    return new Response(JSON.stringify({ error: { message: `OCI Worker proxy error: ${err.message}`, type: "proxy_error" } }), {
      status: 502,
      headers: { "Content-Type": "application/json" }
    });
  }
}

