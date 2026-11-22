# 智校安 · 请假自动审批

把「智校安」里**请假还没结束**的学生请假申请自动同意（已结束的积压不碰）。

不用装任何依赖，系统自带 `python3` 就能跑（浏览器那套只有首次逆向接口时用过，日常不需要）。

## 跑起来

```sh
cd ~/Desktop/local_repos/zhixiaoan

# 先空跑一轮看看它挑的范围对不对（不提交任何操作）
python3 approve.py

# 确认没问题就后台启动常驻，之后不用管
./run.sh start
```

**没登录或登录过期时，程序会当场提示你粘贴链接**，按屏幕上的两步提示做即可（微信里右键
「请假条提醒」→「复制链接」→ 回终端粘贴回车）。已经在有效期内则什么都不问，直接开跑。

`./run.sh status` 看状态，`./run.sh log` 看实时日志。

> **程序不读剪贴板。** 早先的版本会持续监听系统剪贴板来抓链接，那样能看到你复制的
> 所有内容（包括密码等无关信息），已彻底移除。现在只有你主动粘贴时才会去解析，
> 并且只认智校安的链接。自测里有一条专门断言源码中不存在 `pbpaste` 和后台监听线程。

## 登录信息怎么来

`token` 就在微信提醒的「查看详情」链接里，一个链接带齐了
`token / personId / organCode / userMode / tenant` 全套信息，所以不需要你额外填任何东西。

- 启动时若发现没登录或已过期 → **直接提示你粘贴**，粘完立刻开始工作。
- 后台运行的 `./run.sh start` 没法弹交互提示，所以它会**先检查登录状态**；
  缺了就让你先跑一次 `./run.sh login`，而不是默默启动然后在后台空转。
- 守护过程中登录过期 → 程序**暂停但不退出**，日志里提示你到另一个终端跑
  `./run.sh login`，登录完自动接上，不用重启。

> 登录态来自微信里点开的链接，约 24 小时有效，过期后需要重新粘贴。
> 程序不保存账号密码，也没有后台刷新。链接只在你主动粘贴时解析，不读剪贴板。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `./run.sh login` | 登录（提示你粘贴链接），完了就退出 |
| `./run.sh start` | 后台启动，每 60 秒扫一次 |
| `./run.sh start 30` | 同上，改成每 30 秒 |
| `./run.sh stop` / `status` / `log` | 停止 / 看状态 / 跟日志 |
| `./run.sh token` | 换登录态：回车后直接粘贴链接，不用加引号（推荐） |
| `./run.sh token '<链接>'` | 直接传参；链接里有 `&`，必须加引号 |
| `python3 approve.py --for-real` | 前台常驻，占着当前终端（登录提示最直观） |
| `python3 approve.py --for-real --once` | 只扫一轮就退出 |
| `python3 approve.py --for-real --date 2026-11-13 --once` | 只补批该日期提交的（不看请假是否结束） |
| `python3 approve.py --login` | 只做登录 |
| `python3 approve.py --set-token '<链接>'` | 把链接当参数传，完全不交互 |

### 扫描频率与实时状态

默认**每 60 秒扫一轮**，一直在扫。想改频率：

```sh
./run.sh start 30                              # 后台，改成 30 秒一轮
python3 approve.py --for-real --interval 30    # 前台，改成 30 秒一轮
```

在终端里跑时，会有一行**原地刷新、每秒倒计时**的状态，一眼就能看出它还活着：

```
· 已扫 12 轮｜待批 1｜可批 0｜通过 3｜剩 22.5h｜下次检查还有 43 秒
```

（没有新假条时不会往屏幕/日志堆东西，就是这一行在更新。有审批动作时才另起一行打印明细。）

后台运行时看不到这行，改用 `./run.sh log` 看日志，那里按 `--heartbeat` 间隔（默认 10 分钟）报一次平安。

常驻模式专门处理了这几件事：

