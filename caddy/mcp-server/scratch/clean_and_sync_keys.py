#!/usr/bin/env python3
"""
清洗并恢复 config.json 中的真实有效 Key，剔除所有测试 mock 键，并移除模型池中的 key 冗余绑定。
"""
import json
import sys
from pathlib import Path

def clean_config(config_path: Path):
    if not config_path.exists():
        print(f"File not found: {config_path}")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    providers = data.get("providers", {})
    cleaned_count = 0

    for pid, pdata in providers.items():
        keys = pdata.get("keys", {})
        valid_keys = {}
        for k, s in keys.items():
            if not isinstance(s, str):
                continue
            s_clean = s.strip()
            # 过滤测试假 key
            if s_clean.endswith("-12345") or s_clean.endswith("-67890") or "default-key" in s_clean or "vip-key" in s_clean:
                cleaned_count += 1
                continue
            valid_keys[k] = s_clean

        # 若 default 被过滤了但存在其他真实有效 key，将 default 绑定到第一个真实有效 key
        if "default" not in valid_keys and valid_keys:
            first_valid = next(iter(valid_keys.values()))
            valid_keys["default"] = first_valid

        pdata["keys"] = valid_keys
        pdata["key_labels"] = list(valid_keys.keys())

    # 清理 generation.models 和 vision.models 中的 key 字段
    for m in data.get("generation", {}).get("models", []):
        if "key" in m:
            del m["key"]
    for m in data.get("vision", {}).get("models", []):
        if "key" in m:
            del m["key"]

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ 成功清洗 {config_path}，移除了 {cleaned_count} 个测试假 Key，模型池彻底去 Key 化！")

if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.json")
    clean_config(p)
