"""
scratch/test_dual_pillars_mcp.py
验证双支柱（生图与识图）本地预设加载、配置管理纯凭据入库、双协议分发逻辑测试
"""
import sys
import json
from pathlib import Path

# 添加当前路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_manager import ConfigManager
import server

def test_presets_loading():
    print("[1] 测试本地预设 presets 加载...")
    cm = ConfigManager()
    presets_data = cm.get_all_presets()
    presets = presets_data.get("presets", {})
    assert "agnes" in presets, "presets 中必须包含 agnes"
    assert "minimax" in presets, "presets 中必须包含 minimax"
    assert "google" in presets, "presets 中必须包含 google"
    
    agnes = presets["agnes"]
    assert agnes["adapter_rules"]["image_generation"]["protocol"] == "agnes_image"
    assert agnes["adapter_rules"]["vision"]["protocol"] == "agnes_vision"
    
    minimax = presets["minimax"]
    assert "image-01-live" in [m["name"] if isinstance(m, dict) else m for m in minimax.get("image_models", [])]
    
    google = presets["google"]
    g_img_names = [m["name"] if isinstance(m, dict) else m for m in google.get("image_models", [])]
    assert "gemini-3.1-flash-lite-image" in g_img_names
    assert "gemini-3-pro-image" in g_img_names
    print("  -> 本地预设加载完全成功！涵盖三大最新厂商规格。")

def test_vault_credentials_upsert():
    print("[2] 测试纯凭据对接 (EdgeOne Vault 纯提供商+端点+密钥)...")
    cm = ConfigManager()
    mock_payload = {
        "ok": True,
        "provider": "agnes",
        "name": "Agnes AI (From Vault)",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "keys": {
            "default": "sk-agnes-test-123456"
        }
    }
    updated = cm.upsert_provider_from_vault(
        provider_id="agnes",
        prov_config=mock_payload
    )
    assert updated is not None
    assert updated["base_url"] == "https://apihub.agnes-ai.com/v1"
    assert updated["keys"]["default"] == "sk-agnes-test-123456"
    assert "agnes-image-2.5-flash" in updated["image_models"]
    assert "agnes-3.0-flash" in updated["models"]
    print("  -> 纯凭据入库测试成功，模型与规则完全由本地 Presets 填充！")

def test_vision_and_image_rules():
    print("[3] 测试 ConfigManager 生图与识图规则提取...")
    cm = ConfigManager()
    vis_rules = cm.get_vision_adapter_rules("agnes")
    assert vis_rules["protocol"] == "agnes_vision"
    
    img_rules = cm.get_image_adapter_rules("agnes")
    assert img_rules["protocol"] == "agnes_image"
    assert "16:9" in img_rules["allowed_aspect_ratios"]
    assert "21:9" in img_rules["allowed_aspect_ratios"]
    print("  -> 生图与识图适配规则提取完全对称且准确！")

def test_safe_dict():
    print("[4] 测试前端安全脱敏输出与双支柱能力标志...")
    cm = ConfigManager()
    safe = cm.get_safe_dict()
    assert "providers" in safe
    assert "agnes" in safe["providers"]
    agnes_safe = safe["providers"]["agnes"]
    assert agnes_safe["has_image"] is True
    assert agnes_safe["has_vision"] is True
    assert "vision_models" in agnes_safe
    assert "image_models" in agnes_safe
    assert "default_mode" in safe["vision"]
    print("  -> 前端 API 安全配置提取验证通过！")

if __name__ == "__main__":
    try:
        test_presets_loading()
        test_vault_credentials_upsert()
        test_vision_and_image_rules()
        test_safe_dict()
        print("\n==========================================")
        print("🎉 全部双支柱核心模块单元测试顺利通过！")
        print("==========================================")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
