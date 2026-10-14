"""
上游供应商适配：按 presets/adapter_rules 驱动的生图与识图执行器。
新增或调整厂商协议只需改这里与 presets/*.json。
"""

import asyncio

import httpx

from config_manager import config_mgr

from core import ASPECT_TO_SIZE, detect_image_mime, resolve_image_to_base64, logger
from prompting import normalize_gemini_model, gemini_supports_reasoning, minimax_prompt_optimizer


class SafetyBlocked(RuntimeError):
    """上游安全策略拦截：换模型通常也会被拦，提示 Agent 改写。"""


_GEMINI_BLOCK_REASONS = {"SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def parse_gemini_images(resp_json: dict, label: str = "") -> list[dict]:
    """解析 generateContent 响应；模型附带的文字说明放进第一张图的 note 字段。"""
    block = (resp_json.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise SafetyBlocked(f"Gemini 拒绝了该提示词（{block}）。请换一种说法：去掉真实人物姓名、品牌商标或敏感描述后重试")
    images, notes = [], []
    finish = ""
    for candidate in resp_json.get("candidates", []):
        finish = candidate.get("finishReason", "") or finish
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("thought"):
                continue
            inline_data = part.get("inlineData") or part.get("inline_data")
            if inline_data and "data" in inline_data:
                images.append({
                    "b64": inline_data["data"],
                    "mime": inline_data.get("mimeType", inline_data.get("mime_type", "image/png")),
                })
            elif part.get("text"):
                notes.append(part["text"].strip())
    if not images:
        if finish in _GEMINI_BLOCK_REASONS:
            raise SafetyBlocked(f"Gemini 安全策略拦截了生成结果（{finish}）。请调整描述后重试，例如去掉真实人物、商标或敏感元素")
        hint = f"：{notes[0][:200]}" if notes else ""
        raise RuntimeError(f"Google ({label}) 未返回图像{hint}")
    if notes:
        images[0]["note"] = " ".join(notes)[:500]
    return images


# ---------------------------------------------------------------------------
# 本地凭据解析辅助函数
# ---------------------------------------------------------------------------


def resolve_local_provider_and_key(provider_id: str, key_label: str = "") -> tuple[dict, str, str]:
    """从本服务端已存储的 providers 中查找供应商配置与对应明文 Key（全自动智能选取有效凭据）"""
    providers = config_mgr.get("providers", default={})
    prov = providers.get(provider_id)
    if not prov:
        for k, v in providers.items():
            if k.lower() == (provider_id or "").lower():
                prov = v
                provider_id = k
                break
    if not prov or not isinstance(prov, dict):
        raise ValueError(f"本地未找到供应商 [{provider_id}] 的配置，请先在 Web 管理端从 EdgeOne 中枢拉取")

    keys_dict = prov.get("keys") or {}

    def is_valid_secret(secret: str) -> bool:
        if not secret or not isinstance(secret, str):
            return False
        s = secret.strip()
        if s.endswith("-12345") or s == "mock" or s == "fake":
            return False
        return len(s) > 5

    # 1. 尝试从指定的 key_label 获取有效真实 key
    if key_label and key_label in keys_dict and is_valid_secret(keys_dict[key_label]):
        return prov, key_label, keys_dict[key_label].strip()

    # 2. 自动从 keys_dict 中筛选第一个真实有效的 Key
    for k_lbl, k_sec in keys_dict.items():
        if is_valid_secret(k_sec):
            return prov, k_lbl, k_sec.strip()

    # 3. 若有任何非空 Key 字段则返回首个，否则提示异常
    if keys_dict:
        first_label, first_secret = next(iter(keys_dict.items()))
        return prov, first_label, first_secret.strip()

    raise ValueError(f"供应商 [{provider_id}] 本地尚未配置任何有效 API Key")


# ---------------------------------------------------------------------------
# 基于本地 adapter_rules 驱动的统一生图与图生图执行器
# ---------------------------------------------------------------------------
async def execute_image_generation(
    client: httpx.AsyncClient,
    provider_id: str,
    model: str,
    key_label: str,
    prompt: str,
    aspect_ratio: str = "1:1",
    action: str = "generate",
    image_b64: str = "",
    mask_b64: str = "",
    resolution: str = "auto",
    seed: int = -1,
    negative_prompt: str = "",
    count: int = 1,
    advanced: dict = None,
    reference_images_b64: list = None,
    grounding: bool = False,
    thinking: str = "",
) -> list[dict]:
    """根据本服务端存储的 provider 配置与 adapter_rules.image_generation 规则调用上游生图接口"""
    prov, used_label, api_key = resolve_local_provider_and_key(provider_id, key_label)
    rules = config_mgr.get_image_adapter_rules(provider_id)
    protocol = rules.get("protocol", "openai_images")
    timeout = float(config_mgr.get("generation", "timeout_seconds", default=45))
    clean_model = model.replace("google/", "").replace("models/", "")
    adv = advanced or {}

    logger.info(f"执行生图: provider={provider_id}, key={used_label}, model={clean_model}, protocol={protocol}, action={action}, aspect={aspect_ratio}, res={resolution}")

    # 驱动 1: Google Gemini / Vertex 原生生图协议 (:generateContent)
    if protocol in ("gemini_image", "gemini_generate_content"):
        clean_model = normalize_gemini_model(clean_model)
        headers = {"Content-Type": "application/json"}
        # 自动识别 Google AI Studio / Gemini Developer Key (AIza... / AQ...) 与 Vertex AI Key
        if api_key.startswith("AIza") or api_key.startswith("AQ."):
            native_base = rules.get("native_base_url", "https://generativelanguage.googleapis.com/v1beta/models").rstrip("/")
            url = f"{native_base}/{clean_model}:generateContent"
            headers["x-goog-api-key"] = api_key
        else:
            project = (prov.get("adapter_rules") or {}).get("vertex_project") or rules.get("vertex_project", "jjb111")
            location = (prov.get("adapter_rules") or {}).get("vertex_location") or rules.get("vertex_location", "global")
            host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
            url = f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{clean_model}:generateContent"
            headers["x-goog-api-key"] = api_key

        image_cfg = {"aspectRatio": aspect_ratio}
        res_upper = (resolution or "").strip().upper()
        if res_upper in ("1K", "2K", "4K"):
            image_cfg["imageSize"] = res_upper
        elif adv.get("image_size"):
            image_cfg["imageSize"] = adv["image_size"]
        if adv.get("person_generation"):
            image_cfg["personGeneration"] = adv["person_generation"]
        base_cfg = {
            "responseModalities": rules.get("response_modalities", ["TEXT", "IMAGE"]),
            "imageConfig": image_cfg,
        }

        parts = []
        if action == "edit" and image_b64:
            parts.append({"inlineData": {"mimeType": detect_image_mime(image_b64), "data": image_b64}})
            for ref in (reference_images_b64 or [])[:13]:
                parts.append({"inlineData": {"mimeType": detect_image_mime(ref), "data": ref}})
            if mask_b64:
                parts.append({"inlineData": {"mimeType": detect_image_mime(mask_b64), "data": mask_b64}})
        parts.append({"text": prompt})

        reasoning_ok = gemini_supports_reasoning(clean_model)
        extras = {}
        if grounding and reasoning_ok:
            search = {"googleSearch": {}}
            if adv.get("image_search"):
                search = {"googleSearch": {"searchTypes": {"webSearch": {}, "imageSearch": {}}}}
            extras["tools"] = [search]
        level = thinking or adv.get("thinking_level") or ""
        if level and reasoning_ok:
            extras["thinkingConfig"] = {"thinkingLevel": level}

        def _payload(with_extras: bool) -> dict:
            cfg = dict(base_cfg)
            body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}
            if with_extras:
                if "thinkingConfig" in extras:
                    cfg["thinkingConfig"] = extras["thinkingConfig"]
                if "tools" in extras:
                    body["tools"] = extras["tools"]
            return body

        async def _one() -> list[dict]:
            resp = await client.post(url, json=_payload(True), headers=headers, timeout=timeout)
            if resp.status_code == 400 and extras:
                # 部分区域/版本不接受 tools 或 thinkingConfig：去掉增强项重试一次，不让整次调用失败
                logger.warning(f"Google 拒绝增强参数，去掉 tools/thinking 重试: {resp.text[:200]}")
                resp = await client.post(url, json=_payload(False), headers=headers, timeout=timeout)
            if resp.status_code != 200:
                raise RuntimeError(f"Google ({used_label}) 出图异常 ({resp.status_code}): {resp.text[:300]}")
            return parse_gemini_images(resp.json(), used_label)

        n = max(1, min(int(count or 1), 4))
        batches = await asyncio.gather(*[_one() for _ in range(n)], return_exceptions=True)
        images = [img for b in batches if isinstance(b, list) for img in b]
        if not images:
            first_err = next((b for b in batches if isinstance(b, Exception)), None)
            raise first_err or RuntimeError(f"Google ({used_label}) 未返回有效图像数据")
        return images

    # 驱动 2: MiniMax 海螺生图协议 (/v1/image_generation)
    if protocol == "minimax_image":
        base_url = (prov.get("base_url") or rules.get("base_url") or "https://api.minimaxi.com/v1").rstrip("/")
        if "api.minimax.com" in base_url:
            base_url = base_url.replace("api.minimax.com", "api.minimaxi.com")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        endpoint = rules.get("endpoint", "/image_generation")
        url = f"{base_url}{endpoint}"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        payload = {
            "model": clean_model or "image-01",
            "prompt": prompt,
            "response_format": "base64",
            "n": max(1, min(int(count or 1), 4)),
        }
        if "aigc_watermark" in adv:
            payload["aigc_watermark"] = bool(adv["aigc_watermark"])
        if isinstance(rules.get("default_params"), dict):
            payload.update({k: v for k, v in rules["default_params"].items() if k != "prompt_optimizer"})
        # Agent 写好的完整描述不让 MiniMax 再改写；可在模型 advanced.prompt_optimizer 显式覆盖
        payload["prompt_optimizer"] = minimax_prompt_optimizer(prompt, adv.get("prompt_optimizer"))

        payload["aspect_ratio"] = aspect_ratio
        if seed is not None and seed >= 0:
            payload["seed"] = seed

        if action == "edit" and image_b64:
            ref_field = rules.get("edit_reference_field", "subject_reference")
            ref_type = rules.get("edit_reference_type", "character")
            payload[ref_field] = [{"type": ref_type, "image_file": f"data:{detect_image_mime(image_b64)};base64,{image_b64}"}]

        resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"MiniMax ({used_label}) 出图异常 ({resp.status_code}): {resp.text[:300]}")
        resp_json = resp.json()
        base_resp = resp_json.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax ({used_label}) 业务错误: {base_resp.get('status_msg')}")
        images = []
        raw_data = resp_json.get("data")
        if isinstance(raw_data, dict):
            for img_data in raw_data.get("image_base64", []):
                images.append({"b64": img_data, "mime": "image/jpeg"})
        elif isinstance(raw_data, list):
            for item in raw_data:
                b64_val = item.get("b64_json") or item.get("image_base64") or item.get("base64")
                if b64_val:
                    images.append({"b64": b64_val, "mime": "image/jpeg"})
        if not images:
            raise RuntimeError(f"MiniMax ({used_label}) 未返回有效图像数据")
        return images

    # 驱动 3: Agnes AI 官方生图协议 (/v1/images/generations)
    if protocol == "agnes_image":
        base_url = (prov.get("base_url") or rules.get("base_url") or "https://apihub.agnes-ai.com/v1").rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        endpoint = rules.get("endpoint", "/images/generations")
        url = f"{base_url}{endpoint}"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        size = adv.get("size") or rules.get("default_size", "2K")
        res_upper = (resolution or "").strip().upper()
        if res_upper in ("1K", "2K", "3K", "4K"):
            size = res_upper
        allowed_ratios = rules.get("allowed_aspect_ratios", ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"])
        ratio = aspect_ratio if aspect_ratio in allowed_ratios else "1:1"

        extra_body = {"response_format": "b64_json"}
        if seed is not None and seed >= 0:
            extra_body["seed"] = seed
        if action == "edit" and image_b64:
            extra_body["image"] = [
                f"data:{detect_image_mime(b)};base64,{b}" for b in [image_b64, *(reference_images_b64 or [])]
            ]

        payload = {
            "model": clean_model or "agnes-image-2.5-flash",
            "prompt": prompt,
            "size": size,
            "ratio": ratio,
            "n": max(1, min(int(count or 1), 4)),
            "extra_body": extra_body
        }
        if action != "edit":
            payload["return_base64"] = True

        resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Agnes ({used_label}) 出图异常 ({resp.status_code}): {resp.text[:300]}")
        resp_json = resp.json()
        images = []
        for item in resp_json.get("data", []):
            b64_val = item.get("b64_json")
            if not b64_val and item.get("url"):
                b64_val = await resolve_image_to_base64(client, item["url"])
            if b64_val:
                images.append({"b64": b64_val, "mime": "image/png"})
        if not images:
            raise RuntimeError(f"Agnes ({used_label}) 未返回有效图像数据")
        return images

    # 驱动 4: 通用 OpenAI 兼容生图协议 (SenseNova / SiliconFlow / OpenAI / Zhipu)
    base_url = (rules.get("base_url") or prov.get("base_url") or "").rstrip("/")
    if "api.sensenova.cn" in base_url:
        # 商汤真实生图接口固定在 token.sensenova.cn/v1
        base_url = base_url.replace("api.sensenova.cn", "token.sensenova.cn")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    endpoint = rules.get("endpoint", "/images/generations")
    url = f"{base_url}{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    size_map = rules.get("size_map") or {}
    use_size = size_map.get(aspect_ratio) or ASPECT_TO_SIZE.get(aspect_ratio, "1024x1024")

    payload = {
        "model": clean_model,
        "prompt": prompt,
        "n": max(1, min(count, 4)),
        "size": use_size,
    }
    neg = negative_prompt or adv.get("negative_prompt")
    if neg:
        payload["negative_prompt"] = neg
    if seed is not None and seed >= 0:
        payload["seed"] = seed
    if rules.get("response_format"):
        payload["response_format"] = rules["response_format"]

    resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"{provider_id} ({used_label}) 出图异常 ({resp.status_code}): {resp.text[:300]}")
    resp_json = resp.json()
    images = []
    b64_fields = rules.get("response_b64_fields", ["b64_json", "b64_image", "base64"])
    for item in resp_json.get("data", []):
        b64_val = None
        for f_name in b64_fields:
            if item.get(f_name):
                b64_val = item[f_name]
                break
        if not b64_val and item.get("url"):
            b64_val = await resolve_image_to_base64(client, item["url"])
        if b64_val:
            images.append({"b64": b64_val, "mime": "image/png"})
    if not images:
        raise RuntimeError(f"{provider_id} ({used_label}) 未返回有效图像数据")
    return images


