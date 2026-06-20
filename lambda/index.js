/* global awslambda */
// AWS Lambda Adapter for Vertex AI & Agnes AI Unified Text Proxy
// Supports Function URL Response Streaming (InvokeMode: RESPONSE_STREAM)
// Cleaned up and optimized: Only handles Vertex AI and Agnes text completions forwarding.

// Helper utilities
function parseModels(str) {
  if (!str) return [];
  return str
    .split(",")
    .map(s => s.trim())
    .filter(Boolean)
    .map(m => {
      // Keep Agnes models as-is
      if (m.startsWith("agnes-") || m.startsWith("agnes/")) {
        return m;
      }
      // Prefix Google models if needed
      return m.startsWith("google/") ? m : "google/" + m;
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
    : ["google/gemini-3.1-flash-lite", "agnes-2.0-flash"];

  // Dynamically map models to their owners
  const textModelsList = displayModels.map(id => {
    const isAgnes = id.startsWith("agnes-") || id.startsWith("agnes/");
    return { 
      id, 
      object: "model", 
      created: 1720000000, 
      owned_by: isAgnes ? "agnes" : "google" 
    };
  });

  _cache = {
    textSet: new Set(userTextModels),
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
    const { textSet } = getCache(env);
    
    responseStream = awslambda.HttpResponseStream.from(responseStream, {
      statusCode: 200,
      headers: corsHeaders({ "Content-Type": "application/json" })
    });
    responseStream.write(JSON.stringify({
      status: "ok",
      project,
      location,
      text_models: [...textSet]
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

    const modelName = body.model.trim();
    const isAgnesModel = modelName.startsWith("agnes-") || modelName.startsWith("agnes/");

    // --- Route 1: Agnes AI Text Forwarding ---
    if (isAgnesModel) {
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

      // Strip "agnes/" prefix if present to normalize for the upstream Agnes AI API
      body.model = modelName.replace(/^agnes\//, "");
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

    // --- Route 2: Default Vertex AI (Google) Forwarding ---
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

  // 6. Route not found fallback
  responseStream = awslambda.HttpResponseStream.from(responseStream, {
    statusCode: 404,
    headers: corsHeaders({ "Content-Type": "application/json" })
  });
  responseStream.write(JSON.stringify({ error: { message: `Route not found: ${method} ${path}`, type: "proxy_error", code: 404 } }));
  responseStream.end();
});
