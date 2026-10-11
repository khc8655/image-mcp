#!/usr/bin/env python3
"""
Config Manager for KHC Image MCP Suite
负责 config.json 的持久化读写、EdgeOne Vault 凭据与 GitHub Presets 适配规则的本地存储管理。
"""

import json
import os
import secrets
import copy
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("mcp-config")

DEFAULT_IMAGE_ADAPTER_RULES: Dict[str, Dict[str, Any]] = {
    "google": {
        "protocol": "gemini_image",
        "native_base_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "vertex_project": "jjb111",
        "vertex_location": "global",
        "endpoint_suffix": ":generateContent",
        "supports_edit": True,
        "response_modalities": ["TEXT", "IMAGE"],
        "allowed_aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
    },
    "minimax": {
        "protocol": "minimax_image",
        "base_url": "https://api.minimaxi.com/v1",
        "endpoint": "/image_generation",
        "supports_edit": True,
        "default_params": {
            "response_format": "base64",
            "prompt_optimizer": True,
            "aigc_watermark": False,
        },
        "edit_reference_field": "subject_reference",
        "edit_reference_type": "character",
        "response_path": "data.image_base64",
        "allowed_aspect_ratios": ["1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"],
    },
    "agnes": {
        "protocol": "agnes_image",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "endpoint": "/images/generations",
        "supports_edit": True,
        "default_size": "2K",
        "allowed_aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"],
        "default_params": {
            "extra_body": {
                "response_format": "b64_json"
            }
        },
    },
    "sensenova": {
        "protocol": "openai_images",
        "base_url": "https://api.sensenova.cn/v1",
        "endpoint": "/images/generations",
        "supports_edit": False,
        "size_map": {
            "1:1": "2048x2048",
            "16:9": "2752x1536",
            "9:16": "1536x2752",
            "4:3": "2272x1824",
            "3:4": "1824x2272",
        },
        "response_b64_fields": ["b64_image", "b64_json", "base64"],
    },
}

