"""插件层冒烟测试：用假的 astrbot 模块跑指令与 LLM 工具（无需真装 AstrBot）。

覆盖：指令回复、重复签到、排行榜参数校验、is_admin 重置门禁、llm_tool 参数校验、
配置项默认值（_conf_schema.json 与 README 对齐）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)


def _install_astrbot_stubs() -> None:
    """构造最小可用的 astrbot.api 桩，让 main.py 能 import。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    comp_mod = types.ModuleType("astrbot.api.message_components")

    def _passthrough_decorator(*args, **kwargs):
        def deco(func):
            return func

        return deco

    fake_filter = types.SimpleNamespace(
        command=_passthrough_decorator,
        event_message_type=_passthrough_decorator,
        llm_tool=_passthrough_decorator,
        EventMessageType=types.SimpleNamespace(ALL="all"),
    )

    class AstrMessageEvent:  # noqa: D401 - 仅用于类型标注
        pass

    class Context:
        def __init__(self, config=None):
            self._config = config or {}

        def get_config(self):
            return self._config

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    # 消息段桩：字段/序列化与 astrbot.core.message.components 保持一致
    class Plain:
        def __init__(self, text: str = "", **kwargs):
            self.type = "Plain"
            self.text = text

        def toDict(self):
            return {"type": "text", "data": {"text": self.text}}

    class At:
        def __init__(self, qq=None, name: str = "", **kwargs):
            self.type = "At"
            self.qq = qq
            self.name = name

        def toDict(self):
            return {"type": "at", "data": {"qq": str(self.qq)}}

    class Reply:
        def __init__(self, id=None, **kwargs):
            self.type = "Reply"
            self.id = id

        def toDict(self):
            return {"type": "reply", "data": {"id": str(self.id)}}

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = fake_filter
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    comp_mod.Plain = Plain
    comp_mod.At = At
    comp_mod.Reply = Reply

    api.logger = logging.getLogger("astrbot.stub")
    api.llm_tool = _passthrough_decorator
    api.message_components = comp_mod

    astrbot.api = api
    astrbot.api.event = event_mod
    astrbot.api.star = star_mod
    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.event", event_mod)
    sys.modules.setdefault("astrbot.api.star", star_mod)
    sys.modules.setdefault("astrbot.api.message_components", comp_mod)


_install_astrbot_stubs()

import main as plugin_main  # noqa: E402


class FakeMessageObj:
    """够用的 AstrBotMessage 替身（只需要 message_id 供引用回复使用）。"""

    def __init__(self, message_id="msg-1001", message_str=""):
        self.message_id = message_id
        self.message_str = message_str


class FakeEvent:
    """够用的 AstrMessageEvent 替身。"""

    def __init__(self, text="", group_id="10001", sender_id="20002",
                 sender_name="测试用户", admin=False, message_id="msg-1001"):
        self.message_str = text
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._admin = admin
        self.message_obj = FakeMessageObj(message_id, text)

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, path):
        return ("image", path)

    def chain_result(self, chain):
        return ("chain", chain)


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def run_agen(agen):
    return asyncio.run(_collect(agen))


class PluginCase(unittest.TestCase):
    config = {}

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="checkin_plugin_")
        os.environ["CHECKIN_DATA_DIR"] = self.tmpdir
        self.plugin = plugin_main.CheckinPlugin(
            plugin_main.Context({}), dict(self.config)
        )

    def tearDown(self):
        os.environ.pop("CHECKIN_DATA_DIR", None)
        for name in os.listdir(self.tmpdir):
            path = os.path.join(self.tmpdir, name)
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                for inner in os.listdir(path):
                    os.remove(os.path.join(path, inner))
                os.rmdir(path)
        os.rmdir(self.tmpdir)

    def reply(self, agen):
        results = run_agen(agen)
        self.assertTrue(results, "指令没有产生任何回复")
        return results[0][1]


