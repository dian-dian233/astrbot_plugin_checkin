"""checkin_core 的单元测试（不依赖 AstrBot / pytest，用标准库 unittest）。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkin_core import (  # noqa: E402
    BOARD_STREAK,
    BOARD_TODAY,
    BOARD_TOTAL,
    SCOPE_GLOBAL,
    SCOPE_GROUP,
    AlreadyCheckedIn,
    CheckinStore,
    InvalidArgument,
    Settings,
    parse_board_args,
    format_board,
    format_checkin_reply,
    normalize_date,
    parse_crit_tiers,
    render_crit_line,
    roll_crit,
)

UTC8 = timezone(timedelta(hours=8))


class SeqRng:
    """可控随机源：random() 按序返回给定值，randint() 固定返回 int_value。"""

    def __init__(self, randoms, int_value=5):
        self._randoms = list(randoms) or [0.5]
        self._idx = 0
        self.int_value = int_value

    def random(self):
        value = self._randoms[self._idx % len(self._randoms)]
        self._idx += 1
        return value

    def randint(self, low, high):
        return max(low, min(self.int_value, high))


def make_settings(**kwargs):
    base = dict(
        points_min=1,
        points_max=10,
        first_checkin_bonus=0,
        streak_bonus_start=1,
        streak_bonus_per_day=0.2,
        streak_bonus_max=1.0,
        timezone="Asia/Shanghai",
        leaderboard_limit=10,
        crit_enabled=True,
        crit_chance=0.05,
        crit_multiplier=3.0,
    )
    base.update(kwargs)
    return Settings(**base).normalized()


def _rmtree_files(tmp):
    for name in os.listdir(tmp):
        path = os.path.join(tmp, name)
        if os.path.isfile(path):
            os.remove(path)
    os.rmdir(tmp)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="checkin_test_")
        self.db_path = os.path.join(self.tmpdir, "checkin.db")

    def tearDown(self):
        _rmtree_files(self.tmpdir)

    def new_store(self, rng=None, seed=None, **kwargs):
        store = CheckinStore(self.db_path, make_settings(**kwargs), rng=rng, seed=seed)
        store.init_db()
        return store


# ------------------------------------------------------------------ 暴击判定


class TestCrit(unittest.TestCase):
    def test_chance_zero_never_crits(self):
        """chance=0 时无论随机数怎么取都不暴击。"""
        settings = make_settings(crit_chance=0.0)
        self.assertEqual(settings.resolved_tiers(), ())
        rng = random.Random(20261027)
        for _ in range(2000):
            self.assertEqual(roll_crit(settings, rng).multiplier, 1.0)

    def test_chance_one_always_crits(self):
        """chance=1 时必定暴击。"""
        settings = make_settings(crit_chance=1.0, crit_multiplier=3.0)
        rng = random.Random(20261027)
        for _ in range(2000):
            roll = roll_crit(settings, rng)
            self.assertTrue(roll.is_crit)
            self.assertEqual(roll.multiplier, 3.0)

    def test_disabled_never_crits_even_with_chance_one(self):
        settings = make_settings(crit_enabled=False, crit_chance=1.0)
        self.assertEqual(settings.resolved_tiers(), ())
        for _ in range(200):
            self.assertEqual(roll_crit(settings, random.Random(7)).multiplier, 1.0)

    def test_multi_tier_no_stacking(self):
        """多档只取命中那一档的倍率，绝不叠乘（3 × 10 → 不会变成 30）。"""
        settings = make_settings(
            crit_tiers=parse_crit_tiers(
                [{"chance": 0.05, "multiplier": 3}, {"chance": 0.01, "multiplier": 10}]
            ),
        )
        self.assertEqual([t.multiplier for t in settings.resolved_tiers()], [10.0, 3.0])
        self.assertEqual(roll_crit(settings, SeqRng([0.005])).multiplier, 10.0)  # 高档
        self.assertEqual(roll_crit(settings, SeqRng([0.03])).multiplier, 3.0)    # 低档
        self.assertEqual(roll_crit(settings, SeqRng([0.05])).multiplier, 1.0)    # 边界不算命中
        self.assertEqual(roll_crit(settings, SeqRng([0.99])).multiplier, 1.0)

    def test_multi_tier_statistics_match_configured_chances(self):
        settings = make_settings(crit_tiers=parse_crit_tiers([[0.05, 3], [0.01, 10]]))
        rng = random.Random(424242)
        rounds = 40000
        counts = {1.0: 0, 3.0: 0, 10.0: 0}
        for _ in range(rounds):
            counts[roll_crit(settings, rng).multiplier] += 1
        self.assertEqual(set(counts), {1.0, 3.0, 10.0})  # 只出现单档倍率，无叠加值
        self.assertAlmostEqual(counts[10.0] / rounds, 0.01, delta=0.003)
        self.assertAlmostEqual(counts[3.0] / rounds, 0.04, delta=0.005)  # 0.05 - 0.01
        self.assertAlmostEqual(counts[1.0] / rounds, 0.95, delta=0.006)

    def test_crit_tiers_parsing_variants(self):
        expected = [(0.01, 10.0), (0.05, 3.0)]  # 解析后按倍率从高到低排序
        for raw in (
            '[{"chance": 0.05, "multiplier": 3}, {"chance": 0.01, "multiplier": 10}]',
            [[0.05, 3], [0.01, 10]],
            "0.05:3,0.01:10",
            ["0.05x3", "0.01x10"],
        ):
            tiers = parse_crit_tiers(raw)
            self.assertEqual([(t.chance, t.multiplier) for t in tiers], expected, raw)

    def test_invalid_tiers_fall_back_to_single_tier(self):
        settings = make_settings(
            crit_chance=0.2,
            crit_multiplier=2.0,
            crit_tiers=parse_crit_tiers([{"chance": 0, "multiplier": 1}]),
        )
        tiers = settings.resolved_tiers()
        self.assertEqual(len(tiers), 1)
        self.assertEqual((tiers[0].chance, tiers[0].multiplier), (0.2, 2.0))

    def test_seed_makes_results_reproducible(self):
        """同一种子 → 完全相同的签到结果（含暴击）。"""
        def run():
            tmp = tempfile.mkdtemp(prefix="checkin_seed_")
            try:
                store = CheckinStore(os.path.join(tmp, "c.db"), make_settings(), seed=1234)
                store.init_db()
                return [
                    store.checkin("g", "u%d" % i, now=datetime(2026, 10, 27, 9, 0, tzinfo=UTC8))
                    for i in range(5)
                ]
            finally:
                _rmtree_files(tmp)

        a, b = run(), run()
        self.assertEqual([r.points for r in a], [r.points for r in b])
        self.assertEqual([r.crit_multiplier for r in a], [r.crit_multiplier for r in b])


# ------------------------------------------------------------------ 积分公式


class TestPoints(StoreCase):
    def test_formula_floor_base_plus_streak_times_crit(self):
        store = self.new_store(rng=SeqRng([0.0], int_value=5))  # 必暴击 ×3
        result = store.checkin("g1", "u1", "阿一")
        self.assertEqual(result.base_points, 5)
        self.assertEqual(result.streak_bonus, 1)  # round(5 * 0.2)
        self.assertEqual(result.subtotal, 6)
        self.assertEqual(result.crit_multiplier, 3.0)
        self.assertEqual(result.points, int(math.floor((5 + 1) * 3)))
        self.assertEqual(result.points, 18)

    def test_formula_without_crit(self):
        store = self.new_store(rng=SeqRng([0.99], int_value=7))
        result = store.checkin("g1", "u1")
        self.assertEqual(result.crit_multiplier, 1.0)
        self.assertEqual(result.points, 7 + round(7 * 0.2))  # 7 + 1 = 8

    def test_multi_tier_crit_end_to_end(self):
        store = self.new_store(
            rng=SeqRng([0.005], int_value=5),
            crit_tiers=parse_crit_tiers([[0.05, 3], [0.01, 10]]),
        )
        result = store.checkin("g1", "u1")
        self.assertEqual(result.crit_multiplier, 10.0)  # 只取高档
        self.assertEqual(result.points, (5 + 1) * 10)   # 不是 6*30

    def test_first_bonus_added_after_crit(self):
        store = self.new_store(rng=SeqRng([0.0], int_value=5), first_checkin_bonus=5)
        result = store.checkin("g1", "u1")
        self.assertTrue(result.is_first_daily)
        self.assertEqual(result.first_bonus, 5)
        self.assertEqual(result.points, 18 + 5)
        second = store.checkin("g1", "u2")
        self.assertFalse(second.is_first_daily)
        self.assertEqual(second.first_bonus, 0)

    def test_points_accumulate_per_group_and_user(self):
        store = self.new_store(rng=SeqRng([0.99], int_value=4))
        day1 = datetime(2026, 10, 20, 10, 0, tzinfo=UTC8)
        store.checkin("g1", "u1", now=day1)                      # 4 + 1 = 5
        store.checkin("g1", "u1", now=day1 + timedelta(days=1))  # 4 + 2 = 6
        self.assertEqual(store.total_points("g1", "u1"), 11)
        store.checkin("g2", "u1", now=day1)                      # 另一个群独立累计
        self.assertEqual(store.total_points("g2", "u1"), 5)
        self.assertEqual(store.global_total_points("u1"), 16)


# ------------------------------------------------------------------ 去重 / 连签 / 时区


class TestDuplicateStreakTimezone(StoreCase):
    def test_duplicate_same_day_rejected_by_unique_index(self):
        store = self.new_store(rng=SeqRng([0.99], int_value=5))
        day = datetime(2026, 10, 27, 9, 0, tzinfo=UTC8)
        store.checkin("g1", "u1", now=day)
        with self.assertRaises(AlreadyCheckedIn):
            store.checkin("g1", "u1", now=day + timedelta(hours=3))
        self.assertEqual(store.count_records("g1"), 1)
        store.checkin("g2", "u1", now=day)  # 换个群可以签
        self.assertEqual(store.count_records("g2"), 1)

    def test_streak_increment_and_break(self):
        store = self.new_store(rng=SeqRng([0.99], int_value=5))
        day = datetime(2026, 10, 27, 9, 0, tzinfo=UTC8)
        self.assertEqual(store.checkin("g1", "u1", now=day).streak, 1)
        self.assertEqual(store.checkin("g1", "u1", now=day + timedelta(days=1)).streak, 2)
        self.assertEqual(store.checkin("g1", "u1", now=day + timedelta(days=2)).streak, 3)
        self.assertEqual(store.checkin("g1", "u1", now=day + timedelta(days=4)).streak, 1)

    def test_asia_shanghai_day_boundary(self):
        store = self.new_store(rng=SeqRng([0.99], int_value=5))
        late = datetime(2026, 10, 26, 23, 59, 59, tzinfo=UTC8)
        first = store.checkin("g1", "u1", now=late)
        self.assertEqual(first.day, "2026-10-26")
        second = store.checkin("g1", "u1", now=late + timedelta(minutes=1))
        self.assertEqual(second.day, "2026-10-27")
        self.assertEqual(second.streak, 2)

    def test_utc_input_is_localized(self):
        store = self.new_store(rng=SeqRng([0.99], int_value=5))
        utc_moment = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)  # == 次日 00:00 +08
        self.assertEqual(store.checkin("g1", "u1", now=utc_moment).day, "2026-10-27")


# ------------------------------------------------------------------ 排行榜


class TestLeaderboard(StoreCase):
    def _seed_users(self, store, spec, now):
        for user_id, points in spec:
            store.checkin("g1", user_id, "昵称" + user_id,
                          now=now, rng=SeqRng([0.99], int_value=points))

    def test_today_board_and_ties(self):
        store = self.new_store(crit_chance=0.0, points_min=1, points_max=20)
        now = datetime(2026, 10, 27, 9, 0, tzinfo=UTC8)
        self._seed_users(store, [("a", 10), ("b", 10), ("c", 5)], now)
        board = store.leaderboard(BOARD_TODAY, group_id="g1", now=now)
        self.assertEqual([row.rank for row in board.rows], [1, 1, 3])  # 同分同名次
        # 单日得分含连签加成（streak1 = +20%）：10→12，5→6
        self.assertEqual([row.value for row in board.rows], [12, 12, 6])
        text = format_board(board)
        self.assertIn("今日签到榜", text)
        self.assertIn("🥇", text)
        self.assertIn("同分同名次", text)

    def test_total_and_streak_boards(self):
        store = self.new_store(crit_chance=0.0, points_min=1, points_max=20)
        day1 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC8)
        self._seed_users(store, [("a", 10), ("b", 3)], day1)
        self._seed_users(store, [("a", 10), ("b", 3)], day1 + timedelta(days=1))

        total = store.leaderboard(BOARD_TOTAL, group_id="g1", now=day1 + timedelta(days=1))
        self.assertEqual(total.rows[0].user_id, "a")
        self.assertGreater(total.rows[0].value, total.rows[1].value)
        self.assertIn("总榜", total.title)

        streak = store.leaderboard(BOARD_STREAK, group_id="g1", now=day1 + timedelta(days=1))
        self.assertEqual([row.value for row in streak.rows], [2, 2])
        self.assertEqual([row.rank for row in streak.rows], [1, 1])
        self.assertIn("连签榜", streak.title)

    def test_empty_board_is_friendly_not_error(self):
        store = self.new_store()
        for board in (BOARD_TODAY, BOARD_TOTAL, BOARD_STREAK):
            result = store.leaderboard(board, group_id="g1")
            self.assertTrue(result.is_empty)
            text = format_board(result)
            self.assertIn("📭", text)

    def test_streak_board_excludes_broken_streak(self):
        store = self.new_store(crit_chance=0.0, points_min=1, points_max=20)
        day1 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC8)
        store.checkin("g1", "u1", now=day1, rng=SeqRng([0.99], int_value=5))
        later = day1 + timedelta(days=3)  # 已断签
        self.assertTrue(store.leaderboard(BOARD_STREAK, group_id="g1", now=later).is_empty)
        self.assertEqual(store.leaderboard(BOARD_TOTAL, group_id="g1", now=later).rows[0].value, 6)

    def test_global_scope_aggregates_across_groups(self):
        store = self.new_store(crit_chance=0.0, points_min=1, points_max=20)
        now = datetime(2026, 10, 27, 9, 0, tzinfo=UTC8)
        store.checkin("g1", "u1", now=now, rng=SeqRng([0.99], int_value=6))
        store.checkin("g2", "u1", now=now, rng=SeqRng([0.99], int_value=4))
        store.checkin("g2", "u2", now=now, rng=SeqRng([0.99], int_value=9))

        per_group = store.leaderboard(BOARD_TOTAL, group_id="g1", scope=SCOPE_GROUP, now=now)
        self.assertEqual([row.value for row in per_group.rows], [7])

        overall = store.leaderboard(BOARD_TOTAL, scope=SCOPE_GLOBAL, now=now)
        self.assertEqual(overall.scope, SCOPE_GLOBAL)
        self.assertEqual(overall.rows[0].user_id, "u1")
        self.assertEqual(overall.rows[0].value, 12)  # 7 + 5
        self.assertEqual(overall.rows[1].value, 11)  # 9 + 2
        self.assertIn("全群汇总", overall.title)

    def test_group_scope_requires_group_id(self):
        store = self.new_store()
        with self.assertRaises(InvalidArgument):
            store.leaderboard(BOARD_TOTAL, scope=SCOPE_GROUP, group_id="")

    def test_limit_clamped_to_ten(self):
        store = self.new_store(crit_chance=0.0, points_min=1, points_max=20)
        now = datetime(2026, 10, 27, 9, 0, tzinfo=UTC8)
        self._seed_users(store, [("u%02d" % i, 20 - (i % 19)) for i in range(30)], now)
        board = store.leaderboard(BOARD_TODAY, group_id="g1", limit=99, now=now)
        self.assertEqual(len(board.rows), 10)

    def test_default_scope_from_config(self):
        store = self.new_store(crit_chance=0.0, leaderboard_scope=SCOPE_GLOBAL,
                               points_min=1, points_max=20)
        now = datetime(2026, 10, 27, 9, 0, tzinfo=UTC8)
        store.checkin("g1", "u1", now=now, rng=SeqRng([0.99], int_value=6))
        self.assertEqual(store.leaderboard(BOARD_TODAY, now=now).scope, SCOPE_GLOBAL)


# ------------------------------------------------------------------ 参数 / 文案 / 管理


class TestArgsAndText(StoreCase):
    def test_parse_board_args(self):
        self.assertEqual(parse_board_args("today"), (BOARD_TODAY, SCOPE_GROUP, 10))
        self.assertEqual(parse_board_args("total 全局"), (BOARD_TOTAL, SCOPE_GLOBAL, 10))
        self.assertEqual(parse_board_args("全局 streak top5"), (BOARD_STREAK, SCOPE_GLOBAL, 5))
        self.assertEqual(parse_board_args("连签榜", SCOPE_GLOBAL), (BOARD_STREAK, SCOPE_GLOBAL, 10))
        self.assertEqual(parse_board_args(None)[0], BOARD_TODAY)
        self.assertEqual(parse_board_args("total 全局 3"), (BOARD_TOTAL, SCOPE_GLOBAL, 3))
        with self.assertRaises(InvalidArgument):
            parse_board_args("今日 火星")
        with self.assertRaises(InvalidArgument):
            parse_board_args("top一百")

    def test_normalize_date(self):
        tz = UTC8
        now = datetime(2026, 10, 27, 8, 0, tzinfo=tz)
        self.assertEqual(str(normalize_date("今天", tz, now)), "2026-10-27")
        self.assertEqual(str(normalize_date("昨天", tz, now)), "2026-10-26")
        self.assertEqual(str(normalize_date("2026-01-02", tz, now)), "2026-01-02")
        with self.assertRaises(InvalidArgument):
            normalize_date("2026-13-01", tz, now)

    def test_crit_message_is_configurable(self):
        settings = make_settings(crit_message="⚡{multiplier}倍暴击！{subtotal}→{points}")
        store = CheckinStore(self.db_path, settings, rng=SeqRng([0.0], int_value=5))
        store.init_db()
        result = store.checkin("g1", "u1")
        self.assertEqual(render_crit_line(settings, result), "⚡3倍暴击！6→18")
        reply = format_checkin_reply(settings, result, "阿一")
        self.assertIn("⚡3倍暴击", reply)
        self.assertIn("累计 18 分", reply)

    def test_default_crit_message(self):
        settings = make_settings()
        store = CheckinStore(self.db_path, settings, rng=SeqRng([0.0], int_value=10))
        store.init_db()
        result = store.checkin("g1", "u1")
        line = render_crit_line(settings, result)
        self.assertIn("💥 暴击 ×3！本次 +", line)
        self.assertIn("36", line)  # floor((10 + 2) * 3)

    def test_no_crit_line_when_not_crit(self):
        settings = make_settings()
        store = CheckinStore(self.db_path, settings, rng=SeqRng([0.9], int_value=5))
        store.init_db()
        result = store.checkin("g1", "u1")
        self.assertNotIn("暴击", format_checkin_reply(settings, result))

    def test_reset_group_clears_only_that_group(self):
        store = self.new_store(rng=SeqRng([0.99], int_value=5))
        store.checkin("g1", "u1")
        store.checkin("g2", "u1")
        stats = store.reset_group("g1")
        self.assertEqual(stats["records"], 1)
        self.assertEqual(store.count_records("g1"), 0)
        self.assertEqual(store.count_records("g2"), 1)

    def test_settings_normalized_clamps_bad_values(self):
        settings = Settings(points_min=-5, points_max="oops", crit_chance=9,
                            crit_multiplier=0.2, leaderboard_limit=500).normalized()
        self.assertGreaterEqual(settings.points_min, 0)
        self.assertGreaterEqual(settings.points_max, settings.points_min)
        self.assertEqual(settings.crit_chance, 1.0)
        self.assertEqual(settings.crit_multiplier, 1.0)
        self.assertEqual(settings.leaderboard_limit, 10)

    def test_old_database_is_migrated(self):
        """老库（缺 crit 相关列）打开后自动补列，不炸。"""
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            "CREATE TABLE checkin_records (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " group_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL DEFAULT '',"
            " date TEXT NOT NULL, ts INTEGER NOT NULL, points INTEGER NOT NULL,"
            " base_points INTEGER NOT NULL DEFAULT 0, bonus_points INTEGER NOT NULL DEFAULT 0,"
            " streak INTEGER NOT NULL, is_first INTEGER NOT NULL DEFAULT 0,"
            " UNIQUE (group_id, user_id, date));"
            "CREATE TABLE checkin_users (group_id TEXT NOT NULL, user_id TEXT NOT NULL,"
            " user_name TEXT NOT NULL DEFAULT '', total_points INTEGER NOT NULL DEFAULT 0,"
            " streak INTEGER NOT NULL DEFAULT 0, max_streak INTEGER NOT NULL DEFAULT 0,"
            " total_days INTEGER NOT NULL DEFAULT 0, last_date TEXT,"
            " updated_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (group_id, user_id));"
        )
        conn.commit()
        conn.close()

        store = CheckinStore(self.db_path, make_settings(), rng=SeqRng([0.0], int_value=5))
        store.init_db()
        result = store.checkin("g1", "u1")
        self.assertEqual(result.crit_multiplier, 3.0)
        self.assertEqual(result.points, 18)


if __name__ == "__main__":
    unittest.main(verbosity=2)
