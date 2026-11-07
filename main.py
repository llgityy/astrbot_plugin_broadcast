import asyncio
import random
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_broadcast", "dsh", "批量转发", "1.0.0")
class BroadcastPlugin(Star):
    """私聊喂内容 -> /go -> 按目标清单逐条转发。内容不过模型，原样透传。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        self.queue: list[Any] = []      # 每条 = 一个原始消息段数组/CQ字符串
        self._running = False
        self.collecting = False  # 只有开启收集时才入队，平时不干扰聊天
        logger.info(f"[broadcast] 已加载，队列 {len(self.queue)}")

    # ---------- 工具 ----------
    def _num_list(self, key: str) -> list[str]:
        raw = self.config.get(key, []) or []
        if isinstance(raw, str):
            raw = [x for x in raw.replace("\n", ",").split(",")]
        out = []
        for x in raw:
            s = str(x).strip()
            if s.isdigit():
                out.append(s)
        return out

    def _allowed(self, event: AstrMessageEvent) -> bool:
        allowed = self._num_list("allowed_users")
        if not allowed:
            return True
        try:
            return str(event.get_sender_id()) in allowed
        except Exception:
            return False

    @staticmethod
    def _segments(event: AstrMessageEvent):
        """取原始 OneBot 消息，最保真（图片/表情/链接都在）。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            msg = raw.get("message")
            if isinstance(msg, (list, str)) and msg:
                return msg
        msg = getattr(event.message_obj, "message", None)
        if msg is not None:
            try:
                return [c.to_dict() for c in msg]
            except Exception:
                pass
        text = event.message_str or ""
        return {"type": "text", "data": {"text": text}} if text else None

    @staticmethod
    def _raw_text(event: AstrMessageEvent) -> str:
        """原始文本（保留 / 前缀；event.message_str 会被去掉唤醒前缀）。"""
        segs = BroadcastPlugin._segments(event)
        if isinstance(segs, str):
            return segs
        if isinstance(segs, dict):
            return str((segs.get("data") or {}).get("text", ""))
        parts = []
        for s in segs or []:
            if isinstance(s, dict) and s.get("type") == "text":
                parts.append(str((s.get("data") or {}).get("text", "")))
        return "".join(parts)

    def _is_command(self, event: AstrMessageEvent) -> bool:
        raw = self._raw_text(event).strip()
        prefixes = self.config.get("wake_prefixes") or ["/", "／", "!", "！"]
        return bool(raw) and any(raw.startswith(p) for p in prefixes)

    # ---------- 收集 ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def collect(self, event: AstrMessageEvent):
        if not self._allowed(event):
            return
        if self._is_command(event):
            return  # 命令交给命令处理器（/go /q /clear）
        if not self.collecting:
            return  # 没开收集：正常聊天，不拦不问
        segs = self._segments(event)
        if not segs:
            return
        self.queue.append(segs)
        event.should_call_llm(False)
        event.stop_event()
        if self.config.get("collect_hint", True):
            yield event.plain_result(f"📥 {len(self.queue)}")

    # ---------- 命令 ----------
    @filter.command("r", alias={"开始", "收集", "转发"})
    async def cmd_begin(self, event: AstrMessageEvent):
        """开始收集内容"""
        if not self._allowed(event):
            return
        self.collecting = True
        hint = "（当前队列还有旧内容，先 /clear 清掉）" if self.queue else ""
        yield event.plain_result(f"🟢 开始收集，发完内容发 /go{hint}")

    @filter.command("sync", alias={"同步"})
    async def cmd_sync(self, event: AstrMessageEvent):
        """拉取机器人所在全部群 + 全部好友，写入目标列表"""
        if not self._allowed(event):
            return
        bot = getattr(event, "bot", None)
        if bot is None:
            yield event.plain_result("当前平台不支持，仅 aiocqhttp 可用")
            return
        api = bot if hasattr(bot, "call_action") else getattr(bot, "api", None)
        if api is None:
            yield event.plain_result("拿不到 OneBot 客户端")
            return

        groups: list[str] = []
        names: list[str] = []
        try:
            raw = await api.call_action("get_group_list")
            for g in raw or []:
                gid = str(g.get("group_id", ""))
                if gid.isdigit():
                    groups.append(gid)
                    names.append(str(g.get("group_name", gid))[:12])
        except Exception as e:
            yield event.plain_result(f"取群列表失败：{e}")
            return

        friends: list[str] = []
        note = ""
        try:
            raw_f = await api.call_action("get_friend_list")
            for f in raw_f or []:
                uid = str(f.get("user_id", ""))
                if uid.isdigit():
                    friends.append(uid)
        except Exception as e:
            note = f"（好友列表取失败：{e}）"

        self.config["target_groups"] = groups
        self.config["target_users"] = friends
        try:
            self.config.save_config()
        except Exception as e:
            yield event.plain_result(f"写入配置失败：{e}")
            return

        preview = "、".join(names[:12])
        if len(names) > 12:
            preview += f" …共{len(names)}个"
        yield event.plain_result(
            f"✅ 群 {len(groups)} 个：{preview}\n好友 {len(friends)} 个{note}\n已写入配置，发 /q 可检查"
        )

    @filter.command("q", alias={"队列"})
    async def cmd_queue(self, event: AstrMessageEvent):
        """查看当前队列"""
        if not self._allowed(event):
            return
        if not self.queue:
            yield event.plain_result("队列空" + ("（收集中）" if self.collecting else ""))
            return
        lines = []
        for i, s in enumerate(self.queue, 1):
            lines.append(f"{i}. {self._describe(s)}")
        yield event.plain_result(f"队列 {len(self.queue)} 条：\n" + "\n".join(lines))

    @filter.command("clear", alias={"清空"})
    async def cmd_clear(self, event: AstrMessageEvent):
        """清空队列"""
        if not self._allowed(event):
            return
        n = len(self.queue)
        self.queue.clear()
        self.collecting = False
        yield event.plain_result(f"已清空 {n} 条，退出收集")

    @filter.command("go", alias={"群发", "转发"})
    async def cmd_go(self, event: AstrMessageEvent):
        """开始群发"""
        if not self._allowed(event):
            return
        if self._running:
            yield event.plain_result("上一批还没发完")
            return
        if not self.queue:
            yield event.plain_result("队列空，先发内容再来 /go")
            return

        groups = self._num_list("target_groups")
        users = self._num_list("target_users")
        if not groups and not users:
            yield event.plain_result("没配置目标（target_groups / target_users）")
            return

        items = list(self.queue)
        self.queue.clear()
        self.collecting = False
        total = len(items) * (len(groups) + len(users))
        self._running = True
        yield event.plain_result(
            f"🚀 {len(items)} 条 x {len(groups)} 群 + {len(users)} 私聊 = {total} 次"
        )

        asyncio.create_task(
            self._run(
                event.bot,
                items,
                groups,
                users,
                event.unified_msg_origin,
            )
        )

    # ---------- 执行 ----------
    @staticmethod
    def _describe(seg):
        try:
            if isinstance(seg, str):
                return seg[:40]
            parts = []
            for s in seg:
                t = s.get("type")
                if t == "text":
                    parts.append(s.get("data", {}).get("text", "")[:30])
                elif t == "image":
                    parts.append("[图片]")
                else:
                    parts.append(f"[{t}]")
            return " ".join(parts)[:40]
        except Exception:
            return "[?]"

    async def _run(self, bot, items, groups, users, origin):
        interval = self.config.get("interval_seconds", 5)
        try:
            interval = float(interval)
        except Exception:
            interval = 5.0

        ok = 0
        fail = 0
        errs = []

        async def send_one(kind, kid, segs):
            nonlocal ok, fail
            try:
                api = bot if hasattr(bot, "call_action") else bot.api
                if kind == "group":
                    await api.call_action(
                        "send_group_msg", group_id=int(kid), message=segs
                    )
                else:
                    await api.call_action(
                        "send_private_msg", user_id=int(kid), message=segs
                    )
                ok += 1
            except Exception as e:
                fail += 1
                if len(errs) < 8:
                    errs.append(f"{'群' if kind == 'group' else '私聊'}{kid}: {str(e)[:60]}")
                logger.warning(f"[broadcast] {kind} {kid} 失败: {e}")
            if interval > 0:
                await asyncio.sleep(random.uniform(interval * 0.75, interval * 1.3))

        # 目标之间稍长停顿
        for gid in groups:
            for segs in items:
                await send_one("group", gid, segs)
            if interval > 0:
                await asyncio.sleep(random.uniform(1.5, 3.0))
        for uid in users:
            for segs in items:
                await send_one("user", uid, segs)
            if interval > 0:
                await asyncio.sleep(random.uniform(1.5, 3.0))

        self._running = False
        text = f"✅ {ok} 成功 / ❌ {fail} 失败"
        if errs:
            text += "\n" + "\n".join(errs)
        try:
            await self.context.send_message(origin, MessageChain([Plain(text)]))
        except Exception as e:
            logger.error(f"[broadcast] 回执失败: {e}")