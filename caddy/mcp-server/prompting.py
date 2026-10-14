"""
面向调用方 Agent 的使用说明，以及降级到非 Gemini 模型时的请求适配。

设计原则：
- 提示词由调用方 Agent 按 Gemini Nano Banana 的写法撰写，服务端原样发送给 Gemini；
- 只有降级到 MiniMax / Agnes 等模型时，才按目标模型的硬限制做最小改写（长度、比例、档位），
  并把每一处改动写进结果的 adjustments，Agent 能看到发生了什么。
"""
import math
import re

# ---------------------------------------------------------------------------
# 对 Agent 暴露的说明文字（server instructions / 工具描述 / MCP prompts）
# ---------------------------------------------------------------------------
SERVER_INSTRUCTIONS = """\
Get the image a task needs.
- An existing real-world image (logo, product photo, a site's screenshot, reference picture) -> search_images.
- A new image -> generate_image. Change an existing image with AI -> edit_image.
- Crop / resize / rotate / convert / remove background only -> transform_image (instant, no model cost).
- Read, describe or OCR an image -> describe_image.
Image generation runs on Gemini Nano Banana. Write the final prompt yourself following the rules in
generate_image; the server sends it unchanged. Leave optional parameters out unless the task needs them:
the server applies the owner's defaults. Work-scene examples are available as MCP prompts.
"""

GENERATE_DESCRIPTION = """\
Generate a new image with Gemini Nano Banana. You write the final prompt; it is sent unchanged.

How to write the prompt:
1. Describe the scene in full sentences, not a keyword list: subject, action, setting, composition, style.
2. State the purpose, e.g. "cover slide for a quarterly IT review", "hero image for a product page".
3. Say what you want, not what to avoid ("an empty street", not "no cars"). There is no negative prompt.
4. Put exact on-image text in quotes and describe the font, e.g. title "数字化转型成果" in bold white sans-serif.
   Chinese is supported. Keep on-image text short; settle the exact copy before calling.
5. Control the shot with camera and lighting terms: "45-degree overhead", "soft three-point lighting",
   "shallow depth of field", "isometric view", "flat vector".
6. For complex scenes, describe in order: background, then foreground, then the main subject.
7. Set the shape with aspect_ratio, not in the prompt. Skip "8k, masterpiece, trending, unreal engine" tags.

Example: "A clean isometric diagram of a hybrid-cloud network for an executive slide: an on-prem data center
on the left, two cloud regions on the right, joined by an encrypted link labeled "SD-WAN". Flat vector
style, navy and cyan on a white background, generous margins for a title."

Optional parameters (omit to use the server's defaults):
- aspect_ratio: 16:9 slides/banners, 9:16 phone, 1:1 icons/avatars, 3:4 or 4:5 posters, 21:9 or 4:1 wide banners.
- resolution: 1K drafts, 2K normal work, 4K print or large screens.
- quality: "best" for dense text, diagrams with many labels, or final deliverables (slower); "fast" for drafts.
- grounding: true when the image must match real things (a specific building, product, species, today's data).
  The model searches the web first. It cannot look up real people.
- count: 1-4 variations.
Returns signed links plus a thumbnail. If the model adds a note, it is in structuredContent.model_note.
"""

EDIT_DESCRIPTION = """\
Edit an existing image with Gemini Nano Banana: change style, add or remove elements, replace a part,
or combine several images.

How to write the instruction:
- Say what changes and what stays exactly the same: "Change only the blue sofa to a brown leather
  chesterfield. Keep the lighting, camera angle and everything else unchanged."
- Point at the region in words; a mask is rarely needed.
- With reference_images, name each image's role: "Use the first image as the room, the second as the lamp
  to add on the side table, matching the room's lighting."
- To keep a face or logo intact, describe it in detail in the instruction.

image: the image to edit (public URL, Base64 or data URI). reference_images: up to 13 extra images.
For crop / resize / rotate / format conversion / background removal use transform_image instead.
Other optional parameters work as in generate_image.
"""