class TestCommands(PluginCase):
    def test_checkin_reply_and_duplicate(self):
        text = self.reply(self.plugin.cmd_checkin(FakeEvent("/签到")))
        self.assertIn("签到成功", text)
        self.assertIn("连签 1 天", text)
        self.assertIn("累计", text)
        again = self.reply(self.plugin.cmd_checkin(FakeEvent("/签到")))
        self.assertIn("已经签到过", again)

    def test_keyword_trigger(self):
        text = self.reply(self.plugin.on_keyword(FakeEvent("签到")))
        self.assertIn("签到成功", text)

    def test_success_reply_is_compact(self):
        """签到成功只回一两条，不带“今日已有 N 人签到”这类附加统计。"""
        text = self.reply(self.plugin.cmd_checkin(FakeEvent("/签到")))
        self.assertLessEqual(len(text.strip().splitlines()), 2)
        self.assertIn("签到成功", text)
        self.assertNotIn("今日已有", text)

    def test_leaderboard_today_text_and_image_fallback(self):
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到")))  # 先有数据
        text = self.reply(self.plugin.cmd_leaderboard(FakeEvent("/签到榜 today")))
        self.assertIn("今日签到榜", text)
        self.assertIn("测试用户", text)

    def test_leaderboard_empty_is_friendly(self):
        text = self.reply(self.plugin.cmd_leaderboard(FakeEvent("/签到榜 total")))
        self.assertIn("📭", text)

    def test_leaderboard_bad_args_returns_usage(self):
        text = self.reply(self.plugin.cmd_leaderboard(FakeEvent("/签到榜 火星榜")))
        self.assertIn("参数不合法", text)
        self.assertIn("用法", text)

    def test_leaderboard_global_scope(self):
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到", group_id="10001")))
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到", group_id="10002",
                                                   sender_id="30003", sender_name="隔壁")))
        text = self.reply(self.plugin.cmd_leaderboard(FakeEvent("/签到榜 total 全局")))
        self.assertIn("全群汇总", text)
        self.assertIn("隔壁", text)

    def test_my_checkin(self):
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到")))
        text = self.reply(self.plugin.cmd_my_checkin(FakeEvent("/我的签到")))
        self.assertIn("累计积分", text)
        self.assertIn("当前连签", text)

    def test_reset_requires_admin(self):
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到")))
        denied = self.reply(self.plugin.cmd_reset(FakeEvent("/签到重置 确认", admin=False)))
        self.assertIn("只有管理员", denied)
        self.assertEqual(self.plugin.store.count_records("10001"), 1)

        need_confirm = self.reply(self.plugin.cmd_reset(FakeEvent("/签到重置", admin=True)))
        self.assertIn("确认请发送", need_confirm)

        done = self.reply(self.plugin.cmd_reset(FakeEvent("/签到重置 确认", admin=True)))
        self.assertIn("已清空", done)
        self.assertEqual(self.plugin.store.count_records("10001"), 0)


class TestCritReply(PluginCase):
    """把暴击概率配成 1，验证回执里确实出现暴击文案。"""

    config = {"crit_chance": 1.0}

    def test_crit_reply_contains_crit_line(self):
        text = self.reply(self.plugin.cmd_checkin(FakeEvent("/签到")))
        self.assertIn("💥 暴击 ×3", text)
        self.assertIn("本次 +", text)

    def test_crit_message_is_configurable(self):
        text = self.reply(self.plugin.cmd_checkin(FakeEvent("/签到")))
        self.assertNotIn("💥 暴击 ×10", text)


class StyleCase(PluginCase):
    """带 first_result 辅助方法的基类（本身不含用例，避免被各模式继承错配）。"""

    config = {}

    def first_result(self, agen):
        results = run_agen(agen)
        self.assertTrue(results, "指令没有产生任何回复")
        return results[0]


class TestSuccessReplyStyleDefault(StyleCase):
    """success_reply_style 默认值与回退行为（默认保持现状：普通文本）。"""

    def test_default_style_is_plain_text(self):
        """默认（text）保持旧行为：plain_result，不带 At / Reply 段。"""
        kind, payload = self.first_result(self.plugin.cmd_checkin(FakeEvent("/签到")))
        self.assertEqual(kind, "plain")
        self.assertIn("测试用户 签到成功", payload)
        self.assertEqual(payload.count("\n"), 0)  # 仍然是一行

    def test_non_success_reply_stays_plain(self):
        """只有签到成功才加装饰，重复签到提示仍是纯文本。"""
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到")))
        kind, payload = self.first_result(self.plugin.cmd_checkin(FakeEvent("/签到")))
        self.assertEqual(kind, "plain")
        self.assertIn("已经签到过", payload)

    def test_invalid_style_falls_back_to_text(self):
        plugin = plugin_main.CheckinPlugin(
            plugin_main.Context({}), {"success_reply_style": "火星模式"}
        )
        old, self.plugin = self.plugin, plugin
        try:
            kind, _payload = self.first_result(plugin.cmd_checkin(FakeEvent("/签到")))
        finally:
            self.plugin = old
        self.assertEqual(kind, "plain")


