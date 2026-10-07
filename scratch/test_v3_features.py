import os
import sys
import base64
import unittest
from pathlib import Path

# 添加 mcp-server 路径
server_dir = Path(__file__).resolve().parent.parent / "caddy" / "mcp-server"
sys.path.insert(0, str(server_dir))

import server

class TestV3Features(unittest.TestCase):
    def test_aspect_ratios(self):
        expected_ratios = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "4:5", "5:4", "21:9"]
        for r in expected_ratios:
            self.assertIn(r, server.ASPECT_TO_SIZE, f"Missing aspect ratio {r}")
        print("✅ 10 种宽高比完整性验证通过")

    def test_scene_presets(self):
        expected_scenes = ["infra_scene", "network_topology", "architecture", "infographic", "icon_set"]
        for sc in expected_scenes:
            self.assertIn(sc, server.SCENE_PRESETS, f"Missing scene {sc}")
            item = server.SCENE_PRESETS[sc]
            self.assertIn("prompt_prefix", item)
            self.assertIn("prompt_suffix", item)
            self.assertIn("negative_prompt", item)
            self.assertIn("default_aspect", item)
            self.assertIn("default_resolution", item)
        print("✅ 5 大专业办公生图场景模板验证通过")

    def test_mime_detection(self):
        png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        jpeg_b64 = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP"
        webp_b64 = "UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAA"
        self.assertEqual(server.detect_image_mime(png_b64), "image/png")
        self.assertEqual(server.detect_image_mime(jpeg_b64), "image/jpeg")
        self.assertEqual(server.detect_image_mime(webp_b64), "image/webp")
        print("✅ Base64 MIME 动态特征识别验证通过")

    def test_lfi_protection(self):
        import asyncio
        async def run_lfi():
            class DummyClient:
                pass
            with self.assertRaises(ValueError) as ctx:
                await server.resolve_image_to_base64(DummyClient(), "/etc/passwd")
            self.assertIn("安全策略拦截", str(ctx.exception))

            with self.assertRaises(ValueError) as ctx2:
                await server.resolve_image_to_base64(DummyClient(), "../config.json")
            self.assertIn("安全策略拦截", str(ctx2.exception))

        asyncio.run(run_lfi())
        print("✅ 本地敏感文件读取 (LFI) 安全拦截验证通过")

    def test_persistence_helper(self):
        tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        meta = server.save_generated_image_to_disk(tiny_png, "image/png")
        self.assertIn("saved_path", meta)
        self.assertIn("relative_url", meta)
        self.assertTrue(os.path.exists(meta["saved_path"]))
        self.assertTrue(meta["relative_url"].startswith("/static/generated/"))
        print(f"✅ 图片本地持久化验证通过: {meta['relative_url']}")

if __name__ == "__main__":
    unittest.main()
