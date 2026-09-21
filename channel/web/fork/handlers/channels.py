"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from auth.object_scope import MANAGE as SCOPE_MANAGE, USE as SCOPE_USE, ObjectScope
from bridge.context import *
from channel.web.admin_handlers import (
    PlatformUsersHandler,
    PlatformUserPasswordHandler,
    PlatformUserExternalIdentitiesHandler,
    PlatformUserExternalIdentityHandler,
    PlatformTenantsHandler,
    PlatformTenantHandler,
    PlatformTenantAdminsHandler,
    PlatformTenantAgentsHandler,
    TenantInfoHandler,
    TenantMembersHandler,
    TenantMemberHandler,
    TenantMemberExternalIdentitiesHandler,
    TenantMemberExternalIdentityHandler,
    ExternalIdentityAttemptsHandler,
    PlatformExternalIdentityAttemptsHandler,
    TenantRolesHandler,
    TenantRoleHandler,
    TenantPermissionsHandler,
    TenantDepartmentsHandler,
    TenantDepartmentHandler,
    IdentityAuditHandler,
    IdentityAdministeredTenantsHandler,
    PlatformTenantRolesHandler,
    PlatformTenantRoleHandler,
    TenantAuthorizationCatalogHandler,
    PlatformTenantAuthorizationCatalogHandler,
    PlatformTenantResourcesHandler,
    TenantChannelsHandler,
    TenantChannelHandler,
    TenantChannelActiveHandler,
    _int_or_zero,
)
from collections import OrderedDict, deque
from common import i18n
from common.log import logger
from typing import Any, Dict, List, Tuple, Optional, Iterator, NoReturn
import base64
import json
import os
import secrets
import sys
import threading
import time
import web


def _live_channel_manager():
    """Return the running ChannelManager, or None before the app is up.

    Resolved through ``common.channel_registry`` (upstream's contract, issue
    #3120): the entry module's private global is not a reliable cell because
    ``python app.py`` makes ``__main__`` a different module object from a later
    ``import app``, so that lookup always yielded None and the console silently
    refused to start a newly configured channel.
    """
    from channel.web.core._common import _live_channel_manager as resolve
    return resolve()


