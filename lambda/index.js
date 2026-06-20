/* global awslambda */
// AWS Lambda Adapter for Vertex AI Unified Proxy
// Supports Function URL Response Streaming (InvokeMode: RESPONSE_STREAM)
// Extremely lightweight with zero dependencies for minimal cold start time.

// Static configuration
const SUPPORTED_IMAGE_MODELS = [
  "google/gemini-2.5-flash-image",
  "google/gemini-3.1-flash-image",
  "agnes/agnes-image-2.1-flash",
  "minimax/image-01",
  "sensenova/sensenova-u1-fast"
];

const DEFAULT_IMAGE_MODEL = "google/gemini-2.5-flash-image";

// Helper utilities
function parseModels(str) {
  if (!str) return [];
  return str
    .split(",")
    .map(s => s.trim())
    .filter(Boolean)
    .map(m => {
      if (
        m.startsWith("google/") ||
        m.startsWith("agnes/") ||
        m.startsWith("minimax/") ||
        m.startsWith("sensenova/") ||
        m === "agnes-2.0-flash"
      ) {
        return m;
      }
      return "google/" + m;
    });
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

  // Add Agnes 2.0 Flash to display list
  const textModelsList = displayModels.map(id => ({ id, object: "model", created: 1720000000, owned_by: "google" }));
  textModelsList.push({ id: "agnes-2.0-flash", object: "model", created: 1720000000, owned_by: "agnes" });

  _cache = {
    textSet: new Set(userTextModels),
    imageSet: new Set(SUPPORTED_IMAGE_MODELS),
    textModelsList: textModelsList,
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
function checkAuth(event, env) {
  const expectedToken = (env.CLIENT_AUTH_TOKEN || "").trim();
  if (!expectedToken) return true;

  // Check URL query param first
  const queryToken = event.queryStringParameters?.token;
  if (queryToken === expectedToken) return true;

  // Check Bearer Token (case-insensitive lookup for authorization header)
  let authHeader = "";
  if (event.headers) {
    for (const key of Object.keys(event.headers)) {
      if (key.toLowerCase() === "authorization") {
        authHeader = event.headers[key] || "";
        break;
      }
    }
  }

  const match = authHeader.match(/^Bearer\s+(.+)$/i);
  if (match && match[1].trim() === expectedToken) return true;

  return false;
}

function corsHeaders(extra = {}) {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
    ...extra
  };
}

async function resolveImageToBase64(imageStr) {
  imageStr = (imageStr || "").trim();
  if (!imageStr) throw new Error("Image source is empty");

  if (imageStr.startsWith("data:image/")) {
    const parts = imageStr.split(",");
    return parts.length > 1 ? parts[1] : parts[0];
  }

  if (imageStr.startsWith("http://") || imageStr.startsWith("https://")) {
    validateUrlSafety(imageStr);
    return await downloadImageAsBase64(imageStr);
  }

  return imageStr;
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

async function downloadImageAsBase64(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`Failed to download image from ${url}, status=${resp.status}`);
  }
  const contentLength = resp.headers.get("Content-Length");
  if (contentLength && parseInt(contentLength, 10) > 10 * 1024 * 1024) {
    throw new Error("Image size exceeds the 10MB limit to protect server memory.");
  }
  const arrayBuffer = await resp.arrayBuffer();
  return Buffer.from(arrayBuffer).toString('base64');
}

