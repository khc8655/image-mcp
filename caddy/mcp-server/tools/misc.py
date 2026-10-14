"""辅助工具：模型与配置查询。"""
from config_manager import config_mgr

import prompting
from core import mcp, _annotations


@mcp.tool(annotations=_annotations(read_only=True, open_world=False, idempotent=True, title="模型与配置查询"))
async def list_models() -> dict:
    """List configured providers, generation/vision model order and the server defaults applied when a parameter is omitted."""
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
            "quality": cfg.get("generation", {}).get("default_quality", "auto"),
            "grounding": bool(cfg.get("generation", {}).get("default_grounding", False)),
            "count": cfg.get("generation", {}).get("default_count", 1),
        },
        "work_scene_prompts": [
            {"name": k, "title": v["title"], "description": v["description"]}
            for k, v in prompting.WORK_SCENE_PROMPTS.items()
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