class ChannelsHandler:
    """API for managing external channel configurations (feishu, dingtalk, etc).

    This is the *instance-level* (global) configuration, owned by the platform
    admin control plane. In database mode ``_require_platform_console`` resolves
    the session and rejects a non-platform-admin: the HTTP-method policy
    processor classifies the route ``platform`` but deliberately does not
    duplicate handler authorization, so without this guard opening the route
    would let any authenticated member read or repoint the global channel
    credentials. A tenant admin configures its own channels through
    ``/api/tenant/channels`` instead.
    """

    CHANNEL_DEFS = OrderedDict([
        ("weixin", {
            "label": {"zh": "微信", "en": "WeChat"},
            "icon": "fa-comment",
            "color": "emerald",
            "fields": [],
        }),
        ("feishu", {
            "label": {"zh": "飞书", "en": "Feishu"},
            "icon": "fa-paper-plane",
            "color": "blue",
            "fields": [
                {"key": "feishu_app_id", "label": "App ID", "type": "text"},
                {"key": "feishu_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("dingtalk", {
            "label": {"zh": "钉钉", "en": "DingTalk"},
            "icon": "fa-comments",
            "color": "blue",
            "fields": [
                {"key": "dingtalk_client_id", "label": "Client ID", "type": "text"},
                {"key": "dingtalk_client_secret", "label": "Client Secret", "type": "secret"},
            ],
        }),
        ("wecom_bot", {
            "label": {"zh": "企微智能机器人", "en": "WeCom Bot"},
            "icon": "fa-robot",
            "color": "emerald",
            "fields": [
                {"key": "wecom_bot_id", "label": "Bot ID", "type": "text"},
                {"key": "wecom_bot_secret", "label": "Secret", "type": "secret"},
            ],
        }),
        ("qq", {
            "label": {"zh": "QQ 机器人", "en": "QQ Bot"},
            "icon": "fa-comment",
            "color": "blue",
            "fields": [
                {"key": "qq_app_id", "label": "App ID", "type": "text"},
                {"key": "qq_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("wechatcom_app", {
            "label": {"zh": "企微自建应用", "en": "WeCom App"},
            "icon": "fa-building",
            "color": "emerald",
            "fields": [
                {"key": "wechatcom_corp_id", "label": "Corp ID", "type": "text"},
                {"key": "wechatcomapp_agent_id", "label": "Agent ID", "type": "text"},
                {"key": "wechatcomapp_secret", "label": "Secret", "type": "secret"},
                {"key": "wechatcomapp_token", "label": "Token", "type": "secret"},
                {"key": "wechatcomapp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatcomapp_port", "label": "Port", "type": "number", "default": 9898},
            ],
        }),
        ("wechat_kf", {
            "label": {"zh": "微信客服", "en": "WeChat Customer Service"},
            "icon": "fa-headset",
            "color": "emerald",
            "fields": [
                {"key": "wechat_kf_corp_id", "label": "Corp ID", "type": "text"},
                {"key": "wechat_kf_secret", "label": "Secret", "type": "secret"},
                {"key": "wechat_kf_token", "label": "Token", "type": "secret"},
                {"key": "wechat_kf_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechat_kf_port", "label": "Port", "type": "number", "default": 9888},
            ],
        }),
        ("wechatmp", {
            "label": {"zh": "公众号", "en": "WeChat MP"},
            "icon": "fa-comment-dots",
            "color": "emerald",
            "fields": [
                {"key": "wechatmp_app_id", "label": "App ID", "type": "text"},
                {"key": "wechatmp_app_secret", "label": "App Secret", "type": "secret"},
                {"key": "wechatmp_token", "label": "Token", "type": "secret"},
                {"key": "wechatmp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatmp_port", "label": "Port", "type": "number", "default": 8080},
            ],
        }),
        ("telegram", {
            "label": {"zh": "Telegram", "en": "Telegram"},
            "icon": "fa-paper-plane",
            "color": "sky",
            "fields": [
                {"key": "telegram_token", "label": "Bot Token", "type": "secret"},
            ],
        }),
        ("slack", {
            "label": {"zh": "Slack", "en": "Slack"},
            "icon": "fa-hashtag",
            "color": "purple",
            "fields": [
                {"key": "slack_bot_token", "label": "Bot Token (xoxb-)", "type": "secret"},
                {"key": "slack_app_token", "label": "App Token (xapp-)", "type": "secret"},
            ],
        }),
        ("discord", {
            "label": {"zh": "Discord", "en": "Discord"},
            "icon": "fa-discord",
            "color": "indigo",
            "fields": [
                {"key": "discord_token", "label": "Bot Token", "type": "secret"},
            ],
        }),
    ])

    # Channels that lead the list in English. Everything defined above them
    # needs a mainland-China account, so an English user scrolling past those
    # to reach Telegram is scrolling past options they cannot use.
    EN_FIRST_CHANNELS = ("telegram", "discord", "slack")

    @classmethod
    def _ordered_channel_defs(cls, lang=None):
        """
        Channel definitions ordered for `lang`, defaulting to the configured UI
        language. Callers pass the language of the interface they are drawing:
        the desktop client keeps its own language in localStorage, so the global
        setting is not always what the user is looking at.
        """
        from common import i18n
        if (lang or i18n.get_language()) != i18n.EN:
            return list(cls.CHANNEL_DEFS.items())
        lead = [(k, cls.CHANNEL_DEFS[k]) for k in cls.EN_FIRST_CHANNELS if k in cls.CHANNEL_DEFS]
        rest = [(k, v) for k, v in cls.CHANNEL_DEFS.items() if k not in cls.EN_FIRST_CHANNELS]
        return lead + rest

    @staticmethod
    def _get_weixin_login_status() -> str:
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = _live_channel_manager()
            if mgr:
                ch = mgr.get_channel("weixin")
                if ch and hasattr(ch, 'login_status'):
                    return ch.login_status
        except Exception:
            pass
        return "unknown"

    @staticmethod
    def _mask_secret(value: str) -> str:
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    @staticmethod
    def _parse_channel_list(raw) -> list:
        if isinstance(raw, list):
            return [ch.strip() for ch in raw if ch.strip()]
        if isinstance(raw, str):
            return [ch.strip() for ch in raw.split(",") if ch.strip()]
        return []

    @classmethod
    def _active_channel_set(cls) -> set:
        from channel.web.web_channel import conf
        return set(cls._parse_channel_list(conf().get("channel_type", "")))

    @staticmethod
    def _multi_agent_mode() -> bool:
        """True once the install has crossed into multi-Agent territory.

        The team.json file only exists after a second Agent (or channel
        instance) is created; until then everything lives in config.json and the
        channels view stays single-instance, exactly as a legacy install expects.
        """
        from channel.web.web_channel import conf
        from agent import team
        return team.team_file(conf()).exists()

    @classmethod
    def _channel_instances_view(cls) -> list:
        """Per-instance channel cards for every multi-instance-ready type.

        Expands ``channel_instances`` into one card each, carrying instance_id,
        the bound agent_id and masked credentials, so the console can show and
        edit each bot independently. Covers all MULTI_INSTANCE_READY types
        (feishu, dingtalk, qq, telegram, slack, discord), not just feishu.
        """
        from channel.web.web_channel import conf
        from common import i18n
        from channel.channel_instances import (
            resolve_channel_instances,
            MULTI_INSTANCE_READY,
        )
        from agent import team

        settings = team.resolve(conf())
        local_config = conf()
        is_hant = i18n.get_language() == i18n.ZH_HANT
        out = []
        for inst in resolve_channel_instances(settings):
            if inst.channel_type not in MULTI_INSTANCE_READY:
                continue
            ch_def = cls.CHANNEL_DEFS.get(inst.channel_type)
            if not ch_def:
                continue
            fields_out = []
            for f in ch_def["fields"]:
                raw_val = (inst.credentials or {}).get(f["key"], "")
                # Mirror runtime credential resolution (channel.cfg): when an
                # instance record is missing a value, the channel falls back to
                # the global config.json. Show the same value so a credential
                # the bot actually uses never renders as a blank field (e.g. a
                # secret that lives only in the global config still shows masked).
                if raw_val in (None, ""):
                    raw_val = local_config.get(f["key"], f.get("default", ""))
                if f["type"] == "secret" and raw_val:
                    display_val = cls._mask_secret(str(raw_val))
                else:
                    display_val = raw_val
                label_val = f["label"]
                if is_hant and isinstance(label_val, str):
                    label_val = i18n.to_traditional(label_val)
                elif is_hant and isinstance(label_val, dict):
                    label_val = label_val.copy()
                    label_val["zh-Hant"] = i18n.to_traditional(label_val.get("zh", ""))
                fields_out.append({
                    "key": f["key"],
                    "label": label_val,
                    "type": f["type"],
                    "value": display_val,
                    "default": f.get("default", ""),
                })
            label_val = ch_def["label"]
            if is_hant and isinstance(label_val, str):
                label_val = i18n.to_traditional(label_val)
            elif is_hant and isinstance(label_val, dict):
                label_val = label_val.copy()
                label_val["zh-Hant"] = i18n.to_traditional(label_val.get("zh", ""))
            out.append({
                "name": inst.channel_type,
                "instance_id": inst.instance_id,
                "channel_type": inst.channel_type,
                "agent_id": inst.agent_id or "",
                # User-editable label; empty falls back client-side to the id.
                "instance_name": inst.name or "",
                "members": list(inst.members or []),
                "label": label_val,
                "icon": ch_def["icon"],
                "color": ch_def["color"],
                "active": True,
                "fields": fields_out,
            })
        return out

    def GET(self):
        from channel.web.web_channel import _require_platform_console
        from channel.web.web_channel import conf
        _require_platform_console()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common import i18n
            local_config = conf()
            active_channels = self._active_channel_set()
            channels = []
            is_hant = i18n.get_language() == i18n.ZH_HANT
            # The caller may be rendering in a different language than the
            # global setting; honour it when it sends one.
            req_lang = web.input().get("lang") or None
            if req_lang not in (i18n.EN, i18n.ZH, i18n.ZH_HANT):
                req_lang = None
            for ch_name, ch_def in self._ordered_channel_defs(req_lang):
                fields_out = []
                for f in ch_def["fields"]:
                    raw_val = local_config.get(f["key"], f.get("default", ""))
                    if f["type"] == "secret" and raw_val:
                        display_val = self._mask_secret(str(raw_val))
                    else:
                        display_val = raw_val
                    
                    label_val = f["label"]
                    if is_hant and isinstance(label_val, str):
                        label_val = i18n.to_traditional(label_val)
                    elif is_hant and isinstance(label_val, dict):
                        label_val = label_val.copy()
                        label_val["zh-Hant"] = i18n.to_traditional(label_val.get("zh", ""))

                    fields_out.append({
                        "key": f["key"],
                        "label": label_val,
                        "type": f["type"],
                        "value": display_val,
                        "default": f.get("default", ""),
                    })
                
                label_val = ch_def["label"]
                if is_hant and isinstance(label_val, str):
                    label_val = i18n.to_traditional(label_val)
                elif is_hant and isinstance(label_val, dict):
                    label_val = label_val.copy()
                    label_val["zh-Hant"] = i18n.to_traditional(label_val.get("zh", ""))

                ch_info = {
                    "name": ch_name,
                    "label": label_val,
                    "icon": ch_def["icon"],
                    "color": ch_def["color"],
                    "active": ch_name in active_channels,
                    "fields": fields_out,
                }
                if ch_name == "weixin" and ch_name in active_channels:
                    ch_info["login_status"] = self._get_weixin_login_status()
                channels.append(ch_info)

            from channel.channel_instances import MULTI_INSTANCE_READY
            multi_agent = self._multi_agent_mode()
            payload = {
                "status": "success",
                "channels": channels,
                "multi_agent": multi_agent,
                "multi_instance_types": sorted(MULTI_INSTANCE_READY),
            }
            # In multi-Agent mode the multi-instance-ready types (feishu) render
            # one card per channel_instances record instead of one per type.
            if multi_agent:
                payload["instances"] = self._channel_instances_view()
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Channels API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        from channel.web.web_channel import _require_platform_console
        _require_platform_console()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action")
            channel_name = body.get("channel")

            if not action or not channel_name:
                return json.dumps({"status": "error", "message": "action and channel required"})

            if channel_name not in self.CHANNEL_DEFS:
                return json.dumps({"status": "error", "message": f"unknown channel: {channel_name}"})

            # Multi-Agent + a multi-instance-ready type (feishu) manages each bot
            # as its own channel_instances record in team.json rather than the
            # legacy flat config.json path. instance_id empty on connect means
            # "create a new instance".
            from channel.channel_instances import MULTI_INSTANCE_READY
            instance_id = (body.get("instance_id") or "").strip()
            # A multi-instance-ready type (feishu) is only an *instance* when it
            # carries an instance_id (connect with an empty id creates one). But
            # the same type can still be active the legacy way — enabled in
            # config.json's channel_type before this install went multi-Agent —
            # in which case its card has no instance_id. Disconnect/rename on
            # such a card must fall through to the legacy per-type path, or it
            # would be rejected ("instance_id is required") and never removed.
            is_instance_op = action in ("save", "connect") or bool(instance_id)
            if self._multi_agent_mode() and channel_name in MULTI_INSTANCE_READY and is_instance_op:
                if action == "save":
                    return self._handle_instance_save(channel_name, instance_id, body.get("config", {}))
                elif action == "connect":
                    return self._handle_instance_connect(channel_name, instance_id, body.get("config", {}))
                elif action == "disconnect":
                    return self._handle_instance_disconnect(channel_name, instance_id)
                elif action == "rename":
                    return self._handle_instance_rename(channel_name, instance_id, body.get("name", ""))
                else:
                    return json.dumps({"status": "error", "message": f"unknown action: {action}"})

            if action == "save":
                return self._handle_save(channel_name, body.get("config", {}))
            elif action == "connect":
                return self._handle_connect(channel_name, body.get("config", {}))
            elif action == "disconnect":
                return self._handle_disconnect(channel_name)
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] Channels POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _handle_save(self, channel_name: str, updates: dict):
        from channel.web.web_channel import _read_config_file_for_write
        from channel.web.web_channel import conf
        from channel.web.web_channel import get_data_root
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        local_config = conf()
        applied = {}
        # Track which applied keys actually changed value, so a save that leaves
        # every credential untouched (e.g. the user re-saved the form, or only
        # an unrelated setting moved) does not needlessly tear down and
        # reconnect a live channel.
        changed = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            if local_config.get(key) != value:
                changed[key] = value
            local_config[key] = value
            applied[key] = value

        if not applied:
            return json.dumps({"status": "error", "message": "no valid fields to update"})

        config_path = os.path.join(get_data_root(), "config.json")
        file_cfg = _read_config_file_for_write()
        file_cfg.update(applied)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(
            f"[WebChannel] Channel '{channel_name}' config saved: {list(applied.keys())}, "
            f"changed: {list(changed.keys())}"
        )

        # Only a real change to this channel's config warrants a restart. An
        # idempotent save must not interrupt a connected channel.
        should_restart = False
        active_channels = self._active_channel_set()
        if channel_name in active_channels and changed:
            should_restart = True
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = _live_channel_manager()
                if mgr:
                    threading.Thread(
                        target=mgr.restart,
                        args=(channel_name,),
                        daemon=True,
                    ).start()
                    logger.info(f"[WebChannel] Channel '{channel_name}' restart triggered")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to restart channel '{channel_name}': {e}")

        return json.dumps({
            "status": "success",
            "applied": list(applied.keys()),
            "restarted": should_restart,
        }, ensure_ascii=False)

    def _handle_connect(self, channel_name: str, updates: dict):
        """Save config fields, add channel to channel_type, and start it."""
        from channel.web.web_channel import _read_config_file_for_write
        from channel.web.web_channel import conf
        from channel.web.web_channel import get_data_root
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        # Feishu connected via web console must use websocket (long connection) mode
        if channel_name == "feishu":
            updates.setdefault("feishu_event_mode", "websocket")
            valid_keys.add("feishu_event_mode")

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        existing = self._parse_channel_list(conf().get("channel_type", ""))
        if channel_name not in existing:
            existing.append(channel_name)
        new_channel_type = ",".join(existing)
        local_config["channel_type"] = new_channel_type

        config_path = os.path.join(get_data_root(), "config.json")
        file_cfg = _read_config_file_for_write()
        file_cfg.update(applied)
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' connecting, channel_type={new_channel_type}")

        # Feishu pulls its SDK bundle on first use; tell the UI so it can warn
        # about the one-time wait rather than reporting an instant success.
        downloading = False
        if channel_name == "feishu":
            try:
                from channel.feishu import lark_install
                downloading = lark_install.needs_download()
            except Exception as e:
                logger.warning(f"[WebChannel] Could not check Feishu SDK state: {e}")

        def _do_start():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                mgr = _live_channel_manager()
                if mgr is None:
                    logger.warning(f"[WebChannel] ChannelManager not available, cannot start '{channel_name}'")
                    return
                # Stop existing instance first if still running (e.g. re-connect without disconnect)
                existing_ch = mgr.get_channel(channel_name)
                if existing_ch is not None:
                    logger.info(f"[WebChannel] Stopping existing '{channel_name}' before reconnect...")
                    mgr.stop(channel_name)
                # Always wait for the remote service to release the old connection before
                # establishing a new one (DingTalk drops callbacks on duplicate connections)
                logger.info(f"[WebChannel] Waiting for '{channel_name}' old connection to close...")
                time.sleep(5)
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Starting channel '{channel_name}'...")
                mgr.start([channel_name], first_start=False)
                logger.info(f"[WebChannel] Channel '{channel_name}' start completed")
            except Exception as e:
                logger.error(f"[WebChannel] Failed to start channel '{channel_name}': {e}",
                             exc_info=True)

        threading.Thread(target=_do_start, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
            "downloading": downloading,
        }, ensure_ascii=False)

    def _handle_disconnect(self, channel_name: str):
        from channel.web.web_channel import _read_config_file_for_write
        from channel.web.web_channel import conf
        from channel.web.web_channel import get_data_root
        existing = self._parse_channel_list(conf().get("channel_type", ""))
        existing = [ch for ch in existing if ch != channel_name]
        new_channel_type = ",".join(existing)

        local_config = conf()
        local_config["channel_type"] = new_channel_type

        config_path = os.path.join(get_data_root(), "config.json")
        file_cfg = _read_config_file_for_write()
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        def _do_stop():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = _live_channel_manager()
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                if mgr:
                    mgr.stop(channel_name)
                else:
                    logger.warning(f"[WebChannel] ChannelManager not found, cannot stop '{channel_name}'")
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Channel '{channel_name}' disconnected, "
                            f"channel_type={new_channel_type}")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to stop channel '{channel_name}': {e}",
                               exc_info=True)

        threading.Thread(target=_do_stop, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
        }, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Multi-instance channel management (team.json driven, e.g. feishu)
    # ------------------------------------------------------------------
    @staticmethod
    def _channel_mgr():
        return _live_channel_manager()

    def _clean_credentials(self, channel_name: str, updates: dict) -> dict:
        """Keep only real, unmasked credential values for this channel type."""
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}
        creds = {}
        for key, value in (updates or {}).items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                # Skip empty or still-masked secrets so a save that leaves the
                # secret untouched does not overwrite it with the mask.
                if not value or (len(str(value)) > 8 and "*" * 4 in str(value)):
                    continue
            creds[key] = value
        return creds

    def _handle_instance_connect(self, channel_name: str, instance_id: str, updates: dict):
        """Create (empty id) or reconnect a channel instance, stored in team.json."""
        from channel.web.web_channel import conf
        from channel.channel_instances import upsert_instance

        creds = self._clean_credentials(channel_name, updates)
        # Weixin scans its token during the QR flow (before the instance exists),
        # which lands in the global config. Fold it into this instance's own
        # credentials so the instance is self-contained: it stays logged in
        # across restarts and never depends on the transient global value.
        if channel_name == "weixin" and not creds.get("weixin_token"):
            token = conf().get("weixin_token", "")
            if token:
                creds["weixin_token"] = token
                base_url = conf().get("weixin_base_url", "")
                if base_url:
                    creds["weixin_base_url"] = base_url
                # Consume the transient QR token so the *next* Weixin instance
                # created (a different account) does not inherit this one's
                # token from the global config.
                conf()["weixin_token"] = ""
        inst = upsert_instance(
            conf(),
            channel_type=channel_name,
            instance_id=instance_id,
            credentials=creds,
        )

        downloading = False
        if channel_name == "feishu":
            try:
                from channel.feishu import lark_install
                downloading = lark_install.needs_download()
            except Exception as e:
                logger.warning(f"[WebChannel] Could not check Feishu SDK state: {e}")

        def _do_start():
            try:
                mgr = self._channel_mgr()
                if mgr is None:
                    logger.warning(
                        f"[WebChannel] ChannelManager unavailable, cannot start '{inst.instance_id}'"
                    )
                    return
                mgr.add_channel(inst)
                logger.info(f"[WebChannel] Channel instance '{inst.instance_id}' start completed")
            except Exception as e:
                logger.error(
                    f"[WebChannel] Failed to start channel instance '{inst.instance_id}': {e}",
                    exc_info=True,
                )

        threading.Thread(target=_do_start, daemon=True).start()
        return json.dumps({
            "status": "success",
            "instance_id": inst.instance_id,
            "downloading": downloading,
        }, ensure_ascii=False)

    def _handle_instance_save(self, channel_name: str, instance_id: str, updates: dict):
        """Update one instance's credentials in team.json and restart it."""
        from channel.web.web_channel import conf
        from channel.channel_instances import get_instance, upsert_instance

        if not instance_id:
            return json.dumps({"status": "error", "message": "instance_id is required"})
        before = get_instance(conf(), instance_id)
        creds = self._clean_credentials(channel_name, updates)
        inst = upsert_instance(
            conf(),
            channel_type=channel_name,
            instance_id=instance_id,
            credentials=creds,
        )
        # Only restart when a credential actually changed, so re-saving an
        # unchanged form does not tear down a live connection.
        changed = not before or (dict(before.credentials or {}) != dict(inst.credentials or {}))
        if changed:
            def _do_restart():
                try:
                    mgr = self._channel_mgr()
                    if mgr is None:
                        return
                    mgr.restart(inst)
                except Exception as e:
                    logger.error(
                        f"[WebChannel] Failed to restart instance '{inst.instance_id}': {e}",
                        exc_info=True,
                    )
            threading.Thread(target=_do_restart, daemon=True).start()
        logger.info(
            f"[WebChannel] Channel instance '{inst.instance_id}' saved, "
            f"restart={'yes' if changed else 'no'}"
        )
        return json.dumps({"status": "success", "instance_id": inst.instance_id}, ensure_ascii=False)

    def _handle_instance_rename(self, channel_name: str, instance_id: str, name):
        """Set an instance's friendly label. Does not touch credentials or the
        live connection, so renaming never interrupts a running channel."""
        from channel.web.web_channel import conf
        from channel.channel_instances import upsert_instance

        if not instance_id:
            return json.dumps({"status": "error", "message": "instance_id is required"})
        inst = upsert_instance(
            conf(),
            channel_type=channel_name,
            instance_id=instance_id,
            name=str(name or ""),
        )
        logger.info(f"[WebChannel] Channel instance '{inst.instance_id}' renamed to '{inst.name}'")
        return json.dumps(
            {"status": "success", "instance_id": inst.instance_id, "name": inst.name},
            ensure_ascii=False,
        )

    def _handle_instance_disconnect(self, channel_name: str, instance_id: str):
        """Remove one instance record from team.json and stop its channel."""
        from channel.web.web_channel import conf
        from channel.channel_instances import remove_instance, read_raw_instances

        if not instance_id:
            return json.dumps({"status": "error", "message": "instance_id is required"})

        # A legacy channel (enabled the old way via config.json's channel_type)
        # is folded into channel_instances on every team.json write by
        # bootstrap_legacy_instances. Just dropping the record isn't enough:
        # remove_instance itself writes team.json, whose bootstrap immediately
        # re-materializes the record straight from channel_type — so the card
        # comes right back. Prune the type from channel_type *first* (when this
        # is the last instance of it), so by the time remove_instance writes,
        # the bootstrap has nothing to recreate.
        remaining = [
            r for r in read_raw_instances(conf())
            if str(r.get("instance_id") or "").strip() != instance_id
        ]
        self._prune_legacy_channel_type(channel_name, remaining)

        remove_instance(conf(), instance_id)

        def _do_stop():
            try:
                mgr = self._channel_mgr()
                if mgr is None:
                    return
                remover = getattr(mgr, "remove_channel", None)
                if callable(remover):
                    remover(instance_id)
                else:
                    mgr.stop(instance_id)
                logger.info(f"[WebChannel] Channel instance '{instance_id}' disconnected")
            except Exception as e:
                logger.warning(
                    f"[WebChannel] Failed to stop instance '{instance_id}': {e}",
                    exc_info=True,
                )

        threading.Thread(target=_do_stop, daemon=True).start()
        return json.dumps({"status": "success", "instance_id": instance_id}, ensure_ascii=False)

    def _prune_legacy_channel_type(self, channel_name: str, remaining):
        """Drop *channel_name* from config.json's channel_type once no instance
        of that type is left (``remaining`` = the instance records that will
        survive this disconnect).

        Without this, bootstrap_legacy_instances (which runs on every team.json
        write and is keyed off channel_type) would recreate the instance we just
        removed, so the disconnect would never stick. Only prunes when the last
        instance of the type is gone, so removing one of several Feishu bots
        leaves the type — and the others — untouched.
        """
        from channel.web.web_channel import conf
        from channel.web.web_channel import _read_config_file_for_write
        from channel.web.web_channel import get_data_root
        from channel.channel_instances import _normalize_type

        target = _normalize_type(channel_name)
        if any(_normalize_type(str(r.get("channel_type") or "")) == target for r in remaining):
            return

        existing = self._parse_channel_list(conf().get("channel_type", ""))
        pruned = [ch for ch in existing if _normalize_type(ch) != target]
        if len(pruned) == len(existing):
            return
        new_channel_type = ",".join(pruned)

        conf()["channel_type"] = new_channel_type
        try:
            config_path = os.path.join(get_data_root(), "config.json")
            file_cfg = _read_config_file_for_write()
            file_cfg["channel_type"] = new_channel_type
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(file_cfg, f, indent=4, ensure_ascii=False)
            logger.info(
                f"[WebChannel] Pruned legacy channel_type '{channel_name}', "
                f"channel_type={new_channel_type}"
            )
        except Exception as e:
            logger.warning(
                f"[WebChannel] Failed to prune legacy channel_type '{channel_name}': {e}",
                exc_info=True,
            )


