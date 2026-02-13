from __future__ import annotations

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter
from astrbot.api import AstrBotConfig, logger
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

import time
import asyncio
from typing import Any


@register(
    "astrbot_plugin_mute_appeal_thanker",
    "久孤(ksjiu)",
    "当被禁言时私聊求情；被解除禁言时私聊感谢。",
    "3.2.1",
)
class BanResponder(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.plea_template = self.config.get(
            "fixed_plea_message",
            "呜呜呜，{admin_name}大人，我在群【{group_name}】被禁言了 {duration_str}，能原谅我吗？",
        )

        self.thanks_template = self.config.get(
            "fixed_thanks_message",
            "谢谢{admin_name}大人在群【{group_name}】解除了我的禁言！",
        )

        try:
            self.cooldown_seconds = int(self.config.get("cooldown_seconds", 10) or 0)
        except Exception:
            logger.error("cooldown_seconds 配置错误，已使用默认值 10")
            self.cooldown_seconds = 10

        try:
            self.admin_rate_limit = int(self.config.get("admin_rate_limit", 5) or 0)
        except Exception:
            self.admin_rate_limit = 5

        blacklist = self.config.get("admin_blacklist", [])
        if isinstance(blacklist, (list, set, tuple)):
            self.admin_blacklist = {str(i) for i in blacklist}
        elif blacklist:
            self.admin_blacklist = {str(blacklist)}
        else:
            self.admin_blacklist = set()

        self._last_event_cache: dict[str, float] = {}
        self._admin_last_send: dict[str, float] = {}

        self._state_lock = asyncio.Lock()

        logger.info("BanResponder 插件已启动。")

    def _parse_duration(self, seconds: int) -> str:
        if seconds <= 0:
            return "很短时间"

        d, r = divmod(seconds, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)

        text = ""
        if d:
            text += f"{d}天"
        if h:
            text += f"{h}小时"
        if m:
            text += f"{m}分钟"
        if s or not text:
            text += f"{s}秒"

        return text

    def _safe_text(self, text: str) -> str:
        return str(text).replace("{", "【").replace("}", "】")

    def _safe_format(self, template: str, **kwargs) -> str:
        try:
            return template.format(**kwargs)
        except Exception as e:
            logger.error(f"模板格式化失败: {e}")
            return "消息模板存在错误，请检查配置。"

    async def _check_and_mark(self, event_key: str, operator_id: str) -> bool:

        if self.cooldown_seconds <= 0 and self.admin_rate_limit <= 0:
            return False

        async with self._state_lock:
            now = time.time()

            # 清理过期事件
            expired = [
                k for k, v in self._last_event_cache.items()
                if now - v > self.cooldown_seconds
            ]
            for k in expired:
                del self._last_event_cache[k]

            # 事件级去重
            if self.cooldown_seconds > 0:
                last = self._last_event_cache.get(event_key)
                if last and (now - last) < self.cooldown_seconds:
                    return True
                self._last_event_cache[event_key] = now

            # 管理员级限流
            if self.admin_rate_limit > 0:
                last_admin = self._admin_last_send.get(operator_id)
                if last_admin and (now - last_admin) < self.admin_rate_limit:
                    return True
                self._admin_last_send[operator_id] = now

            return False

    async def _get_group_name(self, client: Any, group_id: int) -> str:
        try:
            info = await asyncio.wait_for(
                client.get_group_info(group_id=group_id),
                timeout=5,
            )
            return self._safe_text(info.get("group_name", group_id))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"获取群名失败 group_id={group_id} err={e}")
            return str(group_id)

    async def _get_admin_name(self, client: Any, group_id: int, operator_id: int) -> str:
        try:
            info = await asyncio.wait_for(
                client.get_group_member_info(
                    group_id=group_id,
                    user_id=operator_id,
                ),
                timeout=5,
            )
            name = info.get("card") or info.get("nickname") or operator_id
            return self._safe_text(name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(
                f"获取管理员昵称失败 group_id={group_id} operator_id={operator_id} err={e}"
            )
            return str(operator_id)

    async def _send_private(self, client: Any, user_id: int, message: str) -> bool:
        try:
            await asyncio.wait_for(
                client.send_private_msg(user_id=user_id, message=message),
                timeout=5,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"发送私聊失败 user_id={user_id} err={e}")
            return False

    @filter.event_message_type(EventMessageType.NOTICE)
    async def handle_notice(self, event: AiocqhttpMessageEvent):

        if event.get_platform_name() != "aiocqhttp":
            return

        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return

        if raw.get("post_type") != "notice":
            return

        if raw.get("notice_type") != "group_ban":
            return

        try:
            bot_id = int(event.get_self_id())
            target_id = int(raw.get("user_id"))
            group_id = int(raw.get("group_id"))
            operator_id = int(raw.get("operator_id"))
            duration = int(raw.get("duration", 0))
        except Exception:
            logger.debug(f"字段解析失败 raw={raw}")
            return

        if target_id != bot_id:
            return

        if operator_id == bot_id:
            return

        if str(operator_id) in self.admin_blacklist:
            return

        # event_key 去掉 duration，避免平台浮动绕过去重
        event_key = f"{group_id}:{operator_id}:{target_id}"

        blocked = await self._check_and_mark(event_key, str(operator_id))
        if blocked:
            return

        client = event.bot

        group_name = await self._get_group_name(client, group_id)
        admin_name = await self._get_admin_name(client, group_id, operator_id)

        if duration > 0:
            duration_text = self._parse_duration(duration)
            message = self._safe_format(
                self.plea_template,
                admin_name=admin_name,
                group_name=group_name,
                duration_str=duration_text,
            )
        else:
            message = self._safe_format(
                self.thanks_template,
                admin_name=admin_name,
                group_name=group_name,
            )

        sent = await self._send_private(client, operator_id, message)

        if sent:
            event.stop_event()

    async def terminate(self):
        logger.info("BanResponder 插件已卸载。")