# 工作场景范例，以 MCP prompts 形式暴露（客户端可作斜杠命令，Agent 需要时拉取）
WORK_SCENE_PROMPTS = {
    "architecture_diagram": {
        "title": "Software architecture diagram",
        "description": "Layered system / microservice architecture figure for docs or slides.",
        "aspect_ratio": "16:9",
        "example": (
            "A clean software architecture diagram for a technical design review slide. Four horizontal layers "
            "from top to bottom: clients, API gateway, a row of five microservice boxes, and a data tier with a "
            "database and a message queue. Thin directional arrows show requests flowing down. Each box has a short "
            "label in quotes, e.g. \"Order Service\". Flat vector style, rounded rectangles, a calm palette of navy, "
            "teal and light grey on a white background, generous spacing, no decorative clutter."
        ),
        "tips": "Use quality=best when there are more than ~8 labels. List every label you need in quotes.",
    },
    "network_topology": {
        "title": "Network topology",
        "description": "Enterprise / hybrid-cloud network topology figure.",
        "aspect_ratio": "16:9",
        "example": (
            "An enterprise network topology diagram for an IT infrastructure proposal. A core switch pair in the "
            "center connects to two firewalls, a DMZ with web servers on the right, three branch offices on the left "
            "over SD-WAN links, and a cloud region at the top. Devices drawn as consistent isometric icons, links as "
            "straight colored lines with short labels such as \"10G\" and \"IPsec\". White background, blue and cyan "
            "accents, tidy symmetrical layout."
        ),
        "tips": "Name every device group and link label. Ask for a symmetrical layout to avoid tangled lines.",
    },
    "infographic": {
        "title": "Infographic / report poster",
        "description": "Data-driven poster or one-page summary with Chinese or English text.",
        "aspect_ratio": "3:4",
        "example": (
            "A one-page infographic poster summarizing a digital transformation project for a management report. "
            "Title at the top: \"2026 数字化转型成果\" in bold sans-serif. Below it three large metric cards: "
            "\"系统上线 42 个\", \"效率提升 35%\", \"成本下降 18%\", each with a simple icon. A timeline across the bottom "
            "with four milestones. Clean corporate style, deep navy and cyan, plenty of white space, aligned grid."
        ),
        "tips": "Settle every number and phrase first and quote them exactly. Use quality=best for text-heavy posters.",
    },
    "slide_background": {
        "title": "Slide or web background with copy space",
        "description": "Minimal background that leaves room for text overlay.",
        "aspect_ratio": "16:9",
        "example": (
            "A minimalist background for a presentation title slide about cloud security. A single abstract glass "
            "shield shape sits in the bottom-right of the frame. The rest is a vast, softly graded deep-blue canvas, "
            "leaving the left two-thirds empty for a title. Soft, diffused light from the top left."
        ),
        "tips": "Say where the subject sits and which area must stay empty for text.",
    },
    "product_shot": {
        "title": "Product shot",
        "description": "Studio product photo for e-commerce, proposals or marketing.",
        "aspect_ratio": "1:1",
        "example": (
            "A high-resolution studio product photograph of a matte black edge server appliance for a product "
            "brochure, on a polished concrete surface. Three-point softbox lighting creates soft highlights along "
            "its edges. Slightly elevated 45-degree angle to show the front panel ports. Sharp focus on the front "
            "panel, clean light grey background."
        ),
        "tips": "Set grounding=true for a real, named product so it looks like the actual item.",
    },
    "cover_banner": {
        "title": "Cover or banner",
        "description": "Article cover, event banner or report cover with a headline.",
        "aspect_ratio": "21:9",
        "example": (
            "A wide event banner for an internal AI summit. On the left, the headline \"AI 赋能 · 2026 技术峰会\" in a "
            "bold modern sans-serif, white on a dark indigo background. On the right, an abstract flowing network of "
            "glowing particles forming a brain shape. Cinematic lighting, subtle gradient, clean composition."
        ),
        "tips": "Keep the headline under ~12 characters or words; long copy is where text errors appear.",
    },
}


def scene_prompt_text(key: str, subject: str = "") -> str:
    sc = WORK_SCENE_PROMPTS[key]
    subject_line = f"\nWhat the user needs: {subject}\n" if subject else ""
    return (
        f"Write a generate_image call for: {sc['title']} ({sc['description']}).{subject_line}\n"
        f"Suggested aspect_ratio: {sc['aspect_ratio']}.\n"
        f"Tip: {sc['tips']}\n\n"
        f"Example prompt in the right style (adapt it, don't copy it):\n{sc['example']}"
    )


