"""签到插件的纯逻辑核心（不依赖 AstrBot，方便单元测试）。

设计要点：
- 防重复依赖数据库唯一索引 UNIQUE(group_id, user_id, date)，不做内存判重。
- "今天" 由配置时区（默认 Asia/Shanghai）计算，跨天不会算错。
- 所有写操作放在单条 BEGIN IMMEDIATE 事务里，失败即回滚。
- 积分公式：floor((基础分 + 连签加成) × 暴击倍率) + 首签奖励
- 暴击的随机数来源可注入（rng / seed），自测可复现。
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

DEFAULT_TIMEZONE = "Asia/Shanghai"
BOARD_TODAY = "today"
BOARD_TOTAL = "total"
BOARD_STREAK = "streak"
VALID_BOARDS = (BOARD_TODAY, BOARD_TOTAL, BOARD_STREAK)

SCOPE_GROUP = "group"
SCOPE_GLOBAL = "global"
VALID_SCOPES = (SCOPE_GROUP, SCOPE_GLOBAL)

DEFAULT_CRIT_MESSAGE = "💥 暴击 ×{multiplier}！本次 +{points} 分"

_BOARD_ALIASES = {
    "today": BOARD_TODAY,
    "今日": BOARD_TODAY,
    "今日榜": BOARD_TODAY,
    "日榜": BOARD_TODAY,
    "时间榜": BOARD_TODAY,
    "得分榜": BOARD_TODAY,
    "total": BOARD_TOTAL,
    "总": BOARD_TOTAL,
    "总榜": BOARD_TOTAL,
    "积分榜": BOARD_TOTAL,
    "总分榜": BOARD_TOTAL,
    "累计榜": BOARD_TOTAL,
    "streak": BOARD_STREAK,
    "连签": BOARD_STREAK,
    "连签榜": BOARD_STREAK,
    "连续签到榜": BOARD_STREAK,
    "连续榜": BOARD_STREAK,
}

_SCOPE_ALIASES = {
    "group": SCOPE_GROUP,
    "本群": SCOPE_GROUP,
    "群": SCOPE_GROUP,
    "群内": SCOPE_GROUP,
    "单群": SCOPE_GROUP,
    "global": SCOPE_GLOBAL,
    "全局": SCOPE_GLOBAL,
    "跨群": SCOPE_GLOBAL,
    "全服": SCOPE_GLOBAL,
    "总全局": SCOPE_GLOBAL,
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_ALIASES_TODAY = {"", "today", "今日", "今天", "现在"}
_DATE_ALIASES_YESTERDAY = {"yesterday", "昨日", "昨天"}

EMPTY_BOARD_TIPS = {
    BOARD_TODAY: "📭 今天还没人签到，榜是空的～ 快去抢第一！",
    BOARD_TOTAL: "📭 还没有累计积分，先发一次 /签到 吧～",
    BOARD_STREAK: "📭 还没有连签记录，今天签到就能上榜～",
}


class CheckinError(Exception):
    """签到业务异常基类。"""


class AlreadyCheckedIn(CheckinError):
    """今天已经签过到了（由数据库唯一索引兜底抛出）。"""

    def __init__(self, day: str) -> None:
        super().__init__(f"already checked in on {day}")
        self.day = day


class InvalidArgument(CheckinError):
    """参数非法（用于指令 / LLM 工具入参校验）。"""


def get_tz(name: Optional[str]):
    """解析时区；非法时回退到 UTC+8，避免插件启动失败。"""
    name = (name or DEFAULT_TIMEZONE).strip()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    if name in ("Asia/Shanghai", "PRC", "UTC+8", "GMT+8", ""):
        return timezone(timedelta(hours=8))
    return timezone.utc


def localize(now: Optional[datetime], tz) -> datetime:
    """统一把时间点转成目标时区的 aware datetime。"""
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


# ---------------------------------------------------------------- 暴击


@dataclass(frozen=True)
class CritTier:
    """一个暴击档位：chance 命中概率，multiplier 积分倍率。"""

    chance: float
    multiplier: float


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_crit_tiers(raw: Any) -> Tuple[CritTier, ...]:
    """解析多档暴击配置，支持多种写法，非法项直接丢弃。

    支持：
    - [{"chance": 0.05, "multiplier": 3}, {"chance": 0.01, "multiplier": 10}]
    - [[0.05, 3], [0.01, 10]]
    - '0.05:3,0.01:10'
    - '[{"chance":0.05,"multiplier":3}]'  （JSON 字符串）
    """
    if raw is None or raw == "":
        return ()
    items: Iterable[Any]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except Exception:
                raise InvalidArgument("crit_tiers 不是合法 JSON") from None
        else:
            items = []
            for chunk in text.replace("，", ",").split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                parts = re.split(r"[:：@x*×/]", chunk)
                if len(parts) != 2:
                    raise InvalidArgument("crit_tiers 需写成 chance:multiplier")
                items.append(parts)
    elif isinstance(raw, Mapping):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        raise InvalidArgument("crit_tiers 配置格式不支持")

    tiers = []
    for item in items:
        chance = multiplier = None
        if isinstance(item, Mapping):
            chance = _as_float(item.get("chance"))
            multiplier = _as_float(item.get("multiplier", item.get("mult")))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            chance = _as_float(item[0])
            multiplier = _as_float(item[1])
        elif isinstance(item, str):
            parts = re.split(r"[:：@x*×/]", item.strip())
            if len(parts) == 2:
                chance = _as_float(parts[0])
                multiplier = _as_float(parts[1])
        if chance is None or multiplier is None:
            continue
        if not (0.0 < chance <= 1.0):
            continue
        if multiplier <= 1.0:
            continue
        tiers.append(CritTier(round(chance, 6), round(multiplier, 6)))

    # 去重 + 按倍率从高到低（同倍率时概率低的先判）
    seen = set()
    unique = []
    for tier in tiers:
        key = (tier.chance, tier.multiplier)
        if key in seen:
            continue
        seen.add(key)
        unique.append(tier)
    unique.sort(key=lambda t: (-t.multiplier, t.chance))
    return tuple(unique)


# ---------------------------------------------------------------- 配置


@dataclass
class Settings:
    """插件可配置项（与 _conf_schema.json 一一对应）。"""

    points_min: int = 1
    points_max: int = 10
    first_checkin_bonus: int = 5
    streak_bonus_start: int = 3
    streak_bonus_per_day: float = 0.1
    streak_bonus_max: float = 1.0
    timezone: str = DEFAULT_TIMEZONE
    leaderboard_limit: int = 10
    leaderboard_scope: str = SCOPE_GROUP
    # 暴击
    crit_enabled: bool = True
    crit_chance: float = 0.05
    crit_multiplier: float = 3.0
    crit_tiers: Tuple[CritTier, ...] = ()
    crit_message: str = DEFAULT_CRIT_MESSAGE
    crit_seed: Optional[int] = None

    @classmethod
    def from_config(cls, cfg: Optional[Mapping[str, Any]]) -> "Settings":
        cfg = cfg or {}

        def _int(key: str, default: int) -> int:
            try:
                return int(cfg.get(key, default))
            except (TypeError, ValueError):
                return default

        def _float(key: str, default: float) -> float:
            try:
                return float(cfg.get(key, default))
            except (TypeError, ValueError):
                return default

        def _bool(key: str, default: bool) -> bool:
            value = cfg.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() not in ("", "0", "false", "no", "off")
            return bool(value)

        seed_raw = cfg.get("crit_seed", None)
        seed = None
        if seed_raw not in (None, "", "null"):
            try:
                seed = int(seed_raw)
            except (TypeError, ValueError):
                seed = None

        return cls(
            points_min=_int("points_min", 1),
            points_max=_int("points_max", 10),
            first_checkin_bonus=_int("first_checkin_bonus", 5),
            streak_bonus_start=_int("streak_bonus_start", 3),
            streak_bonus_per_day=_float("streak_bonus_per_day", 0.1),
            streak_bonus_max=_float("streak_bonus_max", 1.0),
            timezone=str(cfg.get("timezone") or DEFAULT_TIMEZONE),
            leaderboard_limit=_int("leaderboard_limit", 10),
            leaderboard_scope=str(cfg.get("leaderboard_scope") or SCOPE_GROUP),
            crit_enabled=_bool("crit_enabled", True),
            crit_chance=_float("crit_chance", 0.05),
            crit_multiplier=_float("crit_multiplier", 3.0),
            crit_tiers=parse_crit_tiers(cfg.get("crit_tiers")),
            crit_message=str(cfg.get("crit_message") or DEFAULT_CRIT_MESSAGE),
            crit_seed=seed,
        )

    def normalized(self) -> "Settings":
        """收敛到安全区间，防止配置写错（类型错、越界）把插件弄崩。"""
        lo = max(0, min(_safe_int(self.points_min, 1), 10**6))
        hi = max(0, min(_safe_int(self.points_max, 10), 10**6))
        if hi < lo:
            lo, hi = hi, lo
        scope = str(self.leaderboard_scope or SCOPE_GROUP).strip().lower()
        if scope not in VALID_SCOPES:
            scope = _SCOPE_ALIASES.get(scope, SCOPE_GROUP)
        return Settings(
            points_min=lo,
            points_max=hi,
            first_checkin_bonus=max(0, min(_safe_int(self.first_checkin_bonus, 0), 10**6)),
            streak_bonus_start=max(1, _safe_int(self.streak_bonus_start, 3)),
            streak_bonus_per_day=max(0.0, min(_safe_float(self.streak_bonus_per_day, 0.1), 10.0)),
            streak_bonus_max=max(0.0, min(_safe_float(self.streak_bonus_max, 1.0), 100.0)),
            timezone=self.timezone or DEFAULT_TIMEZONE,
            leaderboard_limit=max(1, min(_safe_int(self.leaderboard_limit, 10), 10)),
            leaderboard_scope=scope,
            crit_enabled=bool(self.crit_enabled),
            crit_chance=max(0.0, min(_safe_float(self.crit_chance, 0.05), 1.0)),
            crit_multiplier=max(1.0, min(_safe_float(self.crit_multiplier, 3.0), 1000.0)),
            crit_tiers=self.crit_tiers,
            crit_message=self.crit_message or DEFAULT_CRIT_MESSAGE,
            crit_seed=self.crit_seed,
        )

    # --- 连签加成 ---
    def streak_multiplier(self, streak: int) -> float:
        """第 n 天连签的加成系数。

        第 1 ~ (start-1) 天为 1.0；从 streak_bonus_start 起每天 +per_day，
        最大加成为 streak_bonus_max。
        """
        if streak <= 0:
            return 1.0
        if streak < self.streak_bonus_start:
            return 1.0
        extra = (streak - self.streak_bonus_start + 1) * self.streak_bonus_per_day
        return 1.0 + min(extra, self.streak_bonus_max)

    def streak_bonus_points(self, base_points: int, streak: int) -> int:
        """连签加成（绝对分数，参与暴击前相加）。"""
        return int(round(base_points * (self.streak_multiplier(streak) - 1.0)))

    # --- 暴击档位 ---
    def resolved_tiers(self) -> Tuple[CritTier, ...]:
        """最终生效的档位：显式多档优先，否则用 chance/multiplier 组单档。"""
        if not self.crit_enabled:
            return ()
        if self.crit_tiers:
            return self.crit_tiers
        if self.crit_chance <= 0.0 or self.crit_multiplier <= 1.0:
            return ()
        return (CritTier(self.crit_chance, self.crit_multiplier),)


@dataclass
class CritRoll:
    """一次暴击判定结果。"""

    multiplier: float = 1.0
    tier: Optional[CritTier] = None
    roll: float = 1.0

    @property
    def is_crit(self) -> bool:
        return self.tier is not None and self.multiplier > 1.0


def roll_crit(settings: Settings, rng: Any = None) -> CritRoll:
    """暴击判定：只掷一次随机数，多档从高到低判定，命中即止（不叠加）。"""
    tiers = settings.resolved_tiers()
    if not tiers:
        return CritRoll(multiplier=1.0, tier=None, roll=1.0)
    rng = rng or random
    value = float(rng.random())
    for tier in tiers:  # 已按倍率从高到低排序
        if value < tier.chance:
            return CritRoll(multiplier=tier.multiplier, tier=tier, roll=value)
    return CritRoll(multiplier=1.0, tier=None, roll=value)


# ---------------------------------------------------------------- 结果对象


@dataclass
class CheckinResult:
    group_id: str
    user_id: str
    day: str
    base_points: int
    streak_bonus: int
    first_bonus: int
    crit_multiplier: float
    points: int
    streak: int
    rank: int
    total_points: int
    is_first_daily: bool
    multiplier: float  # 兼容旧字段：连签加成系数

    @property
    def is_crit(self) -> bool:
        return self.crit_multiplier > 1.0

    @property
    def subtotal(self) -> int:
        """暴击前的（基础分 + 连签加成）。"""
        return self.base_points + self.streak_bonus


@dataclass
class UserStats:
    group_id: str
    user_id: str
    total_points: int = 0
    streak: int = 0
    max_streak: int = 0
    total_days: int = 0
    last_date: Optional[str] = None
    today_checked: bool = False
    today_points: int = 0
    today_rank: Optional[int] = None
    effective_streak: int = 0


@dataclass
class BoardRow:
    rank: int
    user_id: str
    user_name: str
    value: int
    extra: int
    group_id: str = ""


@dataclass
class BoardResult:
    board: str
    scope: str
    title: str
    rows: list  # list[BoardRow]
    limit: int = 10
    group_id: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.rows


# ---------------------------------------------------------------- 参数校验工具


def normalize_board(raw: Optional[str]) -> str:
    """把花式榜名归一化，非法直接抛 InvalidArgument。"""
    key = (raw or "").strip().lower()
    if not key:
        return BOARD_TODAY
    if key in _BOARD_ALIASES:
        return _BOARD_ALIASES[key]
    if key in VALID_BOARDS:
        return key
    raise InvalidArgument("不支持的榜单类型，仅支持 today / total / streak")


def normalize_scope(raw: Optional[str], default: str = SCOPE_GROUP) -> str:
    """榜单口径：本群 / 全局。"""
    key = (raw or "").strip().lower()
    if not key:
        return default if default in VALID_SCOPES else SCOPE_GROUP
    if key in VALID_SCOPES:
        return key
    if key in _SCOPE_ALIASES:
        return _SCOPE_ALIASES[key]
    raise InvalidArgument("不支持的榜单口径，仅支持 本群 / 全局")


def clamp_limit(raw: Any, default: int = 10, hard_max: int = 10) -> int:
    """排行榜条数：非数字直接报错，越界收敛到 [1, hard_max]。"""
    if raw is None or raw == "":
        return max(1, min(default, hard_max))
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise InvalidArgument("limit 必须是整数")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise InvalidArgument("limit 必须是整数") from None
    return max(1, min(value, hard_max))


def parse_board_args(raw: Optional[str], default_scope: str = SCOPE_GROUP,
                     default_limit: int = 10) -> Tuple[str, str, int]:
    """解析 `/签到榜 <榜单> [口径] [条数]` 的参数，顺序随意。"""
    board: Optional[str] = None
    scope: Optional[str] = None
    limit: Optional[int] = None
    tokens = str(raw or "").replace("　", " ").split()
    for token in tokens:
        text = token.strip()
        if not text:
            continue
        low = text.lower()
        if low.startswith("top") and low[3:].isdigit():
            limit = clamp_limit(low[3:], default_limit, 10)
            continue
        if text.isdigit():
            limit = clamp_limit(text, default_limit, 10)
            continue
        if board is None and (low in VALID_BOARDS or low in _BOARD_ALIASES):
            board = normalize_board(low)
            continue
        if scope is None and (low in VALID_SCOPES or low in _SCOPE_ALIASES):
            scope = normalize_scope(low)
            continue
        raise InvalidArgument(f"看不懂参数「{text}」，用法：/签到榜 [today|total|streak] [本群|全局] [top10]")
    return (
        board or BOARD_TODAY,
        scope or normalize_scope(None, default_scope),
        clamp_limit(limit, default_limit, 10),
    )


def normalize_date(raw: Optional[str], tz, now: Optional[datetime] = None) -> _date:
    """只接受 今天/昨天/YYYY-MM-DD，其余一律拒绝。"""
    text = (raw or "").strip().lower()
    now_local = localize(now, tz)
    if text in _DATE_ALIASES_TODAY:
        return now_local.date()
    if text in _DATE_ALIASES_YESTERDAY:
        return (now_local - timedelta(days=1)).date()
    if _DATE_RE.match(text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise InvalidArgument("日期不存在") from None
    raise InvalidArgument("日期格式仅支持 今天 / 昨天 / YYYY-MM-DD")


def fmt_num(value: float) -> str:
    """3.0 -> '3'，2.5 -> '2.5'。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return ("%.2f" % number).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- 存储层

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkin_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    user_name       TEXT    NOT NULL DEFAULT '',
    date            TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    points          INTEGER NOT NULL,
    base_points     INTEGER NOT NULL DEFAULT 0,
    streak_bonus    INTEGER NOT NULL DEFAULT 0,
    first_bonus     INTEGER NOT NULL DEFAULT 0,
    bonus_points    INTEGER NOT NULL DEFAULT 0,
    crit_multiplier REAL    NOT NULL DEFAULT 1.0,
    streak          INTEGER NOT NULL,
    is_first        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (group_id, user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_checkin_records_group_date
    ON checkin_records (group_id, date, points DESC);

CREATE INDEX IF NOT EXISTS idx_checkin_records_date
    ON checkin_records (date, points DESC);

CREATE TABLE IF NOT EXISTS checkin_users (
    group_id     TEXT    NOT NULL,
    user_id      TEXT    NOT NULL,
    user_name    TEXT    NOT NULL DEFAULT '',
    total_points INTEGER NOT NULL DEFAULT 0,
    streak       INTEGER NOT NULL DEFAULT 0,
    max_streak   INTEGER NOT NULL DEFAULT 0,
    total_days   INTEGER NOT NULL DEFAULT 0,
    crit_count   INTEGER NOT NULL DEFAULT 0,
    last_date    TEXT,
    updated_at   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_checkin_users_group_points
    ON checkin_users (group_id, total_points DESC);
"""

# 老库升级：缺列就补，保证升级插件不会炸
_MIGRATIONS = {
    "checkin_records": (
        ("streak_bonus", "INTEGER NOT NULL DEFAULT 0"),
        ("first_bonus", "INTEGER NOT NULL DEFAULT 0"),
        ("crit_multiplier", "REAL NOT NULL DEFAULT 1.0"),
    ),
    "checkin_users": (
        ("crit_count", "INTEGER NOT NULL DEFAULT 0"),
    ),
}


class CheckinStore:
    """SQLite 存储。方法均为同步实现，调用方用 asyncio.to_thread 包装。

    随机源可注入：`CheckinStore(db, settings, rng=random.Random(42))` 或
    `CheckinStore(db, settings, seed=42)`，自测可完全复现。
    """

    def __init__(self, db_path, settings: Settings, rng: Any = None,
                 seed: Optional[int] = None) -> None:
        self.db_path = Path(db_path)
        self.settings = settings.normalized()
        self.tz = get_tz(self.settings.timezone)
        if rng is not None:
            self.rng = rng
        elif seed is not None:
            self.rng = random.Random(seed)
        elif self.settings.crit_seed is not None:
            self.rng = random.Random(self.settings.crit_seed)
        else:
            self.rng = random

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            for table, columns in _MIGRATIONS.items():
                existing = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
                for name, ddl in columns:
                    if name not in existing:
                        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
            conn.commit()
        finally:
            conn.close()

    def today(self, now: Optional[datetime] = None) -> _date:
        return localize(now, self.tz).date()

    # --- 签到 ---
    def checkin(
        self,
        group_id: str,
        user_id: str,
        user_name: str = "",
        now: Optional[datetime] = None,
        rng: Any = None,
    ) -> CheckinResult:
        group_id, user_id = str(group_id), str(user_id)
        if not group_id or not user_id:
            raise InvalidArgument("group_id / user_id 不能为空")
        now_local = localize(now, self.tz)
        today_str = now_local.strftime("%Y-%m-%d")
        yesterday_str = (now_local.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        ts = int(now_local.timestamp())
        rng = rng or self.rng

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT streak, last_date FROM checkin_users "
                "WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            if row is not None and row["last_date"] == yesterday_str:
                streak = int(row["streak"]) + 1
            elif row is not None and row["last_date"] == today_str:
                conn.rollback()
                raise AlreadyCheckedIn(today_str)
            else:
                streak = 1

            base_points = int(rng.randint(self.settings.points_min, self.settings.points_max))
            streak_bonus = self.settings.streak_bonus_points(base_points, streak)
            crit = roll_crit(self.settings, rng)
            crit_multiplier = crit.multiplier

            # 计算顺序：floor((基础分 + 连签加成) × 暴击倍率)
            points = int(math.floor((base_points + streak_bonus) * crit_multiplier))

            first_cnt = conn.execute(
                "SELECT COUNT(1) FROM checkin_records WHERE group_id = ? AND date = ?",
                (group_id, today_str),
            ).fetchone()[0]
            is_first_daily = int(first_cnt) == 0
            first_bonus = self.settings.first_checkin_bonus if is_first_daily else 0

            # 首签奖励在暴击结算之后额外叠加（不计入暴击）
            points += first_bonus
            bonus_points = points - base_points

            try:
                conn.execute(
                    "INSERT INTO checkin_records "
                    "(group_id, user_id, user_name, date, ts, points, base_points,"
                    " streak_bonus, first_bonus, bonus_points, crit_multiplier,"
                    " streak, is_first) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        group_id,
                        user_id,
                        (user_name or "")[:64],
                        today_str,
                        ts,
                        points,
                        base_points,
                        streak_bonus,
                        first_bonus,
                        bonus_points,
                        crit_multiplier,
                        streak,
                        1 if is_first_daily else 0,
                    ),
                )
            except sqlite3.IntegrityError:
                # 唯一索引 (group_id, user_id, date) 兜底：并发/重复签到在此被拒
                conn.rollback()
                raise AlreadyCheckedIn(today_str) from None

            conn.execute(
                "INSERT INTO checkin_users "
                "(group_id, user_id, user_name, total_points, streak, max_streak,"
                " total_days, crit_count, last_date, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(group_id, user_id) DO UPDATE SET "
                "  total_points = total_points + excluded.total_points,"
                "  streak = excluded.streak,"
                "  max_streak = MAX(max_streak, excluded.max_streak),"
                "  total_days = total_days + 1,"
                "  crit_count = crit_count + excluded.crit_count,"
                "  last_date = excluded.last_date,"
                "  updated_at = excluded.updated_at,"
                "  user_name = CASE WHEN excluded.user_name = '' "
                "                   THEN checkin_users.user_name ELSE excluded.user_name END",
                (group_id, user_id, (user_name or "")[:64], points, streak, streak,
                 1 if crit.is_crit else 0, today_str, ts),
            )
            conn.commit()
        except CheckinError:
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        rank = self.today_rank(group_id, user_id, day=today_str)
        return CheckinResult(
            group_id=group_id,
            user_id=user_id,
            day=today_str,
            base_points=base_points,
            streak_bonus=streak_bonus,
            first_bonus=first_bonus,
            crit_multiplier=crit_multiplier,
            points=points,
            streak=streak,
            rank=rank,
            total_points=self.total_points(group_id, user_id),
            is_first_daily=is_first_daily,
            multiplier=self.settings.streak_multiplier(streak),
        )

    # --- 查询 ---
    def today_rank(self, group_id: str, user_id: str, day: Optional[str] = None) -> int:
        day = day or self.today().strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT points, ts FROM checkin_records "
                "WHERE group_id = ? AND user_id = ? AND date = ?",
                (str(group_id), str(user_id), day),
            ).fetchone()
            if row is None:
                return 0
            cnt = conn.execute(
                "SELECT COUNT(1) FROM checkin_records "
                "WHERE group_id = ? AND date = ? AND (points > ? OR (points = ? AND ts < ?))",
                (str(group_id), day, row["points"], row["points"], row["ts"]),
            ).fetchone()[0]
            return int(cnt) + 1
        finally:
            conn.close()

    def total_points(self, group_id: str, user_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT total_points FROM checkin_users WHERE group_id = ? AND user_id = ?",
                (str(group_id), str(user_id)),
            ).fetchone()
            return int(row["total_points"]) if row else 0
        finally:
            conn.close()

    def global_total_points(self, user_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_points), 0) FROM checkin_users WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def user_stats(self, group_id: str, user_id: str,
                   now: Optional[datetime] = None) -> UserStats:
        group_id, user_id = str(group_id), str(user_id)
        now_local = localize(now, self.tz)
        today_str = now_local.strftime("%Y-%m-%d")
        yesterday_str = (now_local.date() - timedelta(days=1)).strftime("%Y-%m-%d")

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT total_points, streak, max_streak, total_days, last_date "
                "FROM checkin_users WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            today_row = conn.execute(
                "SELECT points FROM checkin_records "
                "WHERE group_id = ? AND user_id = ? AND date = ?",
                (group_id, user_id, today_str),
            ).fetchone()
        finally:
            conn.close()

        stats = UserStats(group_id=group_id, user_id=user_id)
        if row is not None:
            stats.total_points = int(row["total_points"])
            stats.streak = int(row["streak"])
            stats.max_streak = int(row["max_streak"])
            stats.total_days = int(row["total_days"])
            stats.last_date = row["last_date"]
            # 断签后展示的连签归零（历史最大连签单独保留）
            if stats.last_date in (today_str, yesterday_str):
                stats.effective_streak = stats.streak
            else:
                stats.effective_streak = 0
        if today_row is not None:
            stats.today_checked = True
            stats.today_points = int(today_row["points"])
            stats.today_rank = self.today_rank(group_id, user_id, day=today_str)
        return stats

    # --- 排行榜 ---
    def board_title(self, board: str, scope: str = SCOPE_GROUP,
                    now: Optional[datetime] = None) -> str:
        board = normalize_board(board)
        scope = normalize_scope(scope)
        suffix = "（全群汇总）" if scope == SCOPE_GLOBAL else ""
        if board == BOARD_TODAY:
            return "📅 今日签到榜 %s%s" % (self.today(now).strftime("%Y-%m-%d"), suffix)
        if board == BOARD_TOTAL:
            return "💰 签到总榜（累计积分）%s" % suffix
        return "🔥 连签榜（连续签到天数）%s" % suffix

    def leaderboard(self, board: str = BOARD_TODAY, limit: int = 10,
                    group_id: Optional[str] = None, scope: Optional[str] = None,
                    now: Optional[datetime] = None) -> BoardResult:
        """返回榜单（名次同分同名次，最多 limit 行）。"""
        board = normalize_board(board)
        scope = normalize_scope(scope, self.settings.leaderboard_scope)
        limit = clamp_limit(limit, self.settings.leaderboard_limit, 10)
        group_id = str(group_id or "")

        if scope == SCOPE_GROUP and not group_id:
            raise InvalidArgument("本群榜单需要群号，请在群聊中使用")

        now_local = localize(now, self.tz)
        today_str = now_local.strftime("%Y-%m-%d")
        yesterday_str = (now_local.date() - timedelta(days=1)).strftime("%Y-%m-%d")

        conn = self._connect()
        try:
            if board == BOARD_TODAY:
                if scope == SCOPE_GLOBAL:
                    rows = conn.execute(
                        "SELECT user_id, MAX(user_name) AS user_name, SUM(points) AS value,"
                        " MAX(streak) AS extra, MIN(ts) AS ts, '' AS group_id "
                        "FROM checkin_records WHERE date = ? GROUP BY user_id "
                        "ORDER BY value DESC, ts ASC LIMIT ?",
                        (today_str, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT user_id, user_name, points AS value, streak AS extra,"
                        " ts, group_id FROM checkin_records "
                        "WHERE group_id = ? AND date = ? "
                        "ORDER BY points DESC, ts ASC LIMIT ?",
                        (group_id, today_str, limit),
                    ).fetchall()
            elif board == BOARD_TOTAL:
                if scope == SCOPE_GLOBAL:
                    rows = conn.execute(
                        "SELECT user_id, SUM(total_points) AS value, SUM(total_days) AS extra,"
                        " (SELECT u.user_name FROM checkin_users u WHERE u.user_id = c.user_id"
                        "  ORDER BY u.updated_at DESC, u.group_id ASC LIMIT 1) AS user_name,"
                        " MAX(updated_at) AS ts, '' AS group_id "
                        "FROM checkin_users c GROUP BY user_id "
                        "ORDER BY value DESC, ts ASC LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT user_id, user_name, total_points AS value,"
                        " total_days AS extra, updated_at AS ts, group_id "
                        "FROM checkin_users WHERE group_id = ? "
                        "ORDER BY total_points DESC, updated_at ASC LIMIT ?",
                        (group_id, limit),
                    ).fetchall()
            else:  # streak（断签的不上榜）
                if scope == SCOPE_GLOBAL:
                    rows = conn.execute(
                        "SELECT user_id, MAX(CASE WHEN last_date IN (?, ?) THEN streak ELSE 0 END)"
                        " AS value, SUM(total_points) AS extra,"
                        " (SELECT u.user_name FROM checkin_users u WHERE u.user_id = c.user_id"
                        "  ORDER BY u.updated_at DESC, u.group_id ASC LIMIT 1) AS user_name,"
                        " MAX(updated_at) AS ts, '' AS group_id "
                        "FROM checkin_users c GROUP BY user_id HAVING value > 0 "
                        "ORDER BY value DESC, extra DESC LIMIT ?",
                        (today_str, yesterday_str, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT user_id, user_name, streak AS value, total_points AS extra,"
                        " updated_at AS ts, group_id FROM checkin_users "
                        "WHERE group_id = ? AND last_date IN (?, ?) AND streak > 0 "
                        "ORDER BY streak DESC, total_points DESC LIMIT ?",
                        (group_id, today_str, yesterday_str, limit),
                    ).fetchall()
        finally:
            conn.close()

        board_rows = self._assign_ranks(
            [
                {
                    "user_id": row["user_id"],
                    "user_name": row["user_name"] or "",
                    "value": int(row["value"] or 0),
                    "extra": int(row["extra"] or 0),
                    "group_id": row["group_id"] or "",
                }
                for row in rows
            ]
        )
        return BoardResult(
            board=board,
            scope=scope,
            title=self.board_title(board, scope, now),
            rows=board_rows,
            limit=limit,
            group_id=group_id,
        )

    @staticmethod
    def _assign_ranks(items: Sequence[Mapping[str, Any]]) -> list:
        """并列名次：同分同名次，下一名次跳过（1,2,2,4）。"""
        ranked = []
        prev_value = None
        rank = 0
        for index, item in enumerate(items):
            value = item.get("value")
            if prev_value is None or value != prev_value:
                rank = index + 1
                prev_value = value
            ranked.append(BoardRow(rank=rank, **dict(item)))
        return ranked

    # --- 管理 ---
    def reset_group(self, group_id: str) -> dict:
        """清空某群所有签到数据（仅管理员指令调用）。"""
        group_id = str(group_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rec = conn.execute("DELETE FROM checkin_records WHERE group_id = ?",
                               (group_id,)).rowcount
            usr = conn.execute("DELETE FROM checkin_users WHERE group_id = ?",
                               (group_id,)).rowcount
            conn.commit()
            return {"records": int(rec), "users": int(usr)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def purge_date(self, group_id: str, day: str) -> int:
        """删除某群某天记录（运维/测试用）。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cnt = conn.execute("DELETE FROM checkin_records WHERE group_id = ? AND date = ?",
                               (str(group_id), str(day))).rowcount
            conn.commit()
            return int(cnt)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def count_records(self, group_id: Optional[str] = None) -> int:
        conn = self._connect()
        try:
            if group_id is None:
                return int(conn.execute("SELECT COUNT(1) FROM checkin_records").fetchone()[0])
            return int(conn.execute("SELECT COUNT(1) FROM checkin_records WHERE group_id = ?",
                                    (str(group_id),)).fetchone()[0])
        finally:
            conn.close()

    def day_count(self, group_id: str, now: Optional[datetime] = None) -> int:
        """某群当天已签到人数。"""
        day = self.today(now).strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(1) FROM checkin_records WHERE group_id = ? AND date = ?",
                    (str(group_id), day),
                ).fetchone()[0]
            )
        finally:
            conn.close()