# ---------------------------------------------------------------------------
# 基于本地 adapter_rules 驱动的统一多模态识图 / OCR 执行器
# ---------------------------------------------------------------------------
async def execute_vision_completion(
    client: httpx.AsyncClient,
    provider_id: str,
    model: str,
    key_label: str,
    prompt: str,
    image_b64: str = "",
    preset: str = "standard"
) -> str:
    """根据本服务端存储的 provider 配置与 adapter_rules.vision 规则调用上游多模态视觉接口"""
    prov, used_label, api_key = resolve_local_provider_and_key(provider_id, key_label)
    rules = config_mgr.get_vision_adapter_rules(provider_id)
    protocol = rules.get("protocol", "openai_vision")
    timeout = float(config_mgr.get("vision", "timeout_seconds", default=45))
    clean_model = model.replace("google/", "").replace("models/", "")
    base_url = (prov.get("base_url") or rules.get("base_url") or "").rstrip("/")

    logger.info(f"执行识图: provider={provider_id}, key={used_label}, model={clean_model}, protocol={protocol}, preset={preset}")

    # 驱动 1: Google Gemini / Vertex 原生多模态视觉协议 (:generateContent)
    if protocol == "gemini_vision":
        headers = {"Content-Type": "application/json"}
        # 自动识别 Google AI Studio / Gemini Developer Key (AIza... / AQ...) 与 Vertex AI Key
        if api_key.startswith("AIza") or api_key.startswith("AQ."):
            native_base = rules.get("native_base_url", "https://generativelanguage.googleapis.com/v1beta/models").rstrip("/")
            url = f"{native_base}/{clean_model}:generateContent"
            headers["x-goog-api-key"] = api_key
        else:
            project = (prov.get("adapter_rules") or {}).get("vertex_project") or rules.get("vertex_project", "jjb111")
            location = (prov.get("adapter_rules") or {}).get("vertex_location") or rules.get("vertex_location", "global")
            host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
            url = f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{clean_model}:generateContent"
            headers["x-goog-api-key"] = api_key

        parts = [{"text": prompt}]
        if image_b64:
            parts.insert(0, {"inlineData": {"mimeType": detect_image_mime(image_b64), "data": image_b64}})
        payload = {"contents": {"role": "USER", "parts": parts}}

        resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            candidates = resp.json().get("candidates", [])
            if candidates:
                text_parts = [p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []) if "text" in p]
                return "\n".join(text_parts).strip()
        raise RuntimeError(f"Google Vertex ({used_label}) 识图异常 ({resp.status_code}): {resp.text[:200]}")

    # 驱动 2: MiniMax / Agnes / OpenAI 兼容多模态 Chat Completions 协议
    endpoint = rules.get("endpoint", "/chat/completions")
    if "api.minimax.com" in base_url:
        base_url = base_url.replace("api.minimax.com", "api.minimaxi.com")
    if "api.sensenova.cn" in base_url:
        base_url = base_url.replace("api.sensenova.cn", "token.sensenova.cn")
    if not base_url.endswith("/v1") and not base_url.endswith("/openai"):
        base_url = f"{base_url}/v1"
    chat_url = f"{base_url}{endpoint}" if not base_url.endswith(endpoint) else base_url
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    case_map = rules.get("case_sensitive_models") or (prov.get("adapter_rules") or {}).get("case_sensitive_models") or {}
    if clean_model.lower() in case_map:
        clean_model = case_map[clean_model.lower()]

    detail = rules.get("detail", "high")
    if image_b64:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{detect_image_mime(image_b64)};base64,{image_b64}", "detail": detail}}
        ]
    else:
        content = prompt

    payload = {
        "model": clean_model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "max_tokens": rules.get("max_tokens", 4096)
    }
    resp = await client.post(chat_url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"{provider_id}/{clean_model} ({used_label}) 识图失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    choices = data.get("choices", [])
    if choices:
        return (choices[0].get("message", {}).get("content") or "").strip()
    raise RuntimeError(f"{provider_id}/{clean_model} 未返回有效分析结果")
