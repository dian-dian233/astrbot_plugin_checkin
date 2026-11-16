# astrbot_plugin_checkin · 群签到 / 积分 / 暴击 / 排行榜

AstrBot 插件。每天一次签到，得积分、攒连签、拼榜；带**签到暴击**和**三个排行榜**（今日榜 / 总榜 / 连签榜），支持**本群榜**与**全局榜**两种口径。

- 防重复：数据库唯一索引 `UNIQUE(group_id, user_id, date)` 兜底，并发也刷不双份
- 时区：默认 `Asia/Shanghai`，跨天判断按配置时区算
- 权限：`/签到重置` 只认 `is_admin`
- 安全：无明文密钥、日志里用户 ID 打码

---

## 📐 积分计算规则（顺序很重要）

```
本次得分 = floor((基础分 + 连签加成) × 暴击倍率) + 首签奖励
```

| 步骤 | 说明 | 默认值 |
| --- | --- | --- |
| 1. 基础分 | 每次签到在 `points_min ~ points_max` 之间随机取一个整数 | **1 ~ 10** |
| 2. 连签加成 | 连签第 `streak_bonus_start` 天起，每多一天按基础分加 `streak_bonus_per_day`，封顶 `streak_bonus_max`；加成 = `round(基础分 × 加成率)` | **第 3 天起每天 +10%，上限 +100%** |
| 3. 暴击 | `crit_enabled` 为真时掷一次随机数，命中则「基础分 + 连签加成」整体乘 `crit_multiplier`，**向下取整** | **5% 概率 ×3** |
| 4. 首签奖励 | 当天本群第一个签到的人额外加分，**在暴击之后叠加，不参与暴击**（可设 0 关闭） | **+5** |

举例：基础分 8，连签 4 天（加成 +20% → +2），暴击 ×3，且不是首签：

```
floor((8 + 2) × 3) = 30 分
```

> 想严格只保留公式本身，把 `first_checkin_bonus` 设为 `0` 即可。

---

## 💥 签到暴击

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `crit_enabled` | bool | `true` | 总开关 |
| `crit_chance` | float | `0.05` | 单档暴击概率，`0` = 永不暴击，`1` = 必定暴击 |
| `crit_multiplier` | float | `3` | 单档暴击倍率 |
| `crit_tiers` | list | `[]` | 多档暴击（配置后覆盖上面两项） |
| `crit_message` | string | `💥 暴击 ×{multiplier}！本次 +{points} 分` | 暴击文案模板 |

**多档写法**（按倍率从高到低判定，命中即止，**不叠加、不连乘**）：

```json
[
  {"chance": 0.05, "multiplier": 3},
  {"chance": 0.01, "multiplier": 10}
]
```

也接受 `[[0.05, 3], [0.01, 10]]` 或字符串 `"0.05:3,0.01:10"`。
上面这组配置的实际效果：1% 概率 ×10，另有 4% 概率 ×3（`0.05 - 0.01`），其余不暴击；命中 ×10 时**不会**再乘 ×3。

**文案占位符**：`{multiplier}` 倍率、`{points}` 本次总分、`{subtotal}` 暴击前分数、`{base}` 基础分、`{streak_bonus}` 连签加成、`{streak}` 连签天数。

**随机源可注入（方便自测复现）**：

```python
store = CheckinStore(db_path, settings, rng=random.Random(42))  # 或 seed=42
```

回执示例（v1.2.0 起「少即是多」，签到成功只回一行）：

```
✅ 阿一 签到成功！本次 +21 分｜🔥 连签 4 天｜📈 累计 88 分
```

暴击时把暴击文案放在同一行（仍然是这一行）：

```
✅ 阿一 签到成功！💥 暴击 ×3！本次 +63 分｜🔥 连签 4 天｜📈 累计 130 分
```

> 基础分/连签加成明细、首签奖励、今日排名、「今日已有 N 人签到」等附加统计**不再输出**，避免刷屏；要完整明细发 `/我的签到`。

---

## 💬 签到成功回复的呈现方式（`success_reply_style`）

签到**成功**的那条回复可以选三种花样：普通文本、@ 发起人、引用发起人的签到消息。默认保持老样子（普通文本），不配置就什么都没变。

