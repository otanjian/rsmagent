"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from agent.permission import (
    MODES as PERMISSION_MODES,
    global_mode as permission_global_mode,
    normalize_mode as permission_normalize_mode,
)
from bridge.context import *
from collections import OrderedDict, deque
from common import const
from common import i18n
from common.log import logger
from models.reasoning_capabilities import provider_reasoning_metadata
from channel.web.core._common import _write_config_file_for_write
import json
import os
import web


def _permission_mode_projection() -> dict:
    """The global default-permission setting as the console should render it.

    A legacy install sets its own default and may edit it. In database mode the
    session permission mode is not what gates execution — the caller's role
    grants are — so the setting is shown read-only as an explanation, never as a
    knob that changes what a tenant user may run.
    """
    from channel.web.web_channel import _is_database_identity
    projection = {
        "agent_permission_mode": permission_global_mode(),
        "permission_modes": list(PERMISSION_MODES),
    }
    if _is_database_identity():
        projection["permission_mode_source"] = "role"
        projection["permission_mode_editable"] = False
    else:
        projection["permission_mode_source"] = "config"
        projection["permission_mode_editable"] = True
    return projection


class ConfigHandler:

    _RECOMMENDED_MODELS = [
        const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_V4_PRO,
        const.MINIMAX_M3, const.MINIMAX_M2_7_HIGHSPEED, const.MINIMAX_M2_7,
        # claude-opus-5 is the Claude default; claude-sonnet-5 / claude-fable-5 follow right after it.
        const.CLAUDE_OPUS_5, const.CLAUDE_SONNET_5, const.CLAUDE_FABLE_5_1, const.CLAUDE_FABLE_5, const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS,
        const.GEMINI_37_FLASH, const.GEMINI_36_FLASH, const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE,
        const.GPT_56_LUNA, const.GPT_56_TERRA, const.GPT_56_SOL, const.GPT_55, const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o,
        const.GLM_5_3_FLASH, const.GLM_5_3, const.GLM_5_2, const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7,
        const.QWEN38_FLASH, const.QWEN38_MAX, const.QWEN37_PLUS, const.QWEN37_MAX, const.QWEN36_PLUS,
        const.DOUBAO_SEED_2_1_PRO, const.DOUBAO_SEED_2_1_TURBO, const.DOUBAO_SEED_2_CODE,
        const.KIMI_K3, const.KIMI_K2_7_CODE, const.KIMI_K2_7_CODE_HIGHSPEED, const.KIMI_K2_6, const.KIMI_K2_5, const.KIMI_K2,
        const.ERNIE_5_1, const.ERNIE_5, const.ERNIE_X1_1, const.ERNIE_45_TURBO_128K, const.ERNIE_45_TURBO_32K,
        const.MIMO_V2_5_PRO, const.MIMO_V2_5,
    ]

    # Generic placeholder hints surfaced in the web console. We deliberately
    # show the version-path tail (e.g. "/v1") so users are reminded to type
    # the full base URL. The form is intentionally vague (`...../v1`) so it
    # never looks like a real default a user might paste verbatim — and we
    # never auto-rewrite anything on the server side.
    _PLACEHOLDER_V1 = "https://...../v1"
    _PLACEHOLDER_QIANFAN = "https://...../v2"
    _PLACEHOLDER_ZHIPU = "https://...../api/paas/v4"
    _PLACEHOLDER_DOUBAO = "https://...../api/v3"
    _PLACEHOLDER_GEMINI = "https://....."

    PROVIDER_MODELS = OrderedDict([
        ("deepseek", {
            "label": "DeepSeek",
            "api_key_field": "deepseek_api_key",
            "api_base_key": "deepseek_api_base",
            "api_base_default": "https://api.deepseek.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_V4_PRO],
        }),
        ("claudeAPI", {
            "label": "Claude",
            "api_key_field": "claude_api_key",
            "api_base_key": "claude_api_base",
            "api_base_default": "https://api.anthropic.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.CLAUDE_OPUS_5, const.CLAUDE_SONNET_5, const.CLAUDE_FABLE_5_1, const.CLAUDE_FABLE_5, const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS],
        }),
        ("openai", {
            "label": "OpenAI",
            "api_key_field": "open_ai_api_key",
            "api_base_key": "open_ai_api_base",
            "api_base_default": "https://api.openai.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.GPT_56_LUNA, const.GPT_56_TERRA, const.GPT_56_SOL, const.GPT_55, const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o],
        }),
        ("gemini", {
            "label": "Gemini",
            "api_key_field": "gemini_api_key",
            "api_base_key": "gemini_api_base",
            "api_base_default": "https://generativelanguage.googleapis.com",
            "api_base_placeholder": _PLACEHOLDER_GEMINI,
            "models": [const.GEMINI_37_FLASH, const.GEMINI_36_FLASH, const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE],
        }),
        ("minimax", {
            "label": "MiniMax",
            "api_key_field": "minimax_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.MINIMAX_M3, const.MINIMAX_M2_7, const.MINIMAX_M2_7_HIGHSPEED],
        }),
        ("zhipu", {
            "label": {"zh": "智谱AI", "en": "GLM"},
            "api_key_field": "zhipu_ai_api_key",
            "api_base_key": "zhipu_ai_api_base",
            "api_base_default": "https://open.bigmodel.cn/api/paas/v4",
            "api_base_placeholder": _PLACEHOLDER_ZHIPU,
            "models": [const.GLM_5_3_FLASH, const.GLM_5_3, const.GLM_5_2, const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7],
        }),
        ("dashscope", {
            "label": {"zh": "通义千问", "en": "Qwen"},
            "api_key_field": "dashscope_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.QWEN38_FLASH, const.QWEN38_MAX, const.QWEN37_PLUS, const.QWEN37_MAX, const.QWEN36_PLUS],
        }),
        ("moonshot", {
            "label": "Kimi",
            "api_key_field": "moonshot_api_key",
            "api_base_key": "moonshot_base_url",
            "api_base_default": "https://api.moonshot.cn/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.KIMI_K3, const.KIMI_K2_7_CODE, const.KIMI_K2_7_CODE_HIGHSPEED, const.KIMI_K2_6, const.KIMI_K2_5, const.KIMI_K2],
        }),
        ("doubao", {
            "label": {"zh": "豆包", "en": "Doubao"},
            "api_key_field": "ark_api_key",
            "api_base_key": "ark_base_url",
            "api_base_default": "https://ark.cn-beijing.volces.com/api/v3",
            "api_base_placeholder": _PLACEHOLDER_DOUBAO,
            "models": [const.DOUBAO_SEED_2_1_PRO, const.DOUBAO_SEED_2_1_TURBO, const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_2_CODE],
        }),
        ("qianfan", {
            "label": {"zh": "百度千帆", "en": "ERNIE"},
            "api_key_field": "qianfan_api_key",
            "api_base_key": "qianfan_api_base",
            "api_base_default": "https://qianfan.baidubce.com/v2",
            "api_base_placeholder": _PLACEHOLDER_QIANFAN,
            "models": [const.ERNIE_5_1, const.ERNIE_5, const.ERNIE_X1_1, const.ERNIE_45_TURBO_128K, const.ERNIE_45_TURBO_32K],
        }),
        ("mimo", {
            "label": {"zh": "小米 MiMo", "en": "MiMo"},
            "api_key_field": "mimo_api_key",
            "api_base_key": "mimo_api_base",
            "api_base_default": "https://api.xiaomimimo.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.MIMO_V2_5_PRO, const.MIMO_V2_5],
        }),
        ("linkai", {
            "label": "LinkAI",
            "api_key_field": "linkai_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": _RECOMMENDED_MODELS,
        }),
        ("custom", {
            "label": {"zh": "自定义", "en": "Custom"},
            "api_key_field": "custom_api_key",
            "api_base_key": "custom_api_base",
            "api_base_default": "",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [],
        }),
    ])

    EDITABLE_KEYS = {
        "cow_lang",
        "model", "bot_type", "use_linkai",
        "open_ai_api_base", "deepseek_api_base", "qianfan_api_base", "claude_api_base", "gemini_api_base",
        "zhipu_ai_api_base", "moonshot_base_url", "ark_base_url", "custom_api_base", "mimo_api_base",
        "open_ai_api_key", "deepseek_api_key", "qianfan_api_key", "claude_api_key", "gemini_api_key",
        "zhipu_ai_api_key", "dashscope_api_key", "moonshot_api_key",
        "ark_api_key", "minimax_api_key", "linkai_api_key", "custom_api_key", "mimo_api_key",
        "custom_providers",
        "agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps",
        "enable_thinking", "reasoning_effort", "reasoning_effort_by_model", "self_evolution_enabled",
        "agent_permission_mode",
    }

    # Switches the API exposes flat - one key, one control - while the config
    # file keeps a feature's settings together under one object.
    NESTED_BOOLS = {
        "subagent_enabled": ("subagent", "enabled"),
    }

    @staticmethod
    def _mask_key(value: str) -> str:
        """Mask the middle part of an API key for display."""
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    def GET(self):
        from channel.web.fork.handlers.models import ModelsHandler
        from channel.web.fork.handlers.config import _permission_mode_projection
        from channel.web.fork.handlers.workspace import _project_brand_name
        from channel.web.web_channel import _require_platform_console
        from channel.web.web_channel import conf
        _require_platform_console()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.subagent import SubagentSettings
            from agent.evolution.config import get_evolution_config

            local_config = conf()
            use_agent = local_config.get("agent", True)
            title = _project_brand_name()

            api_bases = {}
            api_keys_masked = {}
            for pid, pinfo in self.PROVIDER_MODELS.items():
                base_key = pinfo.get("api_base_key")
                if base_key:
                    api_bases[base_key] = local_config.get(base_key, pinfo["api_base_default"])
                key_field = pinfo.get("api_key_field")
                if key_field and key_field not in api_keys_masked:
                    raw = local_config.get(key_field, "")
                    api_keys_masked[key_field] = self._mask_key(raw) if raw else ""

            providers = {}
            provider_model = local_config.get("model", "")
            for pid, p in self.PROVIDER_MODELS.items():
                reasoning_by_model = {
                    model: provider_reasoning_metadata(pid, model)
                    for model in p["models"]
                }
                providers[pid] = {
                    "label": p["label"],
                    "models": p["models"],
                    "api_base_key": p["api_base_key"],
                    "api_base_default": p["api_base_default"],
                    "api_base_placeholder": p.get("api_base_placeholder", ""),
                    "api_key_field": p.get("api_key_field"),
                    "reasoning": provider_reasoning_metadata(pid, provider_model),
                    "reasoning_by_model": reasoning_by_model,
                }

            # Expose user-defined custom providers as "custom:<id>" entries so
            # the legacy config page can display and select them. Credentials
            # are managed on the Models page, hence the null key/base fields.
            # Mirrors the Models page: when expanded entries exist, the bare
            # legacy "custom" entry is hidden — unless the flat single-provider
            # custom config is still active or filled in.
            try:
                from models.custom_provider import get_custom_providers
                custom_list = get_custom_providers()
                legacy_custom_in_use = ModelsHandler._legacy_custom_in_use(local_config)
                if custom_list and not legacy_custom_in_use:
                    providers.pop("custom", None)
                for cp in custom_list:
                    cid = f"custom:{cp.get('id')}"
                    cname = cp.get("name") or cp.get("id")
                    providers[cid] = {
                        "label": {"zh": cname, "en": cname},
                        "models": [cp["model"]] if cp.get("model") else [],
                        "api_base_key": None,
                        "api_base_default": None,
                        "api_base_placeholder": "",
                        "api_key_field": None,
                        "reasoning": provider_reasoning_metadata(cid, cp.get("model") or ""),
                        "reasoning_by_model": (
                            {cp["model"]: provider_reasoning_metadata(cid, cp["model"])}
                            if cp.get("model") else {}
                        ),
                    }
            except Exception as cp_err:
                logger.warning(f"[ConfigHandler] failed to expand custom providers: {cp_err}")

            result = {
                "status": "success",
                "use_agent": use_agent,
                "title": title,
                "model": local_config.get("model", ""),
                "bot_type": "openai" if local_config.get("bot_type") == "chatGPT" else local_config.get("bot_type", ""),
                "use_linkai": bool(local_config.get("use_linkai", False)),
                "channel_type": local_config.get("channel_type", ""),
                # Manual cap on the input budget (compact once reached); 0
                # disables the cap and follows the model window. The fallback has
                # to match config.py's shipped default, or a config that predates
                # the key reports a stale budget to the console.
                "agent_max_context_tokens": local_config.get("agent_max_context_tokens", 64000),
                "agent_max_context_turns": local_config.get("agent_max_context_turns", 20),
                "agent_max_steps": local_config.get("agent_max_steps", 20),
                "enable_thinking": bool(local_config.get("enable_thinking", False)),
                "reasoning_effort": local_config.get("reasoning_effort", "high"),
                "reasoning_effort_by_model": local_config.get("reasoning_effort_by_model", {}),
                # Read through the feature's own loader so the default it
                # applies to an absent setting is the one shown here.
                "self_evolution_enabled": get_evolution_config().enabled,
                "subagent_enabled": SubagentSettings.from_config().enabled,
                # Default permission mode for sessions that have not pinned one.
                # In database mode this is read-only (roles own execution).
                **_permission_mode_projection(),
                "api_bases": api_bases,
                "api_keys": api_keys_masked,
                "providers": providers,
            }
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        from channel.web.web_channel import _is_database_identity
        from channel.web.web_channel import _read_config_file_for_write
        from channel.web.web_channel import _require_platform_console
        from channel.web.web_channel import conf
        from channel.web.web_channel import get_data_root
        _require_platform_console()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data())
            updates = data.get("updates", {})
            if not updates:
                return json.dumps({"status": "error", "message": "no updates provided"})

            local_config = conf()
            applied = {}
            nested = {}
            for key, value in updates.items():
                if key in self.NESTED_BOOLS:
                    section, leaf = self.NESTED_BOOLS[key]
                    nested.setdefault(section, {})[leaf] = bool(value)
                    continue
                if key not in self.EDITABLE_KEYS:
                    continue
                if key == "agent_permission_mode" and _is_database_identity():
                    # database mode gates execution on role grants, not this
                    # setting; refuse to persist a change that has no effect.
                    continue
                if key in ("agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps"):
                    value = int(value)
                if key in ("use_linkai", "enable_thinking", "self_evolution_enabled"):
                    value = bool(value)
                # Never persist an unknown mode: every later read would silently
                # fall back and the UI would show a setting that does nothing.
                if key == "agent_permission_mode":
                    value = permission_normalize_mode(value)
                # reasoning_effort_by_model is a dict that must be *merged* with
                # the persisted map, not replaced. A frontend submits only the
                # entries it changed (merged locally), so whole-key replacement
                # here would drop other models' saved efforts on a concurrent or
                # sequential save (or a second open settings page).
                if key == "reasoning_effort_by_model":
                    if not isinstance(value, dict):
                        # Reject malformed payloads explicitly instead of
                        # persisting a non-dict that the resolver would choke on.
                        return json.dumps({
                            "status": "error",
                            "message": "reasoning_effort_by_model must be a JSON object",
                        })
                    merged = dict(local_config.get("reasoning_effort_by_model") or {})
                    merged.update(value)
                    value = merged
                local_config[key] = value
                applied[key] = value

            if not applied and not nested:
                return json.dumps({"status": "error", "message": "no valid keys to update"})

            config_path = os.path.join(get_data_root(), "config.json")
            file_cfg = _read_config_file_for_write()
            file_cfg.update(applied)
            # Merged rather than assigned: the UI sends the one switch it owns,
            # and the rest of the section is the user's to keep.
            for section, values in nested.items():
                merged = dict(file_cfg.get(section) or {})
                merged.update(values)
                file_cfg[section] = merged
                local_config[section] = merged
                applied[section] = merged
            _write_config_file_for_write(config_path, file_cfg)

            logger.info(f"[WebChannel] Config updated: {list(applied.keys())}")

            # Apply a language change immediately so backend logs, agent
            # replies and CLI output switch without a restart.
            if "cow_lang" in applied:
                try:
                    i18n.resolve_language(applied["cow_lang"])
                    logger.info(f"[WebChannel] Language switched to: {i18n.get_language()}")
                except Exception as lang_err:
                    logger.warning(f"[WebChannel] Failed to apply language: {lang_err}")

            # Reset Bridge so that bot routing reflects the new config.
            # Without this, Bridge keeps its cached bot instance (e.g. LinkAIBot)
            # even after the user switches bot_type / use_linkai / model in UI.
            bridge_routing_keys = {"bot_type", "use_linkai", "model"}
            if any(k in applied for k in bridge_routing_keys):
                try:
                    from bridge.bridge import Bridge
                    Bridge().reset_bot()
                    logger.info("[WebChannel] Bridge bot routing reset due to config change")
                except Exception as reset_err:
                    logger.warning(f"[WebChannel] Failed to reset bridge: {reset_err}")

            # Evict cached agent runtimes when a config baked into the Agent at
            # construction time changes. These values (context budget, turn/step
            # caps) are read once in agent_initializer and cached on the Agent
            # instance, so without eviction an edit only takes effect after a
            # restart — the user changes the budget in the UI and sees no change
            # (the context-usage chart keeps the old limit). Clearing the
            # instances makes the next turn rebuild each agent from the new
            # config, no restart needed.
            agent_rebuild_keys = {
                "agent_max_context_tokens",
                "agent_max_context_turns",
                "agent_max_steps",
            }
            if any(k in applied for k in agent_rebuild_keys):
                try:
                    from bridge.bridge import Bridge
                    agent_bridge = Bridge().get_agent_bridge()
                    if agent_bridge is not None:
                        agent_bridge.clear_all_sessions()
                        logger.info(
                            "[WebChannel] Cleared cached agents so new "
                            f"{sorted(agent_rebuild_keys & applied.keys())} takes effect"
                        )
                except Exception as rebuild_err:
                    logger.warning(f"[WebChannel] Failed to clear agents: {rebuild_err}")

            return json.dumps({"status": "success", "applied": applied}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return json.dumps({"status": "error", "message": str(e)})