# ---------------------------------------------------------------- 文案渲染


class _SafeDict(dict):
    def __missing__(self, key):  # 未知占位符原样保留，不让插件崩
        return "{" + str(key) + "}"


def render_template(template: str, values: Mapping[str, Any]) -> str:
    try:
        return str(template).format_map(_SafeDict(values))
    except Exception:
        return str(template)


def render_crit_line(settings: Settings, result: CheckinResult) -> str:
    """暴击文案（模板可配，占位符见 README）。"""
    return render_template(
        settings.crit_message,
        {
            "multiplier": fmt_num(result.crit_multiplier),
            "points": result.points,
            "base": result.base_points,
            "streak_bonus": result.streak_bonus,
            "subtotal": result.subtotal,
            "streak": result.streak,
        },
    )


def format_checkin_reply(settings: Settings, result: CheckinResult,
                         display_name: str = "") -> str:
    """签到回执（纯文本，方便单测）。"""
    lines = []
    name = display_name or result.user_id
    lines.append("✅ %s 签到成功！%s" % (name, result.day))
    if result.first_bonus:
        lines.append("🎁 今日首签奖励 +%d 分" % result.first_bonus)
    calc = "🎯 基础 %d" % result.base_points
    if result.streak_bonus:
        calc += " + 连签加成 %d" % result.streak_bonus
    calc += " = %d" % result.subtotal
    lines.append(calc)
    if result.is_crit:
        lines.append(render_crit_line(settings, result))  # 暴击行已含本次得分
    else:
        lines.append("✨ 本次 +%d 分" % result.points)
    tail = ["🔥 连签 %d 天" % result.streak, "📈 累计 %d 分" % result.total_points]
    if result.rank:
        tail.append("🏅 今日第 %d 名" % result.rank)
    lines.append(" | ".join(tail))
    return "\n".join(lines)


def format_board(result: BoardResult, settings: Optional[Settings] = None) -> str:
    """榜单纯文本：空榜给友好提示，不抛异常。"""
    if result.is_empty:
        return "%s\n%s" % (result.title, EMPTY_BOARD_TIPS.get(result.board, "📭 榜单还是空的～"))
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [result.title]
    for row in result.rows:
        name = row.user_name or row.user_id
        prefix = medals.get(row.rank, "%d." % row.rank)
        if result.board == BOARD_TODAY:
            detail = "%d 分（连签 %d 天）" % (row.value, row.extra)
        elif result.board == BOARD_TOTAL:
            detail = "%d 分（签到 %d 天）" % (row.value, row.extra)
        else:
            detail = "连签 %d 天（累计 %d 分）" % (row.value, row.extra)
        if result.scope == SCOPE_GLOBAL and row.group_id:
            detail += " · 群 %s" % row.group_id
        lines.append("%s %s — %s" % (prefix, name, detail))
    lines.append("— 共 %d 人上榜 · 同分同名次" % len(result.rows))
    return "\n".join(lines)