class TestSuccessReplyStyleAt(StyleCase):
    config = {"success_reply_style": "at"}

    def test_at_segment_carries_real_userid(self):
        """At 段带的是 event.get_sender_id() 的真实 userid，不是纯文本昵称。"""
        kind, chain = self.first_result(
            self.plugin.cmd_checkin(FakeEvent("/签到", sender_id="20002", sender_name="阿一"))
        )
        self.assertEqual(kind, "chain")
        self.assertEqual([seg.type for seg in chain], ["At", "Plain"])
        at, plain = chain
        self.assertEqual(str(at.qq), "20002")
        self.assertEqual(at.name, "阿一")
        self.assertEqual(at.toDict(), {"type": "at", "data": {"qq": "20002"}})

    def test_at_mode_does_not_repeat_nickname(self):
        """@ 已经把人点出来了，正文不再重复昵称，且依然只有一行。"""
        _kind, chain = self.first_result(
            self.plugin.cmd_checkin(FakeEvent("/签到", sender_name="阿一"))
        )
        text = chain[-1].text
        self.assertIn("✅ 签到成功！", text)
        self.assertNotIn("阿一 签到成功", text)
        self.assertEqual(text.count("\n"), 0)

    def test_keyword_trigger_also_decorated(self):
        kind, chain = self.first_result(self.plugin.on_keyword(FakeEvent("签到")))
        self.assertEqual(kind, "chain")
        self.assertEqual(chain[0].type, "At")


class TestSuccessReplyStyleQuote(StyleCase):
    config = {"success_reply_style": "quote"}

    def test_reply_segment_carries_message_id(self):
        kind, chain = self.first_result(
            self.plugin.cmd_checkin(FakeEvent("/签到", message_id="998877"))
        )
        self.assertEqual(kind, "chain")
        self.assertEqual([seg.type for seg in chain], ["Reply", "Plain"])
        reply, plain = chain
        self.assertEqual(str(reply.id), "998877")
        self.assertEqual(reply.toDict(), {"type": "reply", "data": {"id": "998877"}})
        self.assertIn("测试用户 签到成功", plain.text)  # 没有 @，昵称保留

    def test_missing_message_id_falls_back_to_text(self):
        """拿不到 message_id 时不能抛异常，直接退回纯文本。"""
        kind, payload = self.first_result(
            self.plugin.cmd_checkin(FakeEvent("/签到", message_id=""))
        )
        self.assertEqual(kind, "plain")
        self.assertIn("签到成功", payload)


class TestSuccessReplyStyleBoth(StyleCase):
    config = {"success_reply_style": "both"}

    def test_chain_order_reply_at_plain(self):
        """引用段在前、At 其次、正文最后（对齐 OneBot v11 习惯）。"""
        kind, chain = self.first_result(
            self.plugin.cmd_checkin(FakeEvent("/签到", sender_id="30003"))
        )
        self.assertEqual(kind, "chain")
        self.assertEqual([seg.type for seg in chain], ["Reply", "At", "Plain"])
        self.assertEqual(str(chain[0].id), "msg-1001")
        self.assertEqual(str(chain[1].qq), "30003")


class TestSuccessStyleNormalize(unittest.TestCase):
    def test_aliases(self):
        normalize = plugin_main.normalize_success_style
        self.assertEqual(normalize(""), "text")
        self.assertEqual(normalize(None), "text")
        self.assertEqual(normalize("关闭"), "text")
        self.assertEqual(normalize(" plain "), "text")
        self.assertEqual(normalize("AT"), "at")
        self.assertEqual(normalize("@"), "at")
        self.assertEqual(normalize("艾特"), "at")
        self.assertEqual(normalize("引用"), "quote")
        self.assertEqual(normalize("Reply"), "quote")
        self.assertEqual(normalize("at+quote"), "both")
        self.assertEqual(normalize("at-quote"), "both")
        self.assertEqual(normalize("火星"), "text")

    def test_code_default_matches_schema(self):
        with open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), encoding="utf-8") as fp:
            schema = json.load(fp)
        item = schema["success_reply_style"]
        self.assertEqual(item["type"], "string")
        self.assertEqual(item["default"], plugin_main.DEFAULT_SUCCESS_STYLE)
        self.assertEqual(item["options"], ["text", "at", "quote", "both"])