- **登录过期不退出**：暂停并提示，登录后自动接上。
- **配置热更新**：每轮重读 `config.json`，登录态一变立即生效，永不重启。
- **不重复提交**：本次运行内已批过的单号会记住，不会因为列表刷新而重复提交。
- **失败会重试**：网络抖动导致的失败下轮重试；连续 3 次才放弃并留一条日志，避免死循环。
- **日志不刷屏**：没有新假条时不打任何东西，只按 `--heartbeat`（默认 10 分钟）报一次平安。
- **范围实时判断**：每轮重看返校时间，请假一结束就不再处理。

## 行为边界（重要）

- **只动请假还没结束的单子。** 判断依据是申请的 `applyBackSchoolTime`（返校时间），
  已经过了返校时间的积压不会碰；缺这个字段的单子也不批，因为没法判断是否结束。
- 想按提交日期精确处理，用 `--date YYYY-MM-DD`（比如临时补批某一天）。
- **多角色**：账号有多个角色时（比如 `101` 班主任、`157` 年级组长），在 `config.json` 里加
  `"userModes": ["101", "157"]`，每轮会分别扫每个角色的待审批队列；不配就只扫登录链接带的那一个。
- 系统里有个批量接口 `bathApprove`，但它会同意**所有**待审批（含以前积压的），
  所以脚本**故意不用它**，只走单条 `approve`。
- 被过滤掉的待审批会说明原因；没有可批的单子时，每条只提示一次，不刷屏。
- 每次提交都会往 `logs/approve-YYYY-MM-DD.jsonl` 写一条审计记录
  （谁、哪个班、什么类型、请假时段、理由、接口返回）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `approve.py` | 主脚本，日常只用这个 |
| `run.sh` | 后台常驻管理：start / stop / status / log / token |
| `config.json` | 登录态与账号配置（**含凭证，已 gitignore**） |
| `logs/` | `approve-YYYY-MM-DD.jsonl` 审计记录 + `daemon.log` 运行日志 |
| `selftest.py` | 离线自测：假数据验证筛选/去重/重试逻辑，不连服务器 |
| `probe.py` | 接口探针：用真实浏览器打开某页面，打印它调用的接口。登录态读 `config.json`，以后系统改版时用它重新摸接口 |
| `capture.py` | 抓包器：交互式录制页面全部 fetch/XHR，含请求响应体 |

> `probe.py` / `capture.py` 需要 Playwright（日常审批用不到，只在系统改版、要重新摸接口时才用）。
> 需要时现装即可：`python3 -m venv .venv && .venv/bin/pip install playwright`，
> 再用 `.venv/bin/python probe.py <URL>` 运行。
> `probe.py` 的 token 和账号信息直接读 `config.json`，所以先跑一次 `./run.sh login`。

改动脚本后跑一下自测确认没写坏：

```sh
python3 selftest.py     # 期望：通过 30 项，失败 0 项
```

> `config.json`、`logs/`、`.profile/`、`capture/`、`token.txt`、`js/` 都已在 `.gitignore` 里，
> 不会被提交到仓库。

## 接口备忘（改版时对照）

域名 `https://cloud.zuyxa.com`，业务前缀 `/api`。请求头需要：

```
authorization: Bearer <token>
tenant: <organCode 前三位>
```

| 动作 | 方法与路径 |
| --- | --- |
| 待审批列表 | `GET /vacate-attendance-service/vacate/studentVacate/vacateApproveList`<br>参数 `queryPersonId、organCode、pageNo、pageSize、status=0、studentName、userMode` |
| 同意单条 | `POST /vacate-attendance-service/vacate/studentVacate/approve`<br>body `{approveCode:1, vacateId, personId, userMode}` |
| 拒绝单条 | 同上，`approveCode:2` 且多一个 `approveNote`（拒绝理由） |
| 批量同意 | `POST /vacate-attendance-service/vacate/studentVacate/bathApprove`<br>body `{approveCode:1, approveNote, personId, userMode}` —— **会扫全部待审批，脚本不使用** |
| 请假详情 | `POST /vacate-attendance-service/vacate/studentVacate/findDetailById/{id}/{personId}/{personId}` |

`status` 取值：`0` 待审批、`1` 已通过、`2` 已拒绝、`-1` 已撤销、`3` 其他。