| 取值 | 效果 | 发出去的样子 |
| --- | --- | --- |
| `text`（默认） | 只发纯文本 | `✅ 阿一 签到成功！本次 +21 分｜🔥 连签 4 天｜📈 累计 88 分` |
| `at` | 回复里 **@ 签到的人** | `[@阿一] ✅ 签到成功！本次 +21 分｜🔥 连签 4 天｜📈 累计 88 分` |
| `quote` | **引用/回复**用户那条签到消息 | `> 引用：/签到` + `✅ 阿一 签到成功！...` |
| `both` | @ + 引用都加上 | 上面两个一起 |

- 只想普通文本（= 两种都关）：填 `text`，或 `none` / `off` / `关闭`，都认。
- 别名也认：`plain`→text、`@`/`mention`/`艾特`→at、`reply`/`引用`/`回复`→quote、`at+quote`/`all`→both；写错（比如 `火星模式`）自动回落到 `text`，不会报错、不会静默失联。
- 改完即时生效（不用重载插件）。
- **只有「签到成功」这一条会被装饰**；重复签到提示、`/我的签到`、排行榜、`/签到重置` 的输出都还是纯文本。

实现方式直接对齐 AstrBot 框架自己的做法（`astrbot/core/pipeline/result_decorate/stage.py`，v4.29.3）：

```python
# @ 回复
result.chain.insert(0, At(qq=event.get_sender_id(), name=event.get_sender_name()))
# 引用回复
result.chain.insert(0, Reply(id=event.message_obj.message_id))
```

所以插件里也是：

```python
At(qq=event.get_sender_id(), name=event.get_sender_name())   # 带真实 userid，不是纯文本昵称
Reply(id=event.message_obj.message_id)                        # 引用那条签到消息
```

消息链顺序按 OneBot v11 习惯排 `[Reply] [At] [Plain]`；`at` 模式下正文不再重复写昵称（@ 已经点出来了）。
任何一步拿不到东西（平台不给 `message_id`、AstrBot 版本没有 `At`/`Reply` 段）都**自动回退纯文本**，不会抛异常、不会丢回执。

> ⚠️ AstrBot 全局还有个 `platform_settings.reply_with_mention` / `platform_settings.reply_with_quote`，那是**对所有**回复生效的。本插件这个配置只管签到成功那一条。如果全局已经开了，这里建议保持 `text`，否则可能出现重复 @ / 重复引用。

---

## 🏆 排行榜

| 指令 | 榜单 | 排序依据 |
| --- | --- | --- |
| `/签到榜 today` | 今日签到榜 | 当天得分降序（同分按更早签到在前） |
| `/签到榜 total` | 总积分榜 | 累计积分降序（同分按更早达成在前） |
| `/签到榜 streak` | 连签榜 | 当前连续签到天数降序（**已断签不上榜**） |

- 完整语法：`/签到榜 [today|total|streak] [本群|全局] [top10]`，参数顺序随意，也认中文别名（今日/总榜/连签、`top5`、直接写数字）
- 条数：默认 10，最多 10，越界自动收敛
- 口径：`group`（默认，只看本群）/ `global`（跨群汇总，标题带「全群汇总」）；也可用配置 `leaderboard_scope` 改默认口径
- **并列名次**：同分同名次，下一名次跳过 → `1, 2, 2, 4`
- **空榜**：返回友好提示，不报错、不抛异常
- 榜单固定最多 10 行；第 10 名同分时按上面的「更早在前」规则决定谁进榜
- `enable_image_rank` 打开且环境有 Pillow + 中文字体时输出图片，失败自动回退文本

积分累计口径：按 `(group_id, user_id)` 分别累计；全局榜是把同一用户各群的积分相加。

---

## ⌨️ 指令

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `/签到`（或裸发「签到」「打卡」） | 每日签到 | 所有人 |
| `/我的签到` | 查自己积分、连签、累计天数 | 所有人 |
| `/签到榜 ...` | 三个榜单，见上表 | 所有人 |
| `/签到重置 确认` | 清空本群签到数据，需二次确认 | **仅 is_admin** |

---

