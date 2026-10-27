"""AstrBot 每日签到插件（群维度，含暴击 / 积分 / 排行榜）。

指令：
  /签到        每日签到（也可裸发关键词"签到"，可在 WebUI 关闭）
  /签到榜      今日榜 / 总榜 / 连签榜，支持本群 / 全局口径，top10
  /我的签到    查自己的积分、连签、累计签到天数
  /签到重置    清空本群签到数据（仅管理员，需二次确认）

数据落在 AstrBot data 目录下的 plugin_data/astrbot_plugin_checkin/checkin.db。
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:  # 保证同目录模块可被导入
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # AstrBot 4.x 统一导出
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot.plugin.checkin")

try:
    from astrbot.api import llm_tool
except Exception:  # pragma: no cover - 老版本没有该导出时降级到 filter.llm_tool
    llm_tool = getattr(filter, "llm_tool", None)  # type: ignore[assignment]
    if llm_tool is None:
        logger.warning("[checkin] 当前 AstrBot 未提供 llm_tool 装饰器，LLM 工具将不可用")

        def llm_tool(*args: Any, **kwargs: Any):  # type: ignore[misc]
            def _deco(func):
                func.__llm_tool__ = kwargs or True
                return func

            return _deco


try:  # 相对导入（作为包加载时）
    from .checkin_core import (
        BOARD_STREAK,
        BOARD_TODAY,
        BOARD_TOTAL,
        SCOPE_GLOBAL,
        SCOPE_GROUP,
        AlreadyCheckedIn,
        CheckinError,
        CheckinStore,
        InvalidArgument,
        Settings,
        clamp_limit,
        format_board,
        format_checkin_reply,
        get_tz,
        normalize_board,
        normalize_date,
        normalize_scope,
        parse_board_args,
    )
except ImportError:  # 绝对导入（AstrBot 以文件形式加载时）
    from checkin_core import (  # type: ignore[no-redef]
        BOARD_STREAK,
        BOARD_TODAY,
        BOARD_TOTAL,
        SCOPE_GLOBAL,
        SCOPE_GROUP,
        AlreadyCheckedIn,
        CheckinError,
        CheckinStore,
        InvalidArgument,
        Settings,
        clamp_limit,
        format_board,
        format_checkin_reply,
        get_tz,
        normalize_board,
        normalize_date,
        normalize_scope,
        parse_board_args,
    )

PLUGIN_NAME = "astrbot_plugin_checkin"
PLUGIN_VERSION = "1.1.0"

BOARD_LABEL = {BOARD_TODAY: "今日", BOARD_TOTAL: "总榜", BOARD_STREAK: "连签"}
BOARD_UNIT = {BOARD_TODAY: "分", BOARD_TOTAL: "分", BOARD_STREAK: "天"}

# 签到相关配置项（LLM/命令里统一按这个表读取，避免漏项）
SETTINGS_KEYS = (
    "points_min",
    "points_max",
    "first_checkin_bonus",
    "streak_bonus_start",
    "streak_bonus_per_day",
    "streak_bonus_max",
    "timezone",
    "leaderboard_limit",
    "leaderboard_scope",
    "crit_enabled",
    "crit_chance",
    "crit_multiplier",
    "crit_tiers",
    "crit_message",
)

BOARD_USAGE = "用法：/签到榜 [今日|总榜|连签] [本群|全局] [top10]"


def _as_str_list(value: Any) -> list[str]:
    """配置里的白/黑名单可能是 list、逗号分隔字符串或空值。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _resolve_data_dir() -> Path:
    """定位 AstrBot 的 data 目录，绝不写死在插件包目录里。"""
    env_override = os.environ.get("CHECKIN_DATA_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()

    candidates: list[Path] = []
    try:  # AstrBot 官方工具函数
        from astrbot.core.utils import astrbot_path  # type: ignore

        for fn_name in ("get_astrbot_data_path", "get_astrbot_root", "get_astrbot_path"):
            fn = getattr(astrbot_path, fn_name, None)
            if callable(fn):
                try:
                    candidates.append(Path(fn()))
                except Exception:
                    continue
    except Exception:
        pass

    for env_key in ("ASTRBOT_ROOT", "ASTRBOT_DATA_DIR", "ASTRBOT_HOME"):
        raw = os.environ.get(env_key)
        if raw:
            candidates.append(Path(raw))

    candidates.append(Path.cwd() / "data")

    for base in candidates:
        try:
            base = base.expanduser()
            if not base.is_absolute():
                base = (Path.cwd() / base).resolve()
            target = base / "plugin_data" / PLUGIN_NAME
            target.mkdir(parents=True, exist_ok=True)
            return target
        except Exception:
            continue
    fallback = Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


@register(PLUGIN_NAME, "dian-dian233", "每日签到插件：积分、连签、暴击、排行榜", PLUGIN_VERSION)
class CheckinPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = _resolve_data_dir()
        self.settings = self._load_settings()
        self.store = CheckinStore(self.data_dir / "checkin.db", self.settings)
        try:
            self.store.init_db()
        except Exception as exc:  # 不要让数据库问题把插件加载直接打死
            logger.error("[checkin] 初始化数据库失败: %s", exc)
        logger.info(
            "[checkin] 插件已加载 | db=%s | tz=%s | 积分=%s~%s | 暴击=%s",
            self.store.db_path,
            self.settings.timezone,
            self.settings.points_min,
            self.settings.points_max,
            "on" if self.settings.crit_enabled else "off",
        )

    # ------------------------------------------------------------ 配置 / 权限
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            return self.config.get(key, default)
        except AttributeError:
            pass
        try:
            return getattr(self.config, key, default)
        except Exception:
            return default

    def _load_settings(self) -> Settings:
        raw = {k: self._cfg(k) for k in SETTINGS_KEYS}
        raw = {k: v for k, v in raw.items() if v is not None}
        return Settings.from_config(raw).normalized()

    def _refresh_settings(self) -> None:
        """每次触发时同步一次配置，WebUI 改完无需重载插件。"""
        self.settings = self._load_settings()
        self.store.settings = self.settings
        self.store.tz = get_tz(self.settings.timezone)

    @property
    def _keyword_enabled(self) -> bool:
        return bool(self._cfg("keyword_enable", True))

    @property
    def _image_rank_enabled(self) -> bool:
        return bool(self._cfg("enable_image_rank", False))

    @property
    def _allow_private(self) -> bool:
        return bool(self._cfg("allow_private", False))

    def _group_allowed(self, scope: str) -> bool:
        whitelist = _as_str_list(self._cfg("group_whitelist"))
        blacklist = _as_str_list(self._cfg("group_blacklist"))
        if not whitelist and not blacklist:
            return True
        allowed = not whitelist or scope in whitelist
        if blacklist and scope in blacklist:
            return False
        return allowed

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """优先用 AstrBot 的 is_admin()，失败则回退查 admins_id 配置。"""
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                if checker():
                    return True
            except Exception:
                pass
        try:
            admins = self._cfg_from_context("admins_id")
            admin_ids = _as_str_list(admins) if not isinstance(admins, list) else [str(a) for a in admins]
            return str(event.get_sender_id()) in admin_ids
        except Exception:
            return False

    def _cfg_from_context(self, key: str) -> Any:
        try:
            cfg = self.context.get_config()
        except Exception:
            return None
        try:
            return cfg.get(key)
        except Exception:
            return None

    # ------------------------------------------------------------ 事件工具
    @staticmethod
    def _text(event: AstrMessageEvent) -> str:
        text = getattr(event, "message_str", None)
        if not text:
            msg_obj = getattr(event, "message_obj", None)
            text = getattr(msg_obj, "message_str", "")
        return (text or "").strip()

    @staticmethod
    def _scope(event: AstrMessageEvent) -> Optional[str]:
        """群聊用 group_id；私聊按 user 维度（需配置允许）返回 private:<uid>。"""
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if gid:
            return gid
        try:
            uid = str(event.get_sender_id())
        except Exception:
            return None
        return f"private:{uid}"

    def _args_after(self, event: AstrMessageEvent, command: str) -> str:
        text = self._text(event).lstrip("/／!！").strip()
        if text.startswith(command):
            text = text[len(command):]
        return text.strip()

    @staticmethod
    def _sender_meta(event: AstrMessageEvent) -> tuple[str, str]:
        try:
            uid = str(event.get_sender_id())
        except Exception:
            uid = "unknown"
        try:
            name = str(event.get_sender_name() or "") or uid
        except Exception:
            name = uid
        return uid, name

    @staticmethod
    def _mask(uid: str) -> str:
        """日志里不打印完整用户标识。"""
        uid = str(uid)
        if len(uid) <= 4:
            return "***"
        return f"{uid[:2]}***{uid[-2:]}"

    # ------------------------------------------------------------ 签到消息
    def _format_success(self, res, scope: str, name: str = "") -> str:
        """文案统一由 checkin_core 渲染，暴击行也在里面，方便配置与自测。"""
        text = format_checkin_reply(self.settings, res, name)
        try:
            total = self.store.day_count(scope)
            text += "\n🏁 今日已有 %d 人签到" % total
        except Exception:
            pass
        return text

    def _format_already(self, stats, scope: str) -> str:
        total = self.store.day_count(scope)
        rank = stats.today_rank or "-"
        return (
            "🌤️ 今天已经签到过啦，明天再来～\n"
            f"💰 今日获得 {stats.today_points} 分，排名 第 {rank} / {total} 位\n"
            f"🔥 连签 {stats.effective_streak} 天 | 📊 累计 {stats.total_points} 分"
        )

    async def _handle_checkin(self, event: AstrMessageEvent) -> Optional[str]:
        self._refresh_settings()
        scope = self._scope(event)
        if scope is None:
            return "签到失败：无法识别会话身份。"
        if not self._group_allowed(scope):
            return None
        is_private = scope.startswith("private:")
        if is_private and not self._allow_private:
            return "请在群聊里使用 /签到，或在插件配置中开启「允许私聊签到」。"

        uid, name = self._sender_meta(event)
        try:
            res = await asyncio.to_thread(self.store.checkin, scope, uid, name, None)
            return self._format_success(res, scope, name)
        except AlreadyCheckedIn:
            stats = await asyncio.to_thread(self.store.user_stats, scope, uid, None)
            logger.info("[checkin] 重复签到被拒 scope=%s user=%s", scope, self._mask(uid))
            return self._format_already(stats, scope)
        except CheckinError as exc:
            return f"签到失败：{exc}"
        except Exception as exc:
            logger.error("[checkin] 签到异常: %s", exc)
            return "签到失败，请稍后再试。"

    # ------------------------------------------------------------ 指令
    @filter.command("签到")
    async def cmd_checkin(self, event: AstrMessageEvent):
        text = await self._handle_checkin(event)
        if text:
            yield event.plain_result(text)

    @filter.command("我的签到")
    async def cmd_my_checkin(self, event: AstrMessageEvent):
        self._refresh_settings()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result("查询失败：无法识别会话身份。")
            return
        if not self._group_allowed(scope):
            return
        uid, name = self._sender_meta(event)
        stats = await asyncio.to_thread(self.store.user_stats, scope, uid, None)
        if stats.total_days == 0 and not stats.today_checked:
            yield event.plain_result("你还没有签到记录，发送 /签到 开始第一次吧～")
            return
        today_line = (
            f"今日已签（+{stats.today_points} 分，排名 第 {stats.today_rank}）"
            if stats.today_checked
            else "今日未签"
        )
        lines = [
            "📊 我的签到 — %s" % name,
            f"💰 累计积分：{stats.total_points}（全群累计 {self.store.global_total_points(uid)}）",
            f"🔥 当前连签：{stats.effective_streak} 天（最长 {stats.max_streak} 天）",
            f"📅 累计签到：{stats.total_days} 天",
            f"🕘 {today_line}",
        ]
        if stats.last_date:
            lines.append(f"🗓️ 最近签到：{stats.last_date}")
        yield event.plain_result("\n".join(lines))

    @filter.command("签到榜")
    async def cmd_leaderboard(self, event: AstrMessageEvent):
        self._refresh_settings()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result("查询失败：无法识别会话身份。")
            return
        if not self._group_allowed(scope):
            return

        args = self._args_after(event, "签到榜")
        try:
            board, board_scope, limit = parse_board_args(
                args,
                default_scope=self.settings.leaderboard_scope,
                default_limit=self.settings.leaderboard_limit,
            )
        except InvalidArgument as exc:
            yield event.plain_result(f"参数不合法：{exc}\n{BOARD_USAGE}")
            return

        try:
            result = await asyncio.to_thread(
                self.store.leaderboard, board, limit, scope, board_scope, None
            )
        except InvalidArgument as exc:
            yield event.plain_result(f"参数不合法：{exc}\n{BOARD_USAGE}")
            return
        except Exception as exc:
            logger.error("[checkin] 查询排行榜失败: %s", exc)
            yield event.plain_result("排行榜暂时查不出来，稍后再试试～")
            return

        if self._image_rank_enabled and not result.is_empty:
            path = await asyncio.to_thread(self._render_rank_image, result)
            if path is not None:
                yield event.image_result(str(path))
                return

        yield event.plain_result(format_board(result))

    @filter.command("签到重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("⛔ 只有管理员可以重置签到数据。")
            return
        scope = self._scope(event)
        if scope is None or scope.startswith("private:"):
            yield event.plain_result("请在群聊中使用 /签到重置。")
            return
        if "确认" not in self._args_after(event, "签到重置"):
            yield event.plain_result(
                "⚠️ 该操作会清空本群全部签到记录与积分，且不可恢复。\n"
                "确认请发送：/签到重置 确认"
            )
            return
        counts = await asyncio.to_thread(self.store.reset_group, scope)
        logger.info("[checkin] 管理员清空群 %s 签到数据: %s", scope, counts)
        yield event.plain_result(
            f"🧹 已清空本群签到数据：记录 {counts['records']} 条，用户 {counts['users']} 位。"
        )

    # ------------------------------------------------------------ 关键词触发
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_keyword(self, event: AstrMessageEvent):
        if not self._keyword_enabled:
            return
        raw = self._text(event)
        if not raw or raw.startswith(("/", "／", "!", "！")):
            return
        normalized = raw.replace(" ", "").replace("\u3000", "")
        if normalized not in ("签到", "打卡"):
            return
        text = await self._handle_checkin(event)
        if text:
            yield event.plain_result(text)

    # ------------------------------------------------------------ 图片排行榜
    def _render_rank_image(self, result):
        """可选图片榜单；缺字体或 Pillow 时返回 None 由调用方回退文本。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            logger.warning("[checkin] 未安装 Pillow，图片榜单回退为文本")
            return None

        font_path = self._find_cjk_font()
        if font_path is None:
            logger.warning("[checkin] 未找到中文字体，图片榜单回退为文本")
            return None

        try:
            rows = result.rows
            unit = BOARD_UNIT.get(result.board, "分")
            width, row_h = 640, 46
            height = 96 + row_h * max(1, len(rows))
            img = Image.new("RGB", (width, height), (250, 250, 252))
            draw = ImageDraw.Draw(img)
            title_font = ImageFont.truetype(font_path, 26)
            body_font = ImageFont.truetype(font_path, 20)
            draw.rectangle([(0, 0), (width, 64)], fill=(255, 143, 66))
            draw.text((24, 18), result.title, font=title_font, fill=(255, 255, 255))
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}
            for idx, row in enumerate(rows):
                y = 80 + idx * row_h
                name = (row.user_name or self._mask(row.user_id))[:16]
                prefix = medal.get(row.rank, f"{row.rank}.")
                draw.text((28, y), f"{prefix} {name}", font=body_font, fill=(40, 40, 46))
                text = f"{row.value} {unit}"
                box = draw.textbbox((0, 0), text, font=body_font)
                draw.text((width - 32 - (box[2] - box[0]), y), text,
                          font=body_font, fill=(232, 106, 51))
            out_dir = self.data_dir / "cache"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"rank_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.png"
            img.save(out_path)
            self._cleanup_cache(out_dir)
            return out_path
        except Exception as exc:
            logger.warning("[checkin] 生成图片榜单失败: %s", exc)
            return None

    @staticmethod
    def _find_cjk_font() -> Optional[str]:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
        for pattern in ("**/NotoSansCJK*", "**/wqy-*", "**/msyh*", "**/simhei*"):
            for base in ("/usr/share/fonts", "/usr/local/share/fonts"):
                try:
                    for hit in Path(base).glob(pattern):
                        return str(hit)
                except Exception:
                    continue
        return None

    @staticmethod
    def _cleanup_cache(cache_dir: Path, keep: int = 20) -> None:
        try:
            files = sorted(cache_dir.glob("rank_*.png"), key=lambda p: p.stat().st_mtime)
            for old in files[:-keep]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------ LLM 工具
    @llm_tool(name="checkin_my_status")
    async def llm_checkin_status(self, event: AstrMessageEvent, date: str = "") -> str:
        """查询当前用户在本群的签到状态：今天是否已签到、积分、连签天数、累计签到天数。

        Args:
            date (string): 查询哪一天的状态，只支持 "今天" / "昨天" / "YYYY-MM-DD"，留空表示今天。
        """
        self._refresh_settings()
        scope = self._scope(event)
        if scope is None:
            return "无法确定当前会话，请让用户在群聊中直接发送 /我的签到。"
        if not self._group_allowed(scope):
            return "本群未启用签到功能。"
        uid, _name = self._sender_meta(event)
        try:
            day = normalize_date(date, self.store.tz, None)
        except InvalidArgument as exc:
            return f"日期参数不合法（{exc}），只支持 今天 / 昨天 / YYYY-MM-DD。"
        today = self.store.today(None)
        if day > today:
            return "只能查询今天或过去的日期。"
        if day < today:
            return "暂不支持查询历史单日明细，请让用户发送 /我的签到 查看累计数据。"

        stats = await asyncio.to_thread(self.store.user_stats, scope, uid, None)
        if stats.today_checked:
            return (
                f"已签到。今天获得 {stats.today_points} 分，今日排名第 {stats.today_rank}，"
                f"连签 {stats.effective_streak} 天，累计 {stats.total_points} 分，"
                f"历史累计签到 {stats.total_days} 天。"
            )
        return (
            f"今天还没签到。当前连签 {stats.effective_streak} 天，"
            f"累计 {stats.total_points} 分，历史累计签到 {stats.total_days} 天。"
            f"提醒用户发送 /签到 即可签到。"
        )

    @llm_tool(name="checkin_leaderboard")
    async def llm_checkin_leaderboard(
        self, event: AstrMessageEvent, board: str = "today", limit: str = "10",
        scope: str = "group",
    ) -> str:
        """查询签到排行榜（今日榜 / 总榜 / 连签榜），默认本群前 10 名。

        Args:
            board (string): 榜单类型，只能是 today（今日榜）、total（总榜）、streak（连签榜），也接受 今日/总榜/连签。
            limit (int): 返回条数，1-10 之间，默认 10。
            scope (string): 榜单口径，group（本群，默认）或 global（全局汇总）。
        """
        self._refresh_settings()
        group_id = self._scope(event)
        if group_id is None:
            return "无法确定当前会话，请让用户在群聊中直接发送 /签到榜。"
        if not self._group_allowed(group_id):
            return "本群未启用签到功能。"
        try:
            board_key = normalize_board(board)
            board_scope = normalize_scope(scope, SCOPE_GROUP)
            top_n = clamp_limit(limit, default=self.settings.leaderboard_limit, hard_max=10)
        except InvalidArgument as exc:
            return (
                f"参数不合法（{exc}）。board 只能是 today/total/streak，"
                f"limit 只能是 1-10 的整数，scope 只能是 group/global。"
            )

        try:
            result = await asyncio.to_thread(
                self.store.leaderboard, board_key, top_n, group_id, board_scope, None
            )
        except Exception as exc:
            logger.error("[checkin] LLM 查询排行榜失败: %s", exc)
            return "排行榜暂时查不出来，请稍后再试。"

        if result.is_empty:
            return f"{result.title}：还没有相关记录。"
        unit = BOARD_UNIT.get(board_key, "分")
        body = "；".join(
            f"第{row.rank}名 {row.user_name or '匿名用户'} {row.value}{unit}"
            for row in result.rows
        )
        return f"{result.title}（共 {len(result.rows)} 条）：{body}"

    # ------------------------------------------------------------ 生命周期
    async def terminate(self):
        try:
            logger.info("[checkin] 插件已卸载")
        except Exception:
            pass