# ---------------------------------------------------------------------------
# Gemini 模型 ID：俗称 / 旧 preview ID -> 当前正式 ID（AI Studio 与 Vertex 通用）
# ---------------------------------------------------------------------------
GEMINI_MODEL_ALIASES = {
    "nano-banana-2.1": "gemini-nano-banana-2.1",
    "nano-banana-2": "gemini-3.1-flash-image",
    "nano-banana-pro": "gemini-3-pro-image",
    "nano-banana-2-lite": "gemini-3.1-flash-lite-image",
    "nano-banana": "gemini-2.5-flash-image",
    "gemini-3.1-flash-image-preview": "gemini-3.1-flash-image",
    "gemini-3-pro-image-preview": "gemini-3-pro-image",
    "gemini-2.5-flash-image-preview": "gemini-2.5-flash-image",
}

# 支持思考档位 / 搜索接地的 Gemini 图像模型（Lite 与 2.5 不支持）
GEMINI_REASONING_IMAGE_MODELS = ("gemini-nano-banana-2.1", "gemini-3.1-flash-image", "gemini-3-pro-image")


def normalize_gemini_model(model: str) -> str:
    m = (model or "").strip().replace("google/", "").replace("models/", "")
    return GEMINI_MODEL_ALIASES.get(m.lower(), m)


def gemini_supports_reasoning(model: str) -> bool:
    return normalize_gemini_model(model) in GEMINI_REASONING_IMAGE_MODELS


def is_premium_model(model: str) -> bool:
    """quality=best 时优先的模型（目前即 Nano Banana Pro 一类）。"""
    m = (model or "").lower()
    return "pro" in m and "image" in m


# ---------------------------------------------------------------------------
# 降级适配：比例、长度、档位
# ---------------------------------------------------------------------------
def _ratio_value(r: str) -> float:
    a, _, b = r.partition(":")
    return float(a) / float(b)


def nearest_aspect(target: str, allowed: list[str]) -> str:
    """在 allowed 中找与 target 最接近的比例（对数距离），target 已支持则原样返回。"""
    if not allowed or target in allowed:
        return target
    try:
        t = math.log(_ratio_value(target))
    except (ValueError, ZeroDivisionError):
        return allowed[0]
    return min(allowed, key=lambda r: abs(math.log(_ratio_value(r)) - t))


def trim_prompt(prompt: str, limit: int) -> str:
    """按句子边界截断到 limit 字符以内，保住前面的主体描述。"""
    if len(prompt) <= limit:
        return prompt
    cut = prompt[:limit]
    ends = [m.end() for m in re.finditer(r"[。！？.!?;；]\s*", cut)]
    if ends and ends[-1] >= limit * 0.6:
        return cut[:ends[-1]].strip()
    return cut.rstrip()


# 各降级模型的硬限制（来自官方文档）
FALLBACK_LIMITS = {
    # MiniMax image-01：prompt 最长 1500 字符；无负向提示词；按比例出图，无分辨率档位
    "minimax": {"max_prompt_chars": 1500, "resolutions": None},
    # Agnes image 2.5 flash：size 档位 1K/2K/3K/4K + ratio
    "agnes": {"max_prompt_chars": None, "resolutions": ["1K", "2K", "3K", "4K"]},
}


def adapt_request(provider: str, prompt: str, aspect_ratio: str, resolution: str,
                  allowed_aspects: list[str], grounding: bool) -> tuple[str, str, str, list[str]]:
    """按目标模型调整请求，返回 (prompt, aspect_ratio, resolution, adjustments)。"""
    notes: list[str] = []
    aspect = nearest_aspect(aspect_ratio, allowed_aspects or [])
    if aspect != aspect_ratio:
        notes.append(f"{provider} 不支持 {aspect_ratio}，改用最接近的 {aspect}")

    limits = FALLBACK_LIMITS.get(provider, {})
    max_chars = limits.get("max_prompt_chars")
    if max_chars and len(prompt) > max_chars:
        prompt = trim_prompt(prompt, max_chars)
        notes.append(f"{provider} 提示词上限 {max_chars} 字符，已在句子边界截断")

    res_tiers = limits.get("resolutions")
    if res_tiers is not None and resolution not in res_tiers:
        resolution = "2K"

    if grounding and provider != "google":
        notes.append(f"{provider} 不支持搜索接地，已按纯文本生成")
    return prompt, aspect, resolution, notes


def minimax_prompt_optimizer(prompt: str, override=None) -> bool:
    """Agent 已写出完整描述时关闭 MiniMax 自动改写，避免意图被冲淡；只有很短的提示词才开启。"""
    if override is not None:
        return bool(override)
    return len(prompt) < 120