// ---------------------------------------------------------------------------
// AWS Lambda Entrypoint using Response Streaming
// ---------------------------------------------------------------------------
export const handler = awslambda.streamifyResponse(async (event, responseStream, context) => {
  const env = process.env;
  const rawPath = event.rawPath || "/";
  const path = rawPath.replace(/\/+$/, "") || "/";
  const method = event.requestContext?.http?.method || "GET";

  // 1. Handle CORS Preflight OPTIONS Request
  if (method === "OPTIONS") {
    responseStream = awslambda.HttpResponseStream.from(responseStream, {
      statusCode: 204,
      headers: corsHeaders()
    });
    responseStream.end();
    return;
  }

  // 2. Client authentication check for all routes
  if (!checkAuth(event, env)) {
    responseStream = awslambda.HttpResponseStream.from(responseStream, {
      statusCode: 401,
      headers: corsHeaders({ "Content-Type": "application/json" })
    });
    responseStream.write(JSON.stringify({
      error: {
        message: "Unauthorized: Invalid or missing CLIENT_AUTH_TOKEN",
        type: "proxy_error",
        code: 401
      }
    }));
    responseStream.end();
    return;
  }

  // 3. Health & Index route
  if (path === "/" || path === "/health") {
    const project = (env.VERTEX_PROJECT || "").trim();
    const location = env.VERTEX_LOCATION || "global";
    const { textSet, imageSet } = getCache(env);
    
    responseStream = awslambda.HttpResponseStream.from(responseStream, {
      statusCode: 200,
      headers: corsHeaders({ "Content-Type": "application/json" })
    });
    responseStream.write(JSON.stringify({
      status: "ok",
      project,
      location,
      text_models: [...textSet],
      image_models: [...imageSet]
    }));
    responseStream.end();
    return;
  }

  // 4. OpenAI models endpoint
  if (path === "/v1/models" && method === "GET") {
    const { textModelsList } = getCache(env);
    responseStream = awslambda.HttpResponseStream.from(responseStream, {
      statusCode: 200,
      headers: corsHeaders({ "Content-Type": "application/json" })
    });
    responseStream.write(JSON.stringify({ object: "list", data: textModelsList }));
    responseStream.end();
    return;
  }

  // 5. OpenAI chat completions endpoint
  if ((path === "/v1/chat/completions" || path === "/chat/completions") && method === "POST") {
    let body = {};
    try {
      let bodyStr = event.body || "{}";
      if (event.isBase64Encoded) {
        bodyStr = Buffer.from(bodyStr, 'base64').toString('utf8');
      }
      body = JSON.parse(bodyStr);
    } catch (e) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 400,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "Invalid JSON body", type: "proxy_error", code: 400 } }));
      responseStream.end();
      return;
    }

    if (!body.model) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 400,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "model is required", type: "proxy_error", code: 400 } }));
      responseStream.end();
      return;
    }

    // --- Detect Agnes 2.0 Flash text model forwarding ---
    const modelName = body.model.trim();
    if (modelName === "agnes/agnes-2.0-flash" || modelName === "agnes-2.0-flash" || modelName === "google/agnes-2.0-flash") {
      const agnesApiKey = (env.AGNES_API_KEY || "").trim();
      if (!agnesApiKey) {
        responseStream = awslambda.HttpResponseStream.from(responseStream, {
          statusCode: 500,
          headers: corsHeaders({ "Content-Type": "application/json" })
        });
        responseStream.write(JSON.stringify({ error: { message: "AGNES_API_KEY not configured on backend", type: "proxy_error", code: 500 } }));
        responseStream.end();
        return;
      }

      body.model = "agnes-2.0-flash";
      const upstreamUrl = "https://apihub.agnes-ai.com/v1/chat/completions";

      try {
        const upstreamRes = await fetch(upstreamUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${agnesApiKey}`
          },
          body: JSON.stringify(body)
        });

        const contentType = upstreamRes.headers.get("Content-Type") || "application/json";
        responseStream = awslambda.HttpResponseStream.from(responseStream, {
          statusCode: upstreamRes.status,
          headers: corsHeaders({
            "Content-Type": contentType,
            "Cache-Control": body.stream ? "no-cache" : undefined,
            "Connection": body.stream ? "keep-alive" : undefined
          })
        });

        if (!upstreamRes.ok) {
          const errText = await upstreamRes.text().catch(() => "Unknown upstream error");
          responseStream.write(errText);
          responseStream.end();
          return;
        }

        if (body.stream) {
          const reader = upstreamRes.body.getReader();
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            responseStream.write(value);
          }
          responseStream.end();
        } else {
          const data = await upstreamRes.json();
          responseStream.write(JSON.stringify(data));
          responseStream.end();
        }
      } catch (e) {
        responseStream = awslambda.HttpResponseStream.from(responseStream, {
          statusCode: 502,
          headers: corsHeaders({ "Content-Type": "application/json" })
        });
        responseStream.write(JSON.stringify({ error: { message: "Agnes upstream error: " + e.message, type: "proxy_error", code: 502 } }));
        responseStream.end();
      }
      return;
    }

    // --- Default: Vertex AI (Google) forwarding ---
    const project = (env.VERTEX_PROJECT || "").trim();
    const location = env.VERTEX_LOCATION || "global";
    const apiKey = (env.VERTEX_API_KEY || "").trim();

    if (!project || !apiKey) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 500,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "VERTEX_PROJECT or VERTEX_API_KEY not configured", type: "proxy_error", code: 500 } }));
      responseStream.end();
      return;
    }

    const { textSet } = getCache(env);

    if (body.model && !body.model.startsWith("google/")) {
      body.model = "google/" + body.model;
    }

    if (textSet.size > 0 && !textSet.has(body.model)) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 400,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: `unsupported model: ${body.model}`, type: "proxy_error", code: 400 } }));
      responseStream.end();
      return;
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
        body: JSON.stringify(body)
      });

      const contentType = upstreamRes.headers.get("Content-Type") || "application/json";
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: upstreamRes.status,
        headers: corsHeaders({
          "Content-Type": contentType,
          "Cache-Control": body.stream ? "no-cache" : undefined,
          "Connection": body.stream ? "keep-alive" : undefined
        })
      });

      if (!upstreamRes.ok) {
        const errText = await upstreamRes.text().catch(() => "Unknown upstream error");
        responseStream.write(errText);
        responseStream.end();
        return;
      }

      if (body.stream) {
        const reader = upstreamRes.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          responseStream.write(value);
        }
        responseStream.end();
      } else {
        const data = await upstreamRes.json();
        responseStream.write(JSON.stringify(data));
        responseStream.end();
      }
    } catch (e) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 502,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "Upstream error: " + e.message, type: "proxy_error", code: 502 } }));
      responseStream.end();
    }
    return;
  }

  // 6. OpenAI images generations endpoint
  if (path === "/v1/images/generations" && method === "POST") {
    const project = (env.VERTEX_PROJECT || "").trim();
    const location = env.VERTEX_LOCATION || "global";
    const apiKey = (env.VERTEX_API_KEY || "").trim();

    if (!project || !apiKey) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 500,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "VERTEX_PROJECT or VERTEX_API_KEY not configured", type: "proxy_error", code: 500 } }));
      responseStream.end();
      return;
    }

    let body = {};
    try {
      let bodyStr = event.body || "{}";
      if (event.isBase64Encoded) {
        bodyStr = Buffer.from(bodyStr, 'base64').toString('utf8');
      }
      body = JSON.parse(bodyStr);
    } catch (e) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 400,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "Invalid JSON body", type: "proxy_error", code: 400 } }));
      responseStream.end();
      return;
    }

    if (!body.model) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 400,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "model is required", type: "proxy_error", code: 400 } }));
      responseStream.end();
      return;
    }

    const { imageSet } = getCache(env);
    const fullModel = body.model.startsWith("google/") ? body.model : "google/" + body.model;

    if (imageSet.size > 0 && !imageSet.has(fullModel)) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 400,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: `unsupported image model: ${body.model}`, type: "proxy_error", code: 400 } }));
      responseStream.end();
      return;
    }

    if (!body.prompt || typeof body.prompt !== "string") {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 400,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "prompt is required", type: "proxy_error", code: 400 } }));
      responseStream.end();
      return;
    }

    const SIZE_TO_ASPECT = {
      "1024x1024": "1:1", "1792x1024": "16:9", "1024x1792": "9:16",
      "1280x896": "4:3", "896x1280": "3:4"
    };

    let finalPrompt = body.prompt;
    if (body.size && SIZE_TO_ASPECT[body.size]) {
      finalPrompt = `${finalPrompt}, ${SIZE_TO_ASPECT[body.size]} aspect ratio`;
    }

    const chatBody = {
      model: fullModel,
      messages: [{ role: "user", content: finalPrompt }],
      modalities: ["image"]
    };

    if (body.n && body.n > 1) chatBody.n = body.n;

    const host = apiHost(location);
    const upstreamUrl = `https://${host}/v1/projects/${project}/locations/${location}/endpoints/openapi/chat/completions`;

    try {
      const upstreamRes = await fetch(upstreamUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
        body: JSON.stringify(chatBody)
      });

      if (!upstreamRes.ok) {
        responseStream = awslambda.HttpResponseStream.from(responseStream, {
          statusCode: upstreamRes.status,
          headers: corsHeaders({ "Content-Type": "application/json" })
        });
        const errText = await upstreamRes.text();
        responseStream.write(errText);
        responseStream.end();
        return;
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

      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 200,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });

      if (images.length === 0) {
        const reason = data.choices?.[0]?.finish_reason || "unknown";
        responseStream.write(JSON.stringify({ error: { message: `Image generation failed. Finish reason: ${reason}`, type: "proxy_error", code: 400 } }));
      } else {
        responseStream.write(JSON.stringify({ created: Math.floor(Date.now() / 1000), data: images }));
      }
      responseStream.end();
    } catch (e) {
      responseStream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 502,
        headers: corsHeaders({ "Content-Type": "application/json" })
      });
      responseStream.write(JSON.stringify({ error: { message: "Upstream error: " + e.message, type: "proxy_error", code: 502 } }));
      responseStream.end();
    }
    return;
  }

  // 7. Route not found fallback
  responseStream = awslambda.HttpResponseStream.from(responseStream, {
    statusCode: 404,
    headers: corsHeaders({ "Content-Type": "application/json" })
  });
  responseStream.write(JSON.stringify({ error: { message: `Route not found: ${method} ${path}`, type: "proxy_error", code: 404 } }));
  responseStream.end();
});