class WeixinQrHandler:
    """微信扫码接入：按发起者绑定会话、一次性落库（任务 7.1-7.5）。

    GET  /api/weixin/qrlogin  → 为当前已验证发起者开启一次扫码接入
    POST /api/weixin/qrlogin  → ``poll`` / ``refresh`` / ``commit`` / ``cancel``

    旧实现把二维码和"这个 token 是谁扫到的"放在进程级 ``_qr_state`` 里，并把扫到
    的长期 token 写进 ``conf()`` 和共用凭据文件：两个人同时扫码会互相覆盖，任何
    已登录身份都能轮询并接管别人的会话，实例凭据也不由实例自己持有。现在每次接入
    都是 ``channel.web.scan_onboarding`` 的会话，绑定 (user, AuthSession, tenant,
    scope, owner, provider, target, purpose)：别人的句柄与不存在的句柄得到同一个
    拒绝，长期密钥由身份服务加密写入实例自己的凭据，响应只回句柄、二维码与非敏感
    事实。进程级 ``_qr_state`` 已删除，并且没有任何回退槽位。

    四个事实分开报告、互不冒充：扫码完成（``qr_status``）、创建已授权（``authorized``）、
    实例已保存（``saved``）、外部连接（``connection`` / ``connected``）。个人渠道在
    真实执行验收之前，保存成功仍然报告"已保存未连接"（任务 7.4）。

    作用域由服务端按已验证上下文决定（``_scope_for``）：能管理本租户渠道的身份默认
    接入租户公共实例，其他成员默认接入本人实例，两类身份都可显式选择个人实例。平台
    作用域在本 change 不开放——没有服务路径会创建不属于租户的渠道实例——因此显式
    拒绝，而不是给平台管理员伪造一个租户归属。owner/tenant/目标实例一律不接受客户端
    指定。
    """

    PROVIDER = "weixin"
    CHANNEL_TYPE = "weixin"
    PURPOSE = "create"

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """Generate a QR code as a PNG data URI."""
        try:
            import qrcode as qr_lib
            import io
            import base64
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L, box_size=6, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except ImportError:
            return ""

    @staticmethod
    def _get_running_channel():
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = _live_channel_manager()
            if mgr:
                return mgr.get_channel("weixin")
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def GET(self):
        from channel.web.web_channel import _is_database_identity
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        try:
            if not _is_database_identity():
                # 单用户 legacy 部署没有 AuthSession 可绑定。那里的受支持路径是渠道
                # 自己的登录循环（启动渠道，它渲染自己的二维码），即下面的
                # ``source: "channel"`` 分支；进程级二维码槽位正是本次删掉的东西，
                # 所以这里刻意没有第二条路。
                return self._channel_owned_qr()
            from channel.web.auth_handlers import require_management_write
            require_management_write()
            params = web.input(scope="", base_url="")
            return self._start(self._context(), {
                "scope": getattr(params, "scope", ""),
                "base_url": getattr(params, "base_url", ""),
            })
        except web.HTTPError:
            raise
        except Exception as error:
            logger.error(f"[WebChannel] WeixinQr GET error: {error}", exc_info=True)
            self._fail_from(error)

    def POST(self):
        from channel.web.web_channel import _is_database_identity
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Cache-Control", "no-store")
        try:
            if not _is_database_identity():
                self._fail(
                    "database identity is required to bind a scan to an initiator",
                    status=409, code="identity_required")
            try:
                body = json.loads(web.data() or b"{}")
            except (TypeError, ValueError):
                self._fail("invalid JSON body", status=400, code="bad_request")
            if not isinstance(body, dict):
                self._fail("a JSON object is required", status=400,
                           code="bad_request")
            action = str(body.get("action") or "poll").strip().lower()
            if action not in ("poll", "refresh", "commit", "cancel"):
                self._fail(f"unknown action: {action}", status=400,
                           code="unknown_action")
            # 这里的每个动作都会推进会话或提交实例，全部是写操作，因此全部通过既有
            # 的统一来源/CSRF 门（与其他管理面写入同一个 helper）。
            from channel.web.auth_handlers import require_management_write
            require_management_write()
            ctx = self._context()
            if action == "cancel":
                return self._cancel(ctx, body)
            if action == "poll":
                return self._poll(ctx, body)
            if action == "refresh":
                # 一次刷新就是一次新的扫码：旧会话只能自行过期，永远不会被别人的
                # 扫码结果填充，也不会被这个请求改成别的绑定。
                return self._start(ctx, body)
            return self._commit(ctx, body)
        except web.HTTPError:
            raise
        except Exception as error:
            logger.error(f"[WebChannel] WeixinQr POST error: {error}", exc_info=True)
            self._fail_from(error)

    # ------------------------------------------------------------------
    # 请求上下文
    # ------------------------------------------------------------------

    @staticmethod
    def _context():
        """本次请求的已验证上下文；租户必须显式选中。

        渠道实例一律属于某个租户（公共或成员个人），所以会话绑定里的 tenant 就是
        请求选中的租户。没有选中租户的身份拿不到任何会话——这正是"归属不由客户端
        参数或全局配置推断"的落点。
        """
        from channel.web.auth_handlers import _require_context
        return _require_context(require_tenant=True)

    @staticmethod
    def _auth_session_id() -> str:
        """本次请求所属的 AuthSession **行 id**，而不是 bearer token。

        绑定需要"这次扫码是在哪次登录里发起的"，这样同一用户的下一次登录不能回读上
        一次的结果。token 是 bearer 凭据，绝不能进入会话记录、回执或日志；行 id 是
        不透明且非秘密的，正好是绑定需要的那个标识。
        """
        from channel.web.auth_handlers import _get_service, _session_token
        token = _session_token()
        session = _get_service().verify_session(token) if token else None
        # ``verify_session`` answers ``{"user": ..., "session": <store row>}``:
        # the row is a mapping but not a dict, so it is indexed, not ``.get``-ed.
        row = (session or {}).get("session") if isinstance(session, dict) else None
        try:
            ident = str(row["id"] or "") if row is not None else ""
        except Exception:  # noqa: BLE001 - an unreadable session is not a session
            ident = ""
        if not ident:
            WeixinQrHandler._fail("authentication required", status=401,
                                  code="unauthorized")
        return ident

    @classmethod
    def _actor(cls, ctx):
        from channel.web import scan_onboarding as so
        return so.Actor(user_id=ctx.user_id, tenant_id=ctx.tenant_id or "",
                        auth_session_id=cls._auth_session_id())

    # ------------------------------------------------------------------
    # 作用域与目标
    # ------------------------------------------------------------------

    @staticmethod
    def _manages_tenant_channels(ctx) -> bool:
        """*ctx* 是否可以管理本租户的**公共**渠道实例。

        问的是「管理资格」，不是「能不能列举」。这两件事在任务 6.1 之前恰好一致——
        那时列举接口对普通成员直接 403——所以拿列举当代理曾经是对的。共用之后不再
        成立：普通成员现在**也能**列举，只是列举到的是本人的连接。继续用列举做代理
        会把成员判成管理者，于是扫码给本次接入派一个 `tenant` 作用域，而这个作用域
        在写入时必然被拒（`channel instance manage denied`）——正是「先给人一个随后
        必被拒的位置」这种形状。

        因此这里直接问治理资格本身（身份投影里的租户/平台管理员标记），它是租户业务
        接口写入路径真正使用的那个事实。
        """
        if not str(getattr(ctx, "tenant_id", "") or ""):
            return False
        return bool(getattr(ctx, "is_tenant_admin", False)
                    or getattr(ctx, "is_platform_admin", False))

    @classmethod
    def _scope_for(cls, ctx, body) -> str:
        """服务端决定本次接入的 scope（客户端只能在允许范围内选择）。

        默认按能力推导：能管理本租户渠道的身份接入租户公共实例，其他成员接入本人
        实例。显式选择只是收窄/切换到自己本来就有权的位置，不能指定 owner、tenant
        或目标实例。
        """
        from channel.web import scan_onboarding as so
        wanted = str((body or {}).get("scope") or "").strip().lower()
        if wanted in ("", "auto"):
            return (so.SCOPE_TENANT if cls._manages_tenant_channels(ctx)
                    else so.SCOPE_PERSONAL)
        if wanted in ("personal", "user"):
            return so.SCOPE_PERSONAL
        if wanted == "tenant":
            if not cls._manages_tenant_channels(ctx):
                cls._fail("managing this tenant's channels is required",
                          status=403, code="forbidden")
            return so.SCOPE_TENANT
        if wanted == "platform":
            # 平台管理员在租户内的公共实例就是"租户公共实例"，而一个不属于任何租户
            # 的渠道实例没有任何服务路径可以创建。因此这里显式拒绝，而不是替平台身份
            # 伪造一个租户归属。
            cls._fail(
                "a channel instance always belongs to a tenant; select the"
                " tenant it should serve",
                status=400, code="scope_not_supported")
        cls._fail(f"unknown scan scope: {wanted}", status=400, code="bad_scope")

    @classmethod
    def _target(cls, scope: str) -> str:
        return f"{cls.CHANNEL_TYPE}:{scope}"

    # ------------------------------------------------------------------
    # 会话读写
    # ------------------------------------------------------------------

    @classmethod
    def _session(cls, actor, body):
        """调用者**自己**的会话：优先按句柄，其次按发起者。

        POST /api/weixin/qrlogin 的 ``{action: "poll"}`` 早于句柄存在（控制台与桌面
        都这样发），这里不回退到任何全局槽位，而是把"最新的会话"限定在已验证发起者
        自己的范围内——别人的会话与不存在完全一样。
        """
        from channel.web import scan_onboarding as so
        handle = str((body or {}).get("handle") or "").strip()
        if handle:
            return so.require_session(
                handle, actor=actor, provider=cls.PROVIDER, purpose=cls.PURPOSE)
        latest = so.latest_session(
            actor=actor, provider=cls.PROVIDER, purpose=cls.PURPOSE)
        if latest is None:
            # 与"句柄不存在"和"句柄是别人的"完全同一个拒绝：这一句是存在性不可观测
            # 的关键，三条路径共用同一个 code 与同一段文本。
            raise so.ScanSessionError(so.NOT_OWNED_MESSAGE, code="not_owner")
        return latest

    # ------------------------------------------------------------------
    # 开启一次接入
    # ------------------------------------------------------------------

    def _start(self, ctx, body):
        from channel.web import scan_onboarding as so
        from channel import weixin_scan_adapter as adapter
        from auth.service import get_identity_service

        ready, reason = so.shared_state_ready()
        if not ready:
            # 会话登记、回执账本与一次性授权都是进程内状态：多 worker 部署会让每个
            # worker 各自持有一份同一扫码的视图。这时必须拒绝开放，而不是让每个
            # worker 各答一半。
            self._fail(
                "this deployment cannot serve a scan safely: " + reason,
                status=503, code="state_not_shared")
        scope = self._scope_for(ctx, body)
        actor = self._actor(ctx)
        # 端点只由本次请求指定（或厂商默认），绝不读 ``conf()`` 的全局微信配置：
        # 扫码新建的实例不能因为部署里遗留的全局值被指到别处。
        base_url = (str((body or {}).get("base_url") or "").strip()
                    or adapter.default_base_url())
        # 扫码没有名字表单：这里一次性解析出默认名（类型标签 + 首个空序号），随会话绑
        # 定。同一次扫码的每次提交都提交同一份内容，幂等键与回执读回才对得上。
        default_name = adapter.default_display_name(
            get_identity_service(), scope=scope, tenant_id=ctx.tenant_id or "",
            actor_user_id=ctx.user_id)
        session = so.start_session(
            provider=self.PROVIDER, scope=scope, purpose=self.PURPOSE,
            target=self._target(scope), owner_user_id=ctx.user_id,
            tenant_id=ctx.tenant_id or "", auth_session_id=actor.auth_session_id,
            default_display_name=default_name)
        try:
            answer = adapter.fetch_qr(base_url=base_url)
        except Exception as error:
            so.mark_status(session.handle, so.STATUS_FAILED, actor=actor,
                           error=type(error).__name__)
            adapter.log_refusal("qr fetch", error)
            self._fail("the provider did not hand out a QR code", status=502,
                       code="provider_unavailable")
        qrcode = str((answer or {}).get("qrcode") or "")
        if not qrcode:
            so.mark_status(session.handle, so.STATUS_FAILED, actor=actor,
                           error="no_qrcode")
            self._fail("the provider returned no QR code", status=502,
                       code="provider_unavailable")
        session = so.attach_qr(
            session.handle, qrcode=qrcode,
            qrcode_url=str((answer or {}).get("qrcode_img_content") or ""),
            base_url=base_url, actor=actor)
        return self._scan_answer(session, qr_status="waiting")

    def _poll(self, ctx, body):
        from channel.web import scan_onboarding as so
        from channel import weixin_scan_adapter as adapter

        actor = self._actor(ctx)
        session = self._session(actor, body)
        if session.status in (so.STATUS_CANCELLED, so.STATUS_EXPIRED,
                              so.STATUS_FAILED):
            return self._scan_answer(session, qr_status=session.status)
        if session.status in (so.STATUS_CONFIRMED, so.STATUS_COMMITTING) or \
                session.grant_opened:
            # 厂商已经确认过这次扫码：之后每次轮询都是同一次提交，按回执幂等作答。
            return self._commit(ctx, body)
        if not session.qrcode:
            self._fail("this scan has no QR to poll", status=409,
                       code="no_active_scan")
        base_url = adapter.resolve_base_url(
            session_base_url=session.base_url,
            requested=str((body or {}).get("base_url") or ""))
        try:
            answer = adapter.poll_qr(qrcode=session.qrcode, base_url=base_url)
        except Exception as error:
            adapter.log_refusal("qr poll", error)
            self._fail("the provider could not be reached", status=502,
                       code="provider_unavailable")
        status = str((answer or {}).get("status")
                     or adapter.VENDOR_WAIT).strip().lower()
        if status == adapter.VENDOR_CONFIRMED:
            return self._confirm_and_commit(ctx, body, session, actor, answer)
        if status == adapter.VENDOR_EXPIRED:
            return self._reissue_qr(session, actor, base_url)
        if status == adapter.VENDOR_SCANED:
            # 厂商的"已扫待确认"不是状态机的一个状态：会话仍是 pending，直到厂商
            # 确认才进入 confirmed。控制台据此显示"已扫码"。
            return self._scan_answer(session, qr_status="scaned")
        return self._scan_answer(session, qr_status="waiting")

    def _reissue_qr(self, session, actor, base_url):
        """同一会话内重新签发一张二维码（厂商二维码过期，会话仍有效）。"""
        from channel.web import scan_onboarding as so
        from channel import weixin_scan_adapter as adapter

        try:
            answer = adapter.fetch_qr(base_url=base_url)
        except Exception as error:
            adapter.log_refusal("qr reissue", error)
            self._fail("the provider could not be reached", status=502,
                       code="provider_unavailable")
        qrcode = str((answer or {}).get("qrcode") or "")
        if not qrcode:
            self._fail("the provider returned no QR code", status=502,
                       code="provider_unavailable")
        session = so.attach_qr(
            session.handle, qrcode=qrcode,
            qrcode_url=str((answer or {}).get("qrcode_img_content") or ""),
            base_url=base_url, actor=actor)
        return self._scan_answer(session, qr_status="expired")

    def _confirm_and_commit(self, ctx, body, session, actor, answer):
        """厂商确认：收窄并加密临时结果，然后走同一个提交通道。"""
        from channel.web import scan_onboarding as so
        from channel import weixin_scan_adapter as adapter

        if not session.provider_result_present:
            # 只保留渠道类型声明的凭据字段；厂商的 ilink_bot_id / ilink_user_id 与
            # 其余应答字段既不落库也不回显。缺 token 是拒绝，不是空凭据包。
            result = adapter.provider_result(answer)
            session = so.mark_status(session.handle, so.STATUS_CONFIRMED,
                                     actor=actor)
            session = so.attach_provider_result(
                session.handle, result=result,
                sensitive_keys=adapter.provider_secret_keys(),
                declared_keys=adapter.provider_result_keys(), actor=actor)
        return self._commit(ctx, body)

    def _commit(self, ctx, body):
        from channel.web import scan_onboarding as so
        from channel import weixin_scan_adapter as adapter
        from auth.service import get_identity_service

        actor = self._actor(ctx)
        session = self._session(actor, body)
        if session.status in (so.STATUS_CANCELLED, so.STATUS_EXPIRED,
                              so.STATUS_FAILED):
            # 终态会话直接按终态拒绝。让模块在下一步回答"还没被服务商确认"会把操作
            # 者引向继续轮询一个已经结束的会话。已提交的会话不走这条路：那是回读回执
            # 的入口。
            self._fail("this scan has already finished; start a new scan",
                       status=409, code="terminal")
        if not session.provider_result_present:
            self._fail("this scan has not been confirmed by the provider yet",
                       status=409, code="not_confirmed")
        credentials = so.provider_result(session.handle, actor=actor)
        # 授权在提交前才"开启"，且同一会话只开启一次：重试与响应丢失回读用的因此
        # 是同一个授权句柄、同一个幂等键，而不是第二次创建。私有创建要在授权里写明
        # 这一次买的是哪个 Agent（task 4.1）：授权因此不能跨对象复用。目标一旦定下就
        # 成为会话状态，只带句柄的轮询（响应丢失后的回读）仍然呈现同一个目标，而不是
        # 被当成"另一次内容不同的创建"。
        wanted_agent = (str((body or {}).get("agent_id") or "").strip()
                        or session.grant_agent_id)
        ticket = so.open_grant(session.handle, actor=actor,
                               channel_type=self.CHANNEL_TYPE,
                               agent_id=wanted_agent)
        # 开户动作让会话多了"已授权"这个事实，重读一次视图，别拿开启前的旧快照回答。
        session = so.get_session(session.handle, actor=actor)
        service = get_identity_service()
        result = so.commit_scan_binding(
            handle=session.handle, actor=actor, scan_ticket=ticket,
            channel_type=self.CHANNEL_TYPE,
            create_instance=adapter.create_instance_callable(
                service, scope=session.scope),
            # 客户端可以改名；没改名就用扫码开始时绑定的默认名（类型标签 + 空序号）。
            # 两者都随会话固定，重试提交的内容不会漂移。
            display_name=(str((body or {}).get("display_name") or "").strip()
                          or session.default_display_name),
            agent_id=wanted_agent,
            credentials=credentials,
            provider=self.PROVIDER, scope=session.scope, purpose=self.PURPOSE,
            target=session.target,
            # 配额由身份服务自己的 BEGIN IMMEDIATE 事务执行（那里才有实例名额），
            # 这里不预留另一个指标；审计走已交付的审计 API。
            reserve_quota=None, quota_required=False,
            record_audit=adapter.audit_hook(service),
            readback_guard=adapter.readback_guard(
                service, resolve_context=self._context),
        )
        instance_id = str(result.get("instance_id") or "")
        if (instance_id and session.scope == so.SCOPE_TENANT
                and result.get("outcome") in ("committed", "resumed")):
            # 提交后按实例去重、可恢复的连接工作项。成员个人实例由已交付的个人创建
            # 路径负责 reconcile，所以这里不重复施加；回读（replayed）也不重启连接。
            adapter.apply_connection(instance_id)
        return self._committed_answer(session, result, service)

    def _cancel(self, ctx, body):
        from channel.web import scan_onboarding as so

        actor = self._actor(ctx)
        session = self._session(actor, body)
        if session.terminal:
            return self._scan_answer(session, qr_status=session.status)
        session = so.mark_status(session.handle, so.STATUS_CANCELLED, actor=actor)
        return self._scan_answer(session, qr_status=session.status)

    # ------------------------------------------------------------------
    # 应答
    # ------------------------------------------------------------------

    def _channel_owned_qr(self):
        """legacy 单用户部署：观测渠道自己正在展示的二维码。"""
        running_ch = self._get_running_channel()
        qr_url = getattr(running_ch, "_current_qr_url", "") if running_ch else ""
        if qr_url:
            return json.dumps({
                "status": "success",
                "qrcode_url": qr_url,
                "qr_image": self._qr_to_data_uri(qr_url),
                "source": "channel",
            }, ensure_ascii=False)
        self._fail(
            "database identity is required to start a scan session",
            status=409, code="identity_required")

    def _scan_answer(self, session, *, qr_status: str, **extra):
        payload = dict(session.payload())
        payload.update({
            "status": "success",
            "source": "session",
            "qr_status": qr_status,
            "qr_image": (self._qr_to_data_uri(session.qrcode_url)
                         if session.qrcode_url else ""),
            "expires_in": max(0, int(session.expires_at - time.time())),
            # 四个事实分开：扫码（qr_status）/ 已授权（authorized）/ 已保存
            # （saved）/ 已连接（connection, connected）。
            "authorized": bool(session.grant_opened),
            "saved": False,
            "connected": False,
            "connection": "unsaved",
        })
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    def _committed_answer(self, session, result, service):
        from channel import weixin_scan_adapter as adapter

        instance_id = str(result.get("instance_id") or "")
        state = (adapter.connection_state(service, instance_id=instance_id)
                 if instance_id
                 else {"state": "unsaved", "connected": False, "reason": ""})
        payload = dict(session.payload())
        payload.update({
            "status": "success",
            "source": "session",
            "qr_status": "confirmed",
            "qr_image": "",
            "expires_in": max(0, int(session.expires_at - time.time())),
            "authorized": bool(session.grant_opened),
            "authorization_consumed": bool(result.get("authorization_consumed")),
            "saved": bool(result.get("saved")),
            "instance_id": instance_id,
            "channel_type": self.CHANNEL_TYPE,
            "display_name": str(result.get("display_name") or ""),
            "agent_id": str(result.get("agent_id") or ""),
            "secret_present": bool(result.get("secret_present")),
            "outcome": str(result.get("outcome") or ""),
            "receipt_state": str(result.get("receipt_state") or ""),
            "connected": bool(state.get("connected")),
            "connection": str(state.get("state") or "unknown"),
            "connection_reason": str(state.get("reason") or ""),
        })
        return json.dumps(payload, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 拒绝
    # ------------------------------------------------------------------

    @staticmethod
    def _fail(message: str, *, status: int, code: str):
        from channel.web.web_channel import _HTTP_STATUS_TEXT
        raise web.HTTPError(
            f"{status} {_HTTP_STATUS_TEXT.get(status, 'Error')}",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"status": "error", "message": message, "code": code},
                       ensure_ascii=False))

    @classmethod
    def _fail_from(cls, error):
        """把一次拒绝映射为 HTTP 应答，绝不带出凭据原文。"""
        from channel.web.web_channel import _SCAN_ERROR_STATUS
        code = str(getattr(error, "code", "") or "").strip()
        status = getattr(error, "status", None)
        if not isinstance(status, int):
            status = _SCAN_ERROR_STATUS.get(code, 400)
        if not code:
            # 没有 code 的异常是内部错误：日志里有堆栈，应答里只给一句可读的话。
            cls._fail("the scan request failed", status=status,
                      code="scan_failed")
        cls._fail(str(error), status=status, code=code)