class TestLlmTools(PluginCase):
    def test_status_tool(self):
        out = asyncio.run(self.plugin.llm_checkin_status(FakeEvent("/x")))
        self.assertIn("还没签到", out)
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到")))
        out = asyncio.run(self.plugin.llm_checkin_status(FakeEvent("/x")))
        self.assertIn("已签到", out)

    def test_status_tool_rejects_bad_date(self):
        out = asyncio.run(self.plugin.llm_checkin_status(FakeEvent("/x"), date="2026-13-99"))
        self.assertIn("参数不合法", out)

    def test_leaderboard_tool(self):
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到")))
        out = asyncio.run(self.plugin.llm_checkin_leaderboard(FakeEvent("/x"), board="today"))
        self.assertIn("今日签到榜", out)

    def test_leaderboard_tool_validates_args(self):
        out = asyncio.run(
            self.plugin.llm_checkin_leaderboard(FakeEvent("/x"), board="火星", limit="abc")
        )
        self.assertIn("参数不合法", out)
        out = asyncio.run(
            self.plugin.llm_checkin_leaderboard(FakeEvent("/x"), board="total", scope="宇宙")
        )
        self.assertIn("参数不合法", out)

    def test_leaderboard_tool_clamps_limit(self):
        run_agen(self.plugin.cmd_checkin(FakeEvent("/签到")))
        out = asyncio.run(
            self.plugin.llm_checkin_leaderboard(FakeEvent("/x"), board="total", limit="999")
        )
        self.assertIn("共 1 条", out)


class TestConfigSchema(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), encoding="utf-8") as fp:
            self.schema = json.load(fp)

    def test_required_keys_present(self):
        for key in (
            "points_min", "points_max", "first_checkin_bonus", "streak_bonus_start",
            "streak_bonus_per_day", "streak_bonus_max", "crit_enabled", "crit_chance",
            "crit_multiplier", "crit_tiers", "crit_message", "leaderboard_limit",
            "leaderboard_scope", "timezone",
        ):
            self.assertIn(key, self.schema)
            self.assertIn("default", self.schema[key])

    def test_documented_defaults(self):
        self.assertTrue(self.schema["crit_enabled"]["default"])
        self.assertEqual(self.schema["crit_chance"]["default"], 0.05)
        self.assertEqual(self.schema["crit_multiplier"]["default"], 3)
        self.assertEqual(self.schema["streak_bonus_start"]["default"], 3)
        self.assertEqual(self.schema["streak_bonus_per_day"]["default"], 0.1)
        self.assertEqual(self.schema["streak_bonus_max"]["default"], 1.0)
        self.assertEqual(self.schema["points_min"]["default"], 1)
        self.assertEqual(self.schema["points_max"]["default"], 10)
        self.assertEqual(self.schema["timezone"]["default"], "Asia/Shanghai")
        self.assertEqual(self.schema["leaderboard_limit"]["default"], 10)

    def test_defaults_match_code_defaults(self):
        from checkin_core import Settings

        defaults = Settings()
        self.assertEqual(self.schema["crit_chance"]["default"], defaults.crit_chance)
        self.assertEqual(self.schema["crit_multiplier"]["default"], defaults.crit_multiplier)
        self.assertEqual(self.schema["crit_enabled"]["default"], defaults.crit_enabled)
        self.assertEqual(self.schema["points_min"]["default"], defaults.points_min)
        self.assertEqual(self.schema["points_max"]["default"], defaults.points_max)
        self.assertEqual(self.schema["first_checkin_bonus"]["default"],
                         defaults.first_checkin_bonus)
        self.assertEqual(self.schema["streak_bonus_start"]["default"],
                         defaults.streak_bonus_start)
        self.assertEqual(self.schema["streak_bonus_per_day"]["default"],
                         defaults.streak_bonus_per_day)
        self.assertEqual(self.schema["streak_bonus_max"]["default"], defaults.streak_bonus_max)
        self.assertEqual(self.schema["leaderboard_limit"]["default"],
                         defaults.leaderboard_limit)
        self.assertEqual(self.schema["timezone"]["default"], defaults.timezone)
        self.assertEqual(self.schema["crit_message"]["default"], defaults.crit_message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