DEFAULT_VISION_ADAPTER_RULES: Dict[str, Dict[str, Any]] = {
    "google": {
        "protocol": "gemini_vision",
        "native_base_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "vertex_project": "jjb111",
        "vertex_location": "global",
        "endpoint_suffix": ":generateContent",
        "supports_ocr": True,
        "detail": "high",
    },
    "minimax": {
        "protocol": "minimax_vision",
        "base_url": "https://api.minimaxi.com/v1",
        "endpoint": "/chat/completions",
        "case_sensitive_models": {
            "minimax-m3": "MiniMax-M3"
        },
        "supports_ocr": True,
        "detail": "high",
        "max_tokens": 4096,
    },
    "agnes": {
        "protocol": "agnes_vision",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "endpoint": "/chat/completions",
        "supports_ocr": True,
        "detail": "high",
        "max_tokens": 4096,
    },
    "sensenova": {
        "protocol": "openai_vision",
        "base_url": "https://api.sensenova.cn/v1",
        "endpoint": "/chat/completions",
        "supports_ocr": True,
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "auth": {
        # 管理面 Token：只认 ADMIN_TOKEN，不再回退到 CLIENT_AUTH_TOKEN（调用面与管理面分离）
        "admin_token": os.environ.get("ADMIN_TOKEN", ""),
        # 调用面 Token 列表：[{"name": "cursor", "token": "...", "enabled": true, "rpm": 60}]
        "client_tokens": [],
        "default_rpm": 120,
        "signing_secret": ""
    },
    "server": {
        # 生图结果签名 URL 的公网前缀，例如 https://oc.khc6.cn ；为空时生图结果回退为内联 Base64
        "public_base_url": os.environ.get("PUBLIC_BASE_URL", "")
    },
    "edgeone_vault": {
        "url": os.environ.get("EDGEONE_VAULT_URL", "https://api.khc6.cn"),
        "token": os.environ.get("EDGEONE_VAULT_TOKEN", "")
    },
    "providers": {},
    "generation": {
        "default_mode": "sequential",  # sequential
        "timeout_seconds": 45,
        "fallback_enabled": True,
        "return_mode": "url",      # url: 签名 URL + 缩略图 | inline: 原图 Base64
        "url_ttl_hours": 24,
        "retention_days": 7,
        "models": []
    },
    "vision": {
        "default_mode": "sequential",  # sequential
        "timeout_seconds": 45,
        "fallback_enabled": True,
        "default_preset": "standard",  # fast | standard | document | gui
        "rapidocr_enabled": True,
        "models": []
    },
    "search": {
        "default_text_engine": "duckduckgo",  # duckduckgo | bing | pexels
        "pexels_api_key": "",
        "reverse_search_use_ocr": True,
        "max_results": 6
    }
}


def _mask_secret(val: str) -> str:
    if not val:
        return ""
    if len(val) > 8:
        return val[:4] + "*" * min(12, len(val) - 8) + val[-4:]
    return "******"


class ConfigManager:
    def __init__(self, config_path: str = None):
        config_path = config_path or os.environ.get("IMAGE_MCP_CONFIG")
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = Path(__file__).resolve().parent / "config.json"
        self._config: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.presets_dir = Path(__file__).resolve().parent / "presets"
        self._presets: Dict[str, Any] = {}
        self.reload_presets()
        self.load()

    def load(self) -> Dict[str, Any]:
        """从磁盘加载 config.json，若存在旧版 credentials 结构则自动平滑迁移至 providers 结构"""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    disk_config = json.load(f)
                self._deep_update(self._config, disk_config)
                if self._migrate_legacy_credentials(disk_config):
                    self.save()
                logger.info(f"Loaded config from {self.config_path}")
            except Exception as e:
                logger.error(f"Failed to load {self.config_path}: {e}, using defaults")
        else:
            self.save()
        self._ensure_admin_token()
        return self._config

    def _ensure_admin_token(self) -> None:
        """环境变量 ADMIN_TOKEN 优先于磁盘；均为空时生成随机 Token 并持久化。"""
        env_admin = os.environ.get("ADMIN_TOKEN", "").strip()
        auth = self._config.setdefault("auth", {})
        if env_admin and auth.get("admin_token") != env_admin:
            auth["admin_token"] = env_admin
            self.save()
        if not auth.get("admin_token"):
            auth["admin_token"] = secrets.token_urlsafe(32)
            self.save()
            logger.warning(f"未配置 ADMIN_TOKEN，已生成随机 Admin Token 并写入 {self.config_path} (auth.admin_token)")

    def _migrate_legacy_credentials(self, disk_config: Dict[str, Any]) -> bool:
        """将旧版零散的 credentials 迁移至统一的本地 providers + 模型绑定池结构"""
        migrated = False
        creds = disk_config.get("credentials", {})
        providers = self._config.setdefault("providers", {})

        # 迁移 pexels_api_key
        if isinstance(creds, dict) and creds.get("pexels_api_key") and not self._config["search"].get("pexels_api_key"):
            self._config["search"]["pexels_api_key"] = creds["pexels_api_key"]
            migrated = True

        # 若 providers 为空且旧版 credentials 存在密钥，自动转为本地 provider 存储
        if not providers and isinstance(creds, dict):
            v_key = creds.get("vertex_api_key", "")
            m_key = creds.get("minimax_api_key", "")
            s_key = creds.get("sensenova_api_key", "")

            if v_key:
                providers["google"] = {
                    "name": "Google AI Studio / Vertex (Gemini)",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    "protocol": "google_openai",
                    "preset_version": "1.1.0",
                    "adapter_rules": {
                        "image_generation": copy.deepcopy(DEFAULT_IMAGE_ADAPTER_RULES["google"]),
                        "vertex_project": creds.get("vertex_project", "jjb111"),
                        "vertex_location": creds.get("vertex_location", "global"),
                    },
                    "models": ["gemini-3.5-flash", "gemini-2.5-flash"],
                    "image_models": ["gemini-3.1-flash-lite-image", "gemini-3.1-flash-image"],
                    "keys": {"default": v_key}
                }
                migrated = True

            if m_key:
                providers["minimax"] = {
                    "name": "MiniMax",
                    "base_url": "https://api.minimaxi.com/v1",
                    "protocol": "openai",
                    "preset_version": "1.1.0",
                    "adapter_rules": {
                        "image_generation": copy.deepcopy(DEFAULT_IMAGE_ADAPTER_RULES["minimax"])
                    },
                    "models": ["MiniMax-M3"],
                    "image_models": ["image-01"],
                    "keys": {"default": m_key}
                }
                migrated = True

            if s_key:
                providers["sensenova"] = {
                    "name": "商汤日日新 (SenseNova)",
                    "base_url": "https://token.sensenova.cn/v1",
                    "protocol": "openai",
                    "preset_version": "1.1.0",
                    "adapter_rules": {
                        "image_generation": copy.deepcopy(DEFAULT_IMAGE_ADAPTER_RULES["sensenova"])
                    },
                    "models": ["sensenova-6.8-flash-lite", "glm-5.2"],
                    "image_models": ["sensenova-u1-fast"],
                    "keys": {"default": s_key}
                }
                migrated = True

        # 初始化默认生图模型池绑定
        gen_models = self._config["generation"].get("models")
        if not isinstance(gen_models, list) or len(gen_models) == 0:
            new_gen_models = []
            if "google" in providers and providers["google"].get("keys"):
                first_k = next(iter(providers["google"]["keys"].keys()))
                new_gen_models.append({
                    "model": "gemini-3.1-flash-lite-image",
                    "provider": "google",
                    "key": first_k,
                    "role": "general",
                    "enabled": True
                })
            if "minimax" in providers and providers["minimax"].get("keys"):
                first_k = next(iter(providers["minimax"]["keys"].keys()))
                new_gen_models.append({
                    "model": "image-01",
                    "provider": "minimax",
                    "key": first_k,
                    "role": "portrait",
                    "enabled": True
                })
            if "sensenova" in providers and providers["sensenova"].get("keys"):
                first_k = next(iter(providers["sensenova"]["keys"].keys()))
                new_gen_models.append({
                    "model": "sensenova-u1-fast",
                    "provider": "sensenova",
                    "key": first_k,
                    "role": "chinese",
                    "enabled": True
                })
            if new_gen_models:
                self._config["generation"]["models"] = new_gen_models
                migrated = True

        # 初始化默认识图模型池绑定
        vis_models = self._config["vision"].get("models")
        if not isinstance(vis_models, list) or len(vis_models) == 0:
            new_vis_models = []
            if "google" in providers and providers["google"].get("keys"):
                first_k = next(iter(providers["google"]["keys"].keys()))
                new_vis_models.append({
                    "model": "gemini-3.5-flash",
                    "provider": "google",
                    "key": first_k,
                    "enabled": True
                })
            if "minimax" in providers and providers["minimax"].get("keys"):
                first_k = next(iter(providers["minimax"]["keys"].keys()))
                new_vis_models.append({
                    "model": "MiniMax-M3",
                    "provider": "minimax",
                    "key": first_k,
                    "enabled": True
                })
            if new_vis_models:
                self._config["vision"]["models"] = new_vis_models
                migrated = True

        # 清理旧版 credentials 字段
        if "credentials" in self._config:
            self._config.pop("credentials", None)
            migrated = True

        return migrated

    def save(self) -> bool:
        """原子写入 config.json"""
        try:
            temp_path = self.config_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
            temp_path.replace(self.config_path)
            logger.info(f"Config successfully saved to {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save config to {self.config_path}: {e}")
            return False

    def get(self, *keys, default=None) -> Any:
        curr = self._config
        for k in keys:
            if isinstance(curr, dict) and k in curr:
                curr = curr[k]
            else:
                return default
        return curr

    def get_all(self) -> Dict[str, Any]:
        return copy.deepcopy(self._config)

    def reload_presets(self) -> int:
        """从 presets/ 目录加载所有独立提供商预设规则"""
        loaded = 0
        self._presets.clear()
        if not self.presets_dir.exists():
            return 0
        for p_file in self.presets_dir.glob("*.json"):
            if p_file.name == "catalog.json":
                continue
            try:
                with open(p_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    p_id = (data.get("id") or p_file.stem).lower().strip()
                    self._presets[p_id] = data
                    loaded += 1
            except Exception as e:
                logger.error(f"Failed to load preset {p_file.name}: {e}")
        logger.info(f"Loaded {loaded} independent provider presets from {self.presets_dir}")
        return loaded

    def get_preset(self, provider_id: str) -> Dict[str, Any]:
        """获取指定提供商的独立预设规则（若不存在返回空字典）"""
        return copy.deepcopy(self._presets.get((provider_id or "").lower().strip(), {}))

    def get_all_presets(self) -> Dict[str, Any]:
        """获取所有独立预设及 catalog 索引"""
        catalog = {}
        cat_file = self.presets_dir / "catalog.json"
        if cat_file.exists():
            try:
                with open(cat_file, "r", encoding="utf-8") as f:
                    catalog = json.load(f)
            except Exception:
                pass
        return {
            "catalog": catalog,
            "presets": copy.deepcopy(self._presets)
        }

    def get_image_adapter_rules(self, provider_id: str) -> Dict[str, Any]:
        """获取指定提供商的生图适配规则（独立 presets/*.json 优先，合并用户自定义覆盖，兜底默认规则）"""
        p_id = (provider_id or "").lower().strip()
        preset = self._presets.get(p_id) or {}
        preset_rules = (preset.get("adapter_rules") or {}).get("image_generation") or {}

        # 1. 基础规则：优先取独立预设文件中的 image_generation 规则
        base_rules = copy.deepcopy(DEFAULT_IMAGE_ADAPTER_RULES.get(p_id, {}))
        if preset_rules:
            self._deep_update(base_rules, preset_rules)

        # 2. 本地 config.json 中的用户个性化覆盖（如用户在管理端定制了参数）
        prov = self._config.get("providers", {}).get(provider_id) or self._config.get("providers", {}).get(p_id) or {}
        local_rules = (prov.get("adapter_rules") or {}).get("image_generation") or {}
        if isinstance(local_rules, dict) and local_rules:
            self._deep_update(base_rules, local_rules)

        if not base_rules:
            base_rules = {
                "protocol": "openai_images",
                "base_url": prov.get("base_url", preset.get("base_url", "")),
                "endpoint": "/images/generations",
                "supports_edit": False,
                "response_b64_fields": ["b64_json", "b64_image", "base64", "url"],
            }
        return base_rules

    def get_vision_adapter_rules(self, provider_id: str) -> Dict[str, Any]:
        """获取指定提供商的多模态识图适配规则（独立 presets/*.json 优先，合并用户自定义覆盖，兜底默认规则）"""
        p_id = (provider_id or "").lower().strip()
        preset = self._presets.get(p_id) or {}
        preset_rules = (preset.get("adapter_rules") or {}).get("vision") or {}

        # 1. 基础规则：优先取独立预设文件中的 vision 规则
        base_rules = copy.deepcopy(DEFAULT_VISION_ADAPTER_RULES.get(p_id, {}))
        if preset_rules:
            self._deep_update(base_rules, preset_rules)

        # 2. 本地 config.json 中的用户个性化覆盖
        prov = self._config.get("providers", {}).get(provider_id) or self._config.get("providers", {}).get(p_id) or {}
        local_rules = (prov.get("adapter_rules") or {}).get("vision") or {}
        if isinstance(local_rules, dict) and local_rules:
            self._deep_update(base_rules, local_rules)

        if not base_rules:
            base_rules = {
                "protocol": "openai_vision",
                "base_url": prov.get("base_url", preset.get("base_url", "")),
                "endpoint": "/chat/completions",
                "supports_ocr": True,
            }
        return base_rules

    def upsert_provider_from_vault(self, provider_id: str, prov_config: Dict[str, Any]) -> Dict[str, Any]:
        """将从 EdgeOne Vault 拉取到的纯凭据（base_url 与 Key）合并持久化到本地，规则与模型完全由本地 Presets 填充"""
        providers = self._config.setdefault("providers", {})
        p_id = provider_id.lower().strip()
        local_p = providers.setdefault(provider_id, {
            "name": provider_id,
            "base_url": "",
            "protocol": "openai",
            "preset_version": "1.3.0",
            "adapter_rules": {},
            "models": [],
            "image_models": [],
            "keys": {}
        })

        # 关联本地独立的 Presets 预设
        preset = self._presets.get(p_id) or {}
        if preset:
            local_p["preset_version"] = preset.get("version", "1.3.0")
            if not local_p.get("name") or local_p["name"] == provider_id:
                local_p["name"] = preset.get("name", provider_id)
            if not local_p.get("base_url"):
                local_p["base_url"] = preset.get("base_url", "")

        if prov_config.get("name"):
            local_p["name"] = prov_config["name"]
        if prov_config.get("base_url"):
            local_p["base_url"] = prov_config["base_url"]
        if prov_config.get("protocol"):
            local_p["protocol"] = prov_config["protocol"]

        # 模型列表合并（本地预设为主）
        preset_models = [
            (m["name"] if isinstance(m, dict) else m)
            for m in (preset.get("vision_models") or preset.get("recommended_models") or [])
        ]
        remote_models = prov_config.get("cached_models") or prov_config.get("models") or []
        all_models = list(dict.fromkeys(preset_models + remote_models))
        if all_models:
            local_p["models"] = all_models

        # 生图模型合并
        preset_img_models = [
            (im["name"] if isinstance(im, dict) else im)
            for im in (preset.get("image_models") or [])
        ]
        remote_img_models = [
            (im["name"] if isinstance(im, dict) else im)
            for im in (prov_config.get("image_models") or [])
        ]
        all_img = list(dict.fromkeys(preset_img_models + remote_img_models))
        if all_img:
            local_p["image_models"] = all_img

        # 合并 Keys (明文存储在服务端本地)
        local_keys = local_p.setdefault("keys", {})
        incoming_keys = prov_config.get("keys") or {}
        if isinstance(incoming_keys, dict):
            for k_label, k_secret in incoming_keys.items():
                if k_label and k_secret and "***" not in str(k_secret):
                    local_keys[k_label] = k_secret

        self.save()
        return local_p

    def update(self, new_data: Dict[str, Any]) -> bool:
        """更新配置并保存，自动保护脱敏掩码字段不被覆盖"""
        data_copy = copy.deepcopy(new_data)

        # 1. 保护 edgeone_vault token
        vault_in = data_copy.get("edgeone_vault")
        if isinstance(vault_in, dict):
            tk = vault_in.get("token", "")
            if isinstance(tk, str) and ("*" in tk or tk.strip() == ""):
                vault_in["token"] = self.get("edgeone_vault", "token", default="")

        # 1.5 保护 auth：掩码 / 空值不覆盖；签名密钥不允许经 Web 修改
        auth_in = data_copy.get("auth")
        if isinstance(auth_in, dict):
            auth_in.pop("signing_secret", None)
            at = auth_in.get("admin_token", "")
            if not isinstance(at, str) or "*" in at or not at.strip():
                auth_in.pop("admin_token", None)
            if isinstance(auth_in.get("client_tokens"), list):
                old_by_name = {
                    t.get("name"): t.get("token")
                    for t in (self.get("auth", "client_tokens", default=[]) or []) if isinstance(t, dict)
                }
                merged = []
                for t in auth_in["client_tokens"]:
                    if not isinstance(t, dict) or not t.get("name"):
                        continue
                    tk = t.get("token", "")
                    if not isinstance(tk, str) or "*" in tk or not tk.strip():
                        if not old_by_name.get(t["name"]):
                            continue
                        t["token"] = old_by_name[t["name"]]
                    merged.append(t)
                self._config.setdefault("auth", {})["client_tokens"] = merged
                auth_in.pop("client_tokens")

        # 2. 保护 search.pexels_api_key
        search_in = data_copy.get("search")
        if isinstance(search_in, dict):
            pk = search_in.get("pexels_api_key", "")
            if isinstance(pk, str) and "*" in pk:
                search_in["pexels_api_key"] = self.get("search", "pexels_api_key", default="")

        # 3. 保护 providers 中的 keys 明文
        provs_in = data_copy.get("providers")
        if isinstance(provs_in, dict):
            existing_provs = self._config.get("providers", {})
            for pid, pval in provs_in.items():
                if not isinstance(pval, dict):
                    continue
                old_keys = (existing_provs.get(pid) or {}).get("keys", {})
                in_keys = pval.get("keys")
                if isinstance(in_keys, dict):
                    merged_keys = {}
                    for kl, ksec in in_keys.items():
                        if isinstance(ksec, str) and ("*" in ksec or not ksec.strip()):
                            if kl in old_keys:
                                merged_keys[kl] = old_keys[kl]
                        else:
                            merged_keys[kl] = ksec
                    pval["keys"] = merged_keys
                else:
                    pval["keys"] = copy.deepcopy(old_keys)
            # 若前端传入完整的 providers 字典，允许删除被移除的 provider
            self._config["providers"] = provs_in
            data_copy.pop("providers", None)

        # 4. 对于 generation.models 和 vision.models 列表，直接整体替换
        if "generation" in data_copy and isinstance(data_copy["generation"], dict):
            if "models" in data_copy["generation"] and isinstance(data_copy["generation"]["models"], list):
                self._config.setdefault("generation", {})["models"] = data_copy["generation"].pop("models")
        if "vision" in data_copy and isinstance(data_copy["vision"], dict):
            if "models" in data_copy["vision"] and isinstance(data_copy["vision"]["models"], list):
                self._config.setdefault("vision", {})["models"] = data_copy["vision"].pop("models")

        self._deep_update(self._config, data_copy)
        return self.save()

    def get_safe_dict(self) -> Dict[str, Any]:
        """获取供 Web 前端展示的脱敏配置字典"""
        safe_copy = copy.deepcopy(self._config)

        # 脱敏 EdgeOne Vault Token
        vault = safe_copy.get("edgeone_vault", {})
        if isinstance(vault, dict):
            raw_tk = vault.get("token", "")
            vault["has_token"] = bool(raw_tk)
            vault["token"] = _mask_secret(raw_tk)

        # 脱敏管理面 / 调用面 Token，签名密钥不下发
        auth = safe_copy.get("auth", {})
        if isinstance(auth, dict):
            auth["admin_token"] = _mask_secret(auth.get("admin_token", ""))
            auth.pop("signing_secret", None)
            for item in auth.get("client_tokens") or []:
                if isinstance(item, dict):
                    item["token"] = _mask_secret(item.get("token", ""))

        # 脱敏 Pexels API Key
        search = safe_copy.get("search", {})
        if isinstance(search, dict) and search.get("pexels_api_key"):
            search["pexels_api_key"] = _mask_secret(search["pexels_api_key"])

        # 脱敏本地存储的各供应商 Keys，并注入双支柱模型及能力标志
        provs = safe_copy.get("providers", {})
        if isinstance(provs, dict):
            for pid, pdata in provs.items():
                if isinstance(pdata, dict):
                    raw_keys = pdata.get("keys", {})
                    pdata["key_labels"] = list(raw_keys.keys()) if isinstance(raw_keys, dict) else []
                    pdata["keys"] = {
                        k: _mask_secret(v) for k, v in raw_keys.items()
                    } if isinstance(raw_keys, dict) else {}
                    # 注入双支柱能力与模型池清单
                    preset = self._presets.get(pid.lower().strip()) or {}
                    pdata["has_image"] = bool(pdata.get("image_models") or (preset.get("adapter_rules") or {}).get("image_generation"))
                    pdata["has_vision"] = bool(pdata.get("models") or (preset.get("adapter_rules") or {}).get("vision"))
                    pdata["vision_models"] = pdata.get("models", [])

        # 注入独立 Presets 适配器清单 (生图与识图双支柱全对称)
        safe_copy["installed_presets"] = [
            {
                "id": pid,
                "name": p.get("name", pid),
                "version": p.get("version", "1.3.0"),
                "description": p.get("description", ""),
                "has_image": bool((p.get("adapter_rules") or {}).get("image_generation") or p.get("image_models")),
                "has_vision": bool((p.get("adapter_rules") or {}).get("vision") or p.get("vision_models")),
                "image_models": [
                    (im["name"] if isinstance(im, dict) else im)
                    for im in (p.get("image_models") or [])
                ],
                "vision_models": [
                    (vm["name"] if isinstance(vm, dict) else vm)
                    for vm in (p.get("vision_models") or [])
                ],
                "rules": p.get("adapter_rules", {})
            }
            for pid, p in self._presets.items()
        ]

        return safe_copy

    def admin_token_conflicts(self) -> bool:
        """Admin Token 与任一客户端 Token 相同即视为冲突，此时禁用管理面。"""
        from security import load_client_tokens, constant_eq
        admin_token = self.get("auth", "admin_token", default="")
        return any(constant_eq(admin_token, t["token"]) for t in load_client_tokens(self))

    def verify_token(self, token: str) -> bool:
        from security import constant_eq
        admin_token = self.get("auth", "admin_token", default="")
        if not admin_token or not token:
            return False
        if self.admin_token_conflicts():
            logger.error("Admin Token 与 MCP 客户端 Token 相同，管理面已禁用。请设置独立的 ADMIN_TOKEN 环境变量后重启。")
            return False
        return constant_eq(token, admin_token)

    def set_value(self, path: tuple, value: Any) -> bool:
        curr = self._config
        for k in path[:-1]:
            curr = curr.setdefault(k, {})
        curr[path[-1]] = value
        return self.save()

    def _deep_update(self, base: dict, override: dict) -> None:
        for k, v in override.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                self._deep_update(base[k], v)
            else:
                base[k] = v


# 模块级单例
config_mgr = ConfigManager()