class FeishuRegisterHandler:
    """飞书智能体应用一键创建（OAuth 设备授权流，基于 lark.register_app SDK）。

    GET  /api/feishu/register   → 为当前发起者启动一次注册，返回不透明句柄与二维码；
                                   后台线程继续轮询飞书侧直到用户扫码授权。
    POST /api/feishu/register   → 携带句柄轮询该会话状态（downloading / pending /
                                   done / error / expired）。桌面版首次启用时要先下载
                                   飞书 SDK 包，此时二维码尚不存在，改由轮询补发。
                                   注册成功后不直接写 config，由前端再调
                                   /api/tenant/channels 走标准启用流程。

    会话按发起者绑定：句柄不透明，读取一律限定在 ``(user_id, tenant_id)`` 之内，
    因此同一部署里的其他身份既拿不到凭据，也无法探测该会话是否存在。同一身份
    再次发起会替换自己的上一个会话（避免两个 SDK 线程轮询同一次注册），但不会
    影响他人正在进行的会话。
    """

    #: handle -> 会话记录（{handle, owner_user_id, owner_tenant_id, status,
    #: created_at, cancel_event, url, expire_in, qr_image, app_id, app_secret,
    #: error}）。凭据在交付一次后即从记录中移除。
    _sessions: Dict[str, dict] = {}
    _lock = threading.Lock()
    #: 超过此时长的会话被回收；SDK 自身二维码有效期为 600s。
    _SESSION_TTL = 900.0
    #: GET 等待 SDK 产出二维码的上限；超时由前端转轮询。
    _QR_WAIT_SECONDS = 10.0

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """复用 WeixinQrHandler 的二维码渲染。"""
        from channel.web.web_channel import WeixinQrHandler
        return WeixinQrHandler._qr_to_data_uri(data)

    @classmethod
    def _reset_sessions(cls):
        """丢弃全部会话（测试用，也用于干净停机）。"""
        from auth.scan_authorization import _reset as _reset_scan_grants

        with cls._lock:
            for session in cls._sessions.values():
                cancel = session.get("cancel_event")
                if cancel is not None:
                    cancel.set()
            cls._sessions = {}
        # The grants are minted from these sessions and are only meaningful
        # while one exists, so dropping the sessions drops them too.
        _reset_scan_grants()

    @classmethod
    def _purge_expired_locked(cls):
        """回收超时会话。调用方必须已持有 ``_lock``。"""
        now = time.time()
        for handle, session in list(cls._sessions.items()):
            created = float(session.get("created_at") or 0)
            if now - created > cls._SESSION_TTL:
                cancel = session.get("cancel_event")
                if cancel is not None:
                    cancel.set()
                cls._sessions.pop(handle, None)

    @classmethod
    def _create_session(cls, owner_user_id: str, owner_tenant_id: str, *,
                        scope: str = "tenant", agent_id: str = "",
                        auth_session_id: str = "") -> str:
        """为某一身份开启新会话并返回其不透明句柄。

        同一身份已有的会话会被取代（取消并移除），以保证同一次注册只有一个 SDK
        线程在轮询；其他身份的会话不受影响。

        取代的粒度是 ``(user, tenant, scope, agent_id)`` 而不是整个身份：公共页
        与「我的渠道」是两次独立的扫码，同一用户先后打开两边不应该互相取消
        对方的二维码。同一侧重复发起仍然只保留一次注册。
        """
        handle = secrets.token_urlsafe(32)
        owner = (owner_user_id or "", owner_tenant_id or "",
                 scope or "tenant", agent_id or "")
        with cls._lock:
            cls._purge_expired_locked()
            for existing, session in list(cls._sessions.items()):
                if (session.get("owner_user_id"), session.get("owner_tenant_id"),
                        session.get("scope"), session.get("agent_id")) == owner:
                    previous = session.get("cancel_event")
                    if previous is not None:
                        previous.set()
                    cls._sessions.pop(existing, None)
            cls._sessions[handle] = {
                "handle": handle,
                "owner_user_id": owner[0],
                "owner_tenant_id": owner[1],
                "scope": owner[2],
                "agent_id": owner[3],
                "auth_session_id": auth_session_id or "",
                "status": "starting",
                "created_at": time.time(),
                "cancel_event": threading.Event(),
            }
        return handle

    @classmethod
    def _session_for(cls, handle: str, owner_user_id: str, owner_tenant_id: str,
                     auth_session_id: str = ""):
        """仅当调用者拥有该句柄时返回会话记录，否则返回 None。

        他人的句柄与不存在的句柄给出同一答案：调用者不得探测其他身份的会话。
        """
        if not handle:
            return None
        owner = (owner_user_id or "", owner_tenant_id or "")
        with cls._lock:
            session = cls._sessions.get(handle)
            if session is None:
                return None
            if (session.get("owner_user_id"), session.get("owner_tenant_id")) != owner:
                return None
            # The login session is part of the binding too: a handle lifted from
            # a background tab cannot be polled after a re-login, and switching
            # accounts mid-scan costs a rescan rather than handing over a session
            # that was started by someone else.
            if str(session.get("auth_session_id") or "") != (auth_session_id or ""):
                return None
            return session

    @classmethod
    def _mint_scan_grant(cls, session: dict) -> str:
        """The one-time grant that lets this scan's create skip the password.

        Kept here, next to the session that justifies it, so the two cannot
        drift: the grant carries the *same* binding the session does — owner,
        tenant, login session, surface and target — so a ticket minted by the
        personal workbench can never be spent on the public console, and a
        public ticket can never create a private instance (task 4.1).
        """
        from auth.scan_authorization import mint

        return mint(
            actor_user_id=str(session.get("owner_user_id") or ""),
            tenant_id=str(session.get("owner_tenant_id") or ""),
            channel_type="feishu",
            scope=str(session.get("scope") or "tenant"),
            agent_id=str(session.get("agent_id") or ""),
            auth_session_id=str(session.get("auth_session_id") or ""),
        )

    @classmethod
    def _set_status(cls, handle: str, status: str, **fields) -> bool:
        """推进某会话的状态（由 SDK 工作线程调用）。

        会话已被取代或回收时返回 ``False`` 且不写入，因此迟到的 SDK 回调不会
        覆盖更新的会话。
        """
        with cls._lock:
            session = cls._sessions.get(handle)
            if session is None:
                return False
            session["status"] = status
            session.update(fields)
            return True

    @classmethod
    def _poll_payload(cls, handle: str, owner_user_id: str, owner_tenant_id: str,
                      auth_session_id: str = "") -> dict:
        """某一身份的轮询应答；成功时消费凭据。

        未知句柄、他人句柄、别的登录会话的句柄与已消费的会话一律读作
        ``expired``，四者不可区分。
        """
        owner = (owner_user_id or "", owner_tenant_id or "")
        with cls._lock:
            session = cls._sessions.get(handle) if handle else None
            if session is not None and (
                    session.get("owner_user_id"), session.get("owner_tenant_id")) != owner:
                session = None
            if session is not None and str(session.get("auth_session_id") or "") != (
                    auth_session_id or ""):
                session = None
            if session is None:
                return {"status": "success", "register_status": "expired"}
            # 作用域与目标随会话回传：个人扫码的授权只在发起时给定的
            # scope/target 上下文中有效，前端必须原样带回创建请求。
            scope = str(session.get("scope") or "tenant")
            agent_id = str(session.get("agent_id") or "")
            status = session.get("status") or "idle"
            if status == "done":
                payload = {
                    "status": "success",
                    "register_status": "done",
                    "scope": scope,
                    "agent_id": agent_id,
                    "app_id": session.get("app_id", ""),
                    "app_secret": session.get("app_secret", ""),
                    # The console redeems this to create the instance with no
                    # password prompt, so the successful scan does not become a
                    # "configured in the UI but never stored" channel.
                    "scan_ticket": cls._mint_scan_grant(session),
                }
                # 一次性交付：凭据随即从服务端状态中移除。
                cls._sessions.pop(handle, None)
                return payload
            if status in ("error", "expired", "denied"):
                return {"status": "success", "register_status": status,
                        "message": session.get("error", "")}
            if status in ("starting", "idle"):
                # 与旧行为一致：启动阶段对外表现为 pending，二维码由后续轮询补发。
                status = "pending"
            payload = {"status": "success", "register_status": status,
                       "scope": scope, "agent_id": agent_id}
            if session.get("url"):
                payload["qrcode_url"] = session["url"]
                payload["qr_image"] = session.get("qr_image", "")
            return payload

    @classmethod
    def _start_register_thread(cls, handle: str):
        """为 *handle* 指向的会话运行一次 SDK 注册。"""
        with cls._lock:
            session = cls._sessions.get(handle)
            if session is None:
                return
            cancel_event = session["cancel_event"]

        def _worker():
            try:
                # Desktop builds don't bundle lark_oapi; fetch it on demand the
                # first time the user enables Feishu (requires network). Flag it
                # so the modal explains the wait instead of just spinning.
                from channel.feishu import lark_install
                if lark_install.needs_download():
                    cls._set_status(handle, "downloading")
                lark_install.ensure(allow_install=True)
                import lark_oapi as lark
            except ImportError as e:
                cls._set_status(handle, "error", error=(
                    "飞书 SDK 不可用，请联网后重试，"
                    "或手动执行 pip install -U 'lark-oapi>=1.5.5'（%s）" % e
                ))
                return

            def _on_qr(info):
                # SDK 拿到二维码 URL 后立即回调；写入会话让前端 GET 立刻能拿到
                cls._set_status(
                    handle, "pending",
                    url=info.get("url", ""),
                    expire_in=info.get("expire_in", 600),
                    qr_image=cls._qr_to_data_uri(info.get("url", "")),
                )
                logger.info(f"[FeishuRegister] QR ready, expire_in={info.get('expire_in')}s")

            def _on_status(info):
                # 过滤掉 polling 心跳（每 5 秒一次，纯噪音）；
                # 保留 slow_down / domain_switched 等真正的状态切换事件
                status = info.get("status")
                if status == "polling":
                    return
                logger.info(f"[FeishuRegister] SDK status: {info}")

            try:
                result = lark.register_app(
                    on_qr_code=_on_qr,
                    on_status_change=_on_status,
                    source="cowagent",
                    cancel_event=cancel_event,
                )
                cls._set_status(
                    handle, "done",
                    app_id=result.get("client_id", ""),
                    app_secret=result.get("client_secret", ""),
                )
                logger.info(f"[FeishuRegister] App created: app_id={result.get('client_id')}")
            except Exception as e:
                err_msg = str(e)
                err_cls = e.__class__.__name__
                # 飞书 SDK 抛出的 AppExpiredError / AppAccessDeniedError / RegisterAppError
                if "Expired" in err_cls:
                    status = "expired"
                elif "Denied" in err_cls:
                    status = "denied"
                elif "abort" in err_msg.lower() or "cancel" in err_msg.lower():
                    # 被同一身份的新一轮注册取代，保持安静
                    return
                else:
                    status = "error"
                # 会话已被取代或回收时 _set_status 不写入，避免覆盖更新的会话
                cls._set_status(handle, status, error=err_msg)
                logger.warning(f"[FeishuRegister] Register failed ({err_cls}): {err_msg}")

        threading.Thread(target=_worker, daemon=True, name="feishu-register").start()

    @staticmethod
    def _start_scope() -> Tuple[str, str]:
        """``(scope, agent_id)`` for the scan this request starts.

        公共页（无参数）走 ``tenant``；「我的渠道」必须显式声明个人作用域并给出
        本次接入的目标。个人作用域下目标不合法时**在打开飞书对话框之前**就拒绝，
        而不是先让用户扫完码再要求选目标。
        """
        from channel.web.web_channel import _register_owner_scope
        params = web.input(scope='', agent_id='')
        scope = str(getattr(params, "scope", "") or "").strip() or "tenant"
        agent_id = str(getattr(params, "agent_id", "") or "").strip()
        if scope not in ("tenant", "personal"):
            raise ValueError(f"unknown scan scope: {scope}")
        if scope == "tenant":
            if agent_id:
                raise ValueError("a shared scan must not name a personal target")
            return scope, ""
        owner_user_id, owner_tenant_id = _register_owner_scope()
        from auth.service import get_identity_service

        get_identity_service().check_personal_channel_target(
            actor_user_id=owner_user_id, tenant_id=owner_tenant_id,
            agent_id=agent_id)
        return scope, agent_id

    def GET(self):
        """为当前发起者启动一次注册会话，返回句柄与二维码。"""
        from channel.web.web_channel import _register_owner_scope
        from channel.web.web_channel import _verified_auth_session_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from auth.service import IdentityServiceError

            owner_user_id, owner_tenant_id = _register_owner_scope()
            auth_session_id = _verified_auth_session_id()
            try:
                scope, agent_id = self._start_scope()
            except (IdentityServiceError, ValueError) as e:
                # Refused *before* any provider registration starts: a personal
                # scan without a valid private target is a bad request, not a
                # QR code the member scans only to be told to pick a target.
                logger.info(f"[FeishuRegister] scan refused: {e}")
                return json.dumps({
                    "status": "error",
                    "code": getattr(e, "code", "") or "bad_request",
                    "message": str(e),
                }, ensure_ascii=False)
            handle = self._create_session(
                owner_user_id, owner_tenant_id, scope=scope, agent_id=agent_id,
                auth_session_id=auth_session_id)
            self._start_register_thread(handle)
            # 等待 SDK 拿到二维码 URL（最多 10s）。SDK 内部会马上回调 _on_qr。
            import time as _t
            deadline = time.time() + self._QR_WAIT_SECONDS
            while time.time() < deadline:
                session = self._session_for(handle, owner_user_id, owner_tenant_id,
                                            auth_session_id)
                if session is None or session.get("url") or session.get("status") in (
                    "downloading", "error", "expired", "denied"
                ):
                    break
                _t.sleep(0.1)
            session = self._session_for(handle, owner_user_id, owner_tenant_id,
                                        auth_session_id)
            if session is None:
                return json.dumps({
                    "status": "error",
                    "message": "注册会话已失效，请重试",
                })
            if session.get("status") in ("error", "expired", "denied"):
                return json.dumps({
                    "status": "error",
                    "handle": handle,
                    "message": session.get("error", "register failed"),
                })
            if session.get("status") == "downloading":
                # The SDK bundle is still coming down; the QR only exists
                # once it lands, so hand the frontend over to polling.
                return json.dumps({
                    "status": "success",
                    "handle": handle,
                    "register_status": "downloading",
                })
            if not session.get("url"):
                return json.dumps({
                    "status": "error",
                    "handle": handle,
                    "message": "等待飞书二维码超时，请重试",
                })
            return json.dumps({
                "status": "success",
                "handle": handle,
                "qrcode_url": session["url"],
                "qr_image": session.get("qr_image", ""),
                "expire_in": session.get("expire_in", 600),
            })
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        """轮询当前发起者自己的注册会话。"""
        from channel.web.web_channel import _register_owner_scope
        from channel.web.web_channel import _verified_auth_session_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = body.get("action", "poll")
            if action != "poll":
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
            handle = str(body.get("handle") or "").strip()
            if not handle:
                # 句柄缺失不属于归属判定，给出可操作错误而非凭空「过期」。
                return json.dumps({
                    "status": "error",
                    "message": "register handle is required",
                    "code": "missing_handle",
                })
            owner_user_id, owner_tenant_id = _register_owner_scope()
            return json.dumps(
                self._poll_payload(handle, owner_user_id, owner_tenant_id,
                                   _verified_auth_session_id()))
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class PersonalChannelHandler:
    """「我的渠道」：当前租户 + 当前用户的个人消息渠道（任务 6.1-6.7）.

    Every write reuses the tenant channel service with ``allow_owner``: the
    scope/owner pair is forced from the verified request context, so the client
    cannot name a tenant, an owner, or another member's instance. The public
    ``/api/tenant/channels`` surface keeps its administrator gate untouched —
    this is a *separate* registered surface, not a relaxation of that one.
    """

    def GET(self):
        """My instances, plus everything the workbench needs to offer *more*.

        One response carries the list, the candidate targets, the create verdict
        and the per-type readiness, so the page cannot be assembled from a
        partially-failed read (a list that loaded while the candidates did not
        would offer a form that cannot be saved). All three projections are
        derived from the verified context — there is no request field that could
        widen them.
        """
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _personal_channel_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                service = _personal_channel_service()
                listing = service.list_personal_channel_instances(
                    actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id)
                workspace = service.personal_channel_workspace(
                    actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id)
                return json.dumps(
                    {"status": "success", **listing, **workspace},
                    ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Personal channel list error: {e}")
            return _personal_channel_error(e)

    def POST(self):
        """Create one personal instance (``channel_type`` + credentials)."""
        from channel.web.web_channel import _apply_personal_channel_runtime
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _personal_channel_service
        from channel.web.web_channel import _verified_auth_session_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b'{}')
            with _db_scope() as ctx:
                service = _personal_channel_service()
                created = service.create_personal_channel_instance(
                    actor_user_id=ctx.user_id,
                    tenant_id=ctx.tenant_id,
                    channel_type=str(body.get("channel_type") or ""),
                    display_name=str(body.get("display_name") or ""),
                    agent_id=str(body.get("agent_id") or ""),
                    credentials=body.get("credentials"),
                    recent_password=str(body.get("recent_password") or ""),
                    # The grant a completed vendor scan minted. It stands in for
                    # the recent-password proof on the auto-persist path only;
                    # an edit never accepts a create grant (task 4.2).
                    scan_ticket=str(body.get("scan_ticket") or ""),
                    auth_session_id=_verified_auth_session_id(),
                )
                return json.dumps(
                    {"status": "success", "instance": created,
                     "runtime": _apply_personal_channel_runtime(created["id"])},
                    ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Personal channel create error: {e}")
            return _personal_channel_error(e)


class PersonalChannelInstanceHandler:
    """One personal instance: read, edit, enable/disable, revoke, binding.

    The instance id is the only thing the client names; ownership, tenant and
    scope come from the verified context, so a foreign id is refused without
    disclosing whether it exists.
    """

    def GET(self, instance_id: str):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _personal_channel_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                service = _personal_channel_service()
                instance = service.get_personal_channel_instance(
                    actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                    instance_id=instance_id)
                status = service.personal_channel_binding_status(
                    actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                    instance_id=instance_id)
                return json.dumps(
                    {"status": "success", "instance": instance, **status},
                    ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Personal channel read error: {e}")
            return _personal_channel_error(e)

    def POST(self, instance_id: str):
        from channel.web.web_channel import _apply_personal_channel_runtime
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _personal_channel_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b'{}')
            action = (body.get("action") or "").strip()
            with _db_scope() as ctx:
                service = _personal_channel_service()
                if action == "update":
                    result = service.update_personal_channel_instance(
                        actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                        instance_id=instance_id,
                        expected_version=_int_or_zero(body.get("expected_version")),
                        display_name=body.get("display_name"),
                        agent_id=body.get("agent_id"),
                        credentials=body.get("credentials"),
                        recent_password=str(body.get("recent_password") or ""),
                    )
                elif action in ("enable", "disable"):
                    result = service.set_personal_channel_instance_active(
                        actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                        instance_id=instance_id,
                        active=(action == "enable"),
                        expected_version=_int_or_zero(body.get("expected_version")),
                        recent_password=str(body.get("recent_password") or ""),
                    )
                elif action == "revoke":
                    result = service.revoke_personal_channel_credentials(
                        actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                        instance_id=instance_id,
                        expected_version=_int_or_zero(body.get("expected_version")),
                        recent_password=str(body.get("recent_password") or ""),
                    )
                elif action == "start_binding":
                    # The code is returned exactly once: only its hash is stored,
                    # and the member has to be able to read it out of the browser
                    # to send it from their IM account.
                    challenge = service.start_personal_channel_binding(
                        actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                        instance_id=instance_id,
                        expected_version=_int_or_zero(body.get("expected_version")))
                    return json.dumps({"status": "success", "challenge": challenge},
                                      ensure_ascii=False)
                elif action == "unlink":
                    result = service.unlink_personal_channel_instance(
                        actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                        instance_id=instance_id,
                        expected_version=_int_or_zero(body.get("expected_version")))
                else:
                    return json.dumps(
                        {"status": "error", "code": "bad_request",
                         "message": f"unknown action: {action}"},
                        ensure_ascii=False)
                return json.dumps(
                    {"status": "success", "instance": result,
                     "runtime": _apply_personal_channel_runtime(instance_id)
                     if action in ("update", "enable", "disable", "revoke")
                     else None},
                    ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Personal channel write error: {e}")
            return _personal_channel_error(e)


def _personal_channel_service():
    """The identity service, in whatever process serves the console."""
    from auth.service import get_identity_service
    return get_identity_service()


def _personal_channel_error(exc):
    """Render a refusal with the status and code the console branches on."""
    status = getattr(exc, "status", 500)
    code = getattr(exc, "code", "") or "internal"
    message = (exc.args[0] if exc.args else None) or "request failed"
    if status in (401, 403):
        raise web.HTTPError(
            f"{status} Forbidden" if status == 403 else "401 Unauthorized",
            {"Content-Type": "application/json"},
            json.dumps({"status": "error", "code": code, "message": str(message)},
                       ensure_ascii=False))
    if status in (404, 409, 410, 429):
        web.ctx.status = {404: "404 Not Found", 409: "409 Conflict",
                          410: "410 Gone",
                          429: "429 Too Many Requests"}.get(status, "400 Bad Request")
    return json.dumps({"status": "error", "code": code, "message": str(message)},
                      ensure_ascii=False)


def _channel_target_candidates(ctx: "RequestContext") -> List[Dict]:
    """The Agents this caller may name as a channel target, ownership included.

    The choose-a-target component must not offer a target the create would
    refuse (``tenant-channel-configuration``: 界面候选也不允许选择这些目标), and
    only the server knows which those are — so the candidate list is derived here
    from the same object scope the write path runs, never from the caller's role
    name or from the console's own catalogue.

    Each entry carries the ownership the target *produces*, because that is the
    other half of what the operator is choosing: their own private Agent makes a
    connection of their own, a shared Agent makes the tenant's. Naming it here is
    what lets the form say so before saving rather than after.

    Only a usable target is offered: a disabled Agent accepts nothing new, and a
    target that cannot carry traffic is not a legal choice just because its owner
    owns it.
    """
    from channel.web.web_channel import _agent_binding_for
    from channel.web.web_channel import _iter_tenant_agents
    targets: List[Dict] = []
    for profile, tenant_default, _can_chat, unavailable_reason in _iter_tenant_agents(
            ctx, action=SCOPE_MANAGE):
        if unavailable_reason == "agent_disabled":
            continue
        # A channel turns inbound IM traffic into ordinary messages, which a
        # coding Agent cannot receive. Offering it here would promise a target
        # the create is required to refuse.
        if profile.is_coding:
            continue
        binding = _agent_binding_for(ctx, profile.id) or {}
        owns = binding.get("private_owner_user_id") == getattr(ctx, "user_id", None)
        targets.append({
            "id": profile.id,
            "name": profile.name,
            # Which connection this target produces, decided by ownership alone —
            # the same rule ``IdentityService.channel_target_scope`` applies to
            # the write, so the候选 and the write cannot disagree.
            "scope": "user" if owns else "tenant",
            "is_tenant_default": bool(profile.id == tenant_default),
        })
    return targets