## ⚙️ 全部配置项（WebUI → 插件配置）

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `points_min` | int | `1` | 基础积分下限 |
| `points_max` | int | `10` | 基础积分上限（填反会自动交换） |
| `first_checkin_bonus` | int | `5` | 每日首签奖励，暴击后叠加；`0` 关闭 |
| `streak_bonus_start` | int | `3` | 连签加成从第几天开始 |
| `streak_bonus_per_day` | float | `0.1` | 连签加成每天递增比例（相对基础分） |
| `streak_bonus_max` | float | `1.0` | 连签加成上限（相对基础分） |
| `crit_enabled` | bool | `true` | 启用暴击 |
| `crit_chance` | float | `0.05` | 暴击概率（单档） |
| `crit_multiplier` | float | `3` | 暴击倍率（单档） |
| `crit_tiers` | list | `[]` | 多档暴击，非空则覆盖单档 |
| `crit_message` | string | `💥 暴击 ×{multiplier}！本次 +{points} 分` | 暴击文案模板 |
| `success_reply_style` | string | `text` | 签到成功回复呈现方式：`text`（纯文本，默认）/ `at`（@ 发起人）/ `quote`（引用其签到消息）/ `both`（都加） |
| `leaderboard_limit` | int | `10` | 榜单默认条数（上限 10） |
| `leaderboard_scope` | string | `group` | 榜单默认口径：`group` / `global` |
| `timezone` | string | `Asia/Shanghai` | 算「今天」用的时区 |
| `keyword_enable` | bool | `true` | 允许裸发「签到/打卡」触发 |
| `allow_private` | bool | `false` | 允许私聊签到 |
| `enable_image_rank` | bool | `false` | 榜单输出图片（失败回退文本） |
| `group_whitelist` | list | `[]` | 群白名单，留空=全部可用 |
| `group_blacklist` | list | `[]` | 群黑名单 |

> 所有数值配置都会被收敛到安全区间（类型写错、越界都不会把插件弄崩）。

---

## 🗣️ 群公告可直接贴

```
📅 每日签到规则
· 基础积分：1~10 分随机
· 暴击概率：5%，命中 ×3（💥 暴击时积分直接乘 3）
· 连签加成：连签第 3 天起每天 +10%，最高 +100%
· 结算公式：floor((基础分 + 连签加成) × 暴击倍率)
· 每日首签额外 +5 分
· 排行榜：/签到榜 today | total | streak（可加「全局」看跨群榜）

👉 发 /签到 即可，每天一次
```

---

## 🧪 自测

```bash
python3 -m unittest discover -s tests -v
```

覆盖：暴击概率 0 / 1 的边界、多档不重复叠加、多档统计分布、公式取整、首签叠加顺序、
唯一索引防重复、Asia/Shanghai 跨天、三个榜单与并列名次、空榜提示、全局/本群口径、
参数校验、管理员门禁、LLM 工具参数校验、老库自动补列、配置默认值与文档一致、
`success_reply_style` 四种取值（At 段 userid / Reply 段 message_id / 链顺序 / 缺 ID 回退 / 别名收敛）。

---

## 🔐 安全说明

- **无明文密钥**：插件不读取、不保存任何 token / 密码 / API Key，仓库与配置文件里都没有密钥字段
- 日志里用户 ID 打码（`12***89`）
- 重置等危险操作强制 `is_admin` + 二次确认
- 数据库文件落在 AstrBot 的 `data/plugin_data/astrbot_plugin_checkin/`，不进仓库

---

## 📄 变更记录

- **v1.3.0**：新增配置项 `success_reply_style` —— 签到成功回复可切换呈现方式：
  `text`（纯文本，默认，与旧版一致）/ `at`（@ 发起人，At 段带真实 userid）/
  `quote`（引用发起人的签到消息，Reply 段）/ `both`（两者都加）。只改「签到成功回复」
  的消息构造，积分/连签/暴击/三个排行榜、`checkin_leaderboard`、`checkin_my_status` 行为完全未变；
  取不到 `message_id` 或缺少消息段时自动回退纯文本
- **v1.2.0**：签到成功回执瘦身（「少即是多」）—— 只保留一行核心反馈：签到成功 + 本次得分
  （暴击时带暴击文案）+ 连签 + 累计积分；不再输出基础分/加成明细、首签奖励、今日排名与
  「今日已有 N 人签到」。积分累计、连签、暴击、三个排行榜、`checkin_leaderboard`、
  `checkin_my_status` 行为**完全未变**
- **v1.1.0**：新增签到暴击（单档/多档、可配文案、随机源可注入）；积分公式统一为
  `floor((基础分 + 连签加成) × 暴击倍率)`；排行榜支持本群/全局口径与同分同名次；老库自动补列
- **v1.0.0**：签到、连签、积分、今日/总榜/连签榜、管理员重置
