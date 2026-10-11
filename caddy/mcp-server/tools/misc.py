"""辅助工具：提示词增强与模型/配置查询。"""
from typing import Literal

import httpx

from config_manager import config_mgr

import providers
from core import (
    mcp, logger, SCENE_PRESETS, _annotations,
)


# ---------------------------------------------------------------------------
# 辅助工具: 提示词增强与能力列表
# ---------------------------------------------------------------------------
@mcp.tool(annotations=_annotations(read_only=True, title="提示词增强"))
async def enhance_prompt(prompt: str, style: str = "realistic", purpose: Literal["general", "work"] = "general") -> str:
    """根据目标风格与用途自动优化扩展绘图提示词。

    参数说明:
        prompt: 用户的简短原始构思。
        style: 画面风格偏好，例如 "realistic", "isometric", "vector", "cyberpunk", "minimalist" 等。
        purpose: 用途场景，"general"(通用艺术创作) 或 "work"(信息化建设/办公拓扑/软件架构专业场景)。
    """
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")

    if purpose == "work" or style in ("work", "tech", "topology", "architecture", "infographic"):
        sys_prompt = (
            "你是一个顶尖的企业级信息化与技术视觉架构设计专家。请将用户的简短构思扩写为适用于专业生图引擎的高精度英文 Prompt。"
            "重点突出：清晰的技术分层结构、扁平或 2.5D 等距视角、工整的高对比度色彩、干净的纯白或科技感背景、整洁的连线与模块规范。"
            f"风格偏好: {style}。\n"
            "直接输出优化后的英文 Prompt，不要包含任何前导词或额外解释。\n\n"
            f"用户构思: {prompt}"
        )
    else:
        sys_prompt = f"你是一个顶级 AI 绘图提示词大师。请将用户的简短构思扩写为适用于 Midjourney/Imagen 的极具美感、细节丰富的绘图 Prompt。风格偏好: {style}。直接输出优化后的英文 Prompt，不要任何解释。\n\n用户构思: {prompt}"

    vision_models = [
        m for m in config_mgr.get("vision", "models", default=[])
        if isinstance(m, dict) and m.get("enabled", True)
    ]
    for vm in vision_models:
        try:
            res = await providers.execute_vision_completion(
                client=client,
                provider_id=vm["provider"],
                model=vm["model"],
                key_label=vm.get("key", ""),
                prompt=sys_prompt
            )
            if res:
                return res
        except Exception as e:
            logger.warning(f"提示词优化节点 {vm.get('provider')}/{vm.get('model')} 失败: {e}")
    return prompt


@mcp.tool(annotations=_annotations(read_only=True, open_world=False, idempotent=True, title="模型与配置查询"))
async def list_models() -> dict:
    """查询当前系统在本地已持久化的供应商、生图模型池、识图模型池、办公场景模板及运行配置。"""
    cfg = config_mgr.get_all()
    provs = cfg.get("providers", {})
    return {
        "service": "KHC Image MCP Suite",
        "version": "3.0",
        "local_providers": [
            {
                "id": pid,
                "name": p.get("name", pid),
                "preset_version": p.get("preset_version", "1.1.0"),
                "keys_count": len(p.get("keys") or {})
            }
            for pid, p in provs.items()
        ],
        "current_generation_mode": cfg.get("generation", {}).get("default_mode", "sequential"),
        "generation_defaults": {
            "aspect_ratio": cfg.get("generation", {}).get("default_aspect_ratio") or cfg.get("generation", {}).get("default_aspect", "1:1"),
            "resolution": cfg.get("generation", {}).get("default_resolution", "auto"),
            "scene": cfg.get("generation", {}).get("default_scene", "none"),
        },
        "available_scenes": [
            {"id": sid, "name": sinfo["name"], "description": sinfo["description"]}
            for sid, sinfo in SCENE_PRESETS.items()
        ],
        "generation_models": [
            {k: m.get(k) for k in ("provider", "model", "role", "enabled")}
            for m in cfg.get("generation", {}).get("models", []) if isinstance(m, dict)
        ],
        "current_ocr_preset": cfg.get("vision", {}).get("default_preset", "standard"),
        "vision_models": [
            {k: m.get(k) for k in ("provider", "model", "role", "enabled")}
            for m in cfg.get("vision", {}).get("models", []) if isinstance(m, dict)
        ],
        "current_search_engine": cfg.get("search", {}).get("default_text_engine", "duckduckgo")
    }
