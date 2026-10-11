"""
scratch/test_e2e_flow.py
端到端完整闭环验证：
1. 一键全量同步 EdgeOne 中枢凭据 (POST /api/vault/sync)
2. 纯点选模型挂载 (模拟前端逻辑：选厂商 -> 纯点选适配模型 -> 自动绑定Key入库)
3. 行内角色快速修改并自动持久化 (通用 -> 写真 / 文档)
4. 状态启用与禁用切换
5. 模型移除
"""
import sys
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_manager import ConfigManager
import server

async def run_e2e_flow():
    print("🚀 开始执行控制台全流程闭环端到端验证...")
    test_cfg_path = Path(__file__).resolve().parent / "test_config.json"
    if test_cfg_path.exists():
        test_cfg_path.unlink()

    cm = ConfigManager(config_path=test_cfg_path)
    server.config_mgr = cm

    # 1. 验证一键从 EdgeOne 同步所有凭据
    print("\n[Step 1] 模拟点击「一键同步全部凭据」(/api/vault/sync)...")
    mock_client = AsyncMock()

    # 模拟中枢返回的 manifest
    mock_manifest_resp = MagicMock()
    mock_manifest_resp.is_success = True
    mock_manifest_resp.status_code = 200
    mock_manifest_resp.json.return_value = {
        "ok": True,
        "providers": {
            "google": {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "keys": ["default", "work"]},
            "minimax": {"name": "MiniMax", "base_url": "https://api.minimaxi.com/v1", "keys": ["default", "vip"]},
            "agnes": {"name": "Agnes AI", "base_url": "https://apihub.agnes-ai.com/v1", "keys": ["default"]}
        }
    }

    # 模拟拉取纯凭据返回
    def mock_post(url, headers=None, json=None, **kwargs):
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        p_name = (json or {}).get("provider", "google")
        resp.json.return_value = {
            "ok": True,
            "provider": p_name,
            "base_url": f"https://api.{p_name}.com/v1",
            "keys": {
                "default": f"sk-{p_name}-default-key-12345",
                "vip": f"sk-{p_name}-vip-key-67890"
            }
        }
        return resp

    mock_client.get = AsyncMock(return_value=mock_manifest_resp)
    mock_client.post = AsyncMock(side_effect=mock_post)

    server.mcp._http_client = mock_client
    if True:
        # 构造模拟请求
        class DummyRequest:
            async def json(self):
                return {"edgeone_url": "https://api.khc6.cn", "token": "test-token"}
            @property
            def headers(self):
                return {"Authorization": f"Bearer {cm.get('auth', 'admin_token')}"}

        resp = await server.vault_sync_api(DummyRequest())
        res_data = json.loads(resp.body.decode())
        assert res_data["ok"] is True
        print(f"  -> {res_data['message']}")
        # 确认凭据已入库
        providers = cm.get("providers")
        assert "agnes" in providers
        assert "default" in providers["agnes"]["keys"]
        print("  -> Google, MiniMax, Agnes 全部凭据与本地 Presets 自动合并落盘！")

    # 2. 模拟前端【纯点选挂载模型】（零键盘输入）
    print("\n[Step 2] 模拟纯点选官方适配模型挂载...")
    # 模拟挂载 Agnes 最新 flagship 模型
    presets = cm.get_all_presets()["presets"]
    assert "agnes" in presets
    agnes_models = presets["agnes"]["image_models"]
    chosen_model = agnes_models[0]["name"]  # agnes-image-2.5-flash
    
    # 执行添加至生图活跃模型池
    gen_models = cm.get("generation", "models", default=[])
    gen_models.append({
        "model": chosen_model,
        "provider": "agnes",
        "key": "default",
        "role": "general",
        "enabled": True
    })
    cm.update({"generation": {"models": gen_models}})
    print(f"  -> 成功点选并挂载「{chosen_model}」至生图模型池，底层自动关联 default 凭据！")

    # 3. 模拟【行内直接修改角色】（无需删除重加）
    print("\n[Step 3] 模拟表格行内直接切换偏好角色...")
    found_idx = next(i for i, m in enumerate(cm.get("generation", "models")) if m["model"] == chosen_model)
    # 行内切换为 portrait (写真)
    current_models = cm.get("generation", "models")
    current_models[found_idx]["role"] = "portrait"
    cm.update({"generation": {"models": current_models}})
    
    updated_role = cm.get("generation", "models")[found_idx]["role"]
    assert updated_role == "portrait"
    print(f"  -> 行内角色秒级切换为「{updated_role}」并自动持久化！")

    # 4. 模拟【行内状态启停开关】
    print("\n[Step 4] 模拟行内启用/禁用切换...")
    current_models[found_idx]["enabled"] = False
    cm.update({"generation": {"models": current_models}})
    assert cm.get("generation", "models")[found_idx]["enabled"] is False
    print("  -> 模型状态切换为 [禁用]，即时生效！")

    # 5. 模拟【模型移除】
    print("\n[Step 5] 模拟模型移除...")
    current_models.pop(found_idx)
    cm.update({"generation": {"models": current_models}})
    assert not any(m["model"] == chosen_model for m in cm.get("generation", "models"))
    print(f"  -> 成功从模型池移除「{chosen_model}」！")

    # 6. 模拟【挂载模型时自主选择 Key】
    print("\n[Step 6] 模拟挂载模型时显式绑定指定 Key (vip)...")
    gen_models = [
        {
            "model": "image-01",
            "provider": "minimax",
            "key": "vip",
            "role": "general",
            "enabled": True,
            "advanced": {"prompt_optimizer": True, "aigc_watermark": False}
        },
        {
            "model": "imagen-3.0-generate-002",
            "provider": "google",
            "key": "work",
            "role": "portrait",
            "enabled": True,
            "advanced": {"person_generation": "ALLOW_ADULT"}
        }
    ]
    cm.update({"generation": {"models": gen_models}})
    assert cm.get("generation", "models")[0]["key"] == "vip"
    print("  -> 成功挂载 MiniMax 模型并显式绑定 vip Key 与高级参数！")

    # 7. 模拟【行内直接切换绑定 Key】
    print("\n[Step 7] 模拟表格行内直接切换 Key (vip -> default)...")
    cm.get("generation", "models")[0]["key"] = "default"
    cm.update({"generation": {"models": cm.get("generation", "models")}})
    assert cm.get("generation", "models")[0]["key"] == "default"
    print("  -> 行内 Key 秒级切换为 default 并自动保存！")

    # 8. 模拟【编辑弹窗修改高级参数】
    print("\n[Step 8] 模拟编辑弹窗修改高级参数 (prompt_optimizer=False, aigc_watermark=True)...")
    cm.get("generation", "models")[0]["advanced"] = {
        "prompt_optimizer": False,
        "aigc_watermark": True
    }
    cm.update({"generation": {"models": cm.get("generation", "models")}})
    adv = cm.get("generation", "models")[0]["advanced"]
    assert adv["prompt_optimizer"] is False
    assert adv["aigc_watermark"] is True
    print("  -> 高级参数成功持久化保存！")

    # 9. 模拟【纯粹的顺序优先级调度与链式故障转移】
    print("\n[Step 9] 模拟生图顺序优先级调度与故障转移...")
    call_log = []

    async def mock_execute_gen(*args, **kwargs):
        provider = kwargs.get("provider_id")
        call_log.append(provider)
        if provider == "minimax":
            # 模拟第 1 位主力节点网络超时或异常
            raise RuntimeError("MiniMax 接口临时网络故障 (502 Bad Gateway)")
        elif provider == "google":
            # 备用节点正常响应
            return [{"b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "mime": "image/png"}]
        return []

    with patch("server.execute_image_generation", side_effect=mock_execute_gen):
        imgs = await server.generate_image(prompt="测试落日余晖")
        assert len(imgs) == 1
        assert call_log == ["minimax", "google"], f"预期故障转移调用 [minimax, google]，实际为 {call_log}"
        print("  -> 主力模型 minimax 故障，自动按序顺延切换至第 2 顺位 google 并成功生成图像！")

    # 10. 模拟【显式定向指定模型调用】
    print("\n[Step 10] 模拟 Agent 显式定向指定模型调用 (provider='google')...")
    call_log.clear()
    with patch("server.execute_image_generation", side_effect=mock_execute_gen):
        imgs = await server.generate_image(prompt="测试肖像", provider="google")
        assert len(imgs) == 1
        assert call_log == ["google"], f"显式定向指定预期调用 [google]，实际为 {call_log}"
        print("  -> 显式指定 provider='google' 成功直接定向命中目标模型！")

    if test_cfg_path.exists():
        test_cfg_path.unlink()

    print("\n==========================================")
    print("🎉 控制台与后端顺序优先级调度 E2E 测试 100% 成功！")
    print("==========================================")

if __name__ == "__main__":
    asyncio.run(run_e2e_flow())
