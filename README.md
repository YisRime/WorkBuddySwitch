# WorkBuddy Switch

WorkBuddy 桌面客户端账号切换器 + 账号活动面板。只操作本机，不依赖 `workbuddy2api-hub`，
但活动侧的提示与操作口径与 hub（workbuddy2api-hub v1.7.33）保持一致：

- 读取 `data/Account/` 下的单账号 JSON（与 hub `accounts/`、原 `workbuddy-accounts-by-account`
  同格式，一个账号一个文件，可直接互拷）；
- 选择账号后切换登录态，GUI 客户端与独立 CLI 两个槽位各自一个按钮；
- 合并本机各账号的本地会话列表；
- 用 `data/Backup/` 里的一份 auth + db 快照恢复（「恢复」按钮）；
- 续期凭证、收录当前登录态；
- 国内版 Buddy 加油站签到 / 每日活跃 / 成长任务流水 / 猫猫旅行 / 连登管家与抽奖，
  双区积分额度与连登查询。

## 文件

每个功能一个模块，文件名都是单个单词：

```text
WorkBuddySwitch/
  main.pyw      入口（图形界面）
  util.py       路径与区域配置、通用工具（JSON / JWT / HTTP / 额度）、客户端进程
  vault.py      at-rest 加密信封与密钥回收（可独立当扫描子进程）
  client.py     登录态读写、凭证续期、会话构造、切换 / 同步、快照备份与恢复
  slot.py       命名 CLI 实例：独立目录、启动 / 停止 / 删除
  api.py        上游接口：签到 / 上报 / 积分 / 连登 / 领奖 / 抽奖 / 旅行 / 试用 + 任务流水
  store.py      账号库：data/Account 单账号 JSON 的读取、去重、写回
  bulk.py       导入 / 导出 / 合并 / 脱敏 / 清理（平铺档与客户端 .info 档互转）
  signup.py     注册与登录：手机号短信链路、官方页面接力、可选的接码平台
  probe.py      账号体检：本地字段 + 上游连通，判过期 / 失效
  stats.py      积分历史：快照追加、按天归并、趋势报表
  timer.py      定时任务：按天去重地自动签到 / 活跃 / 任务 / 旅行 / 管家
  form.py       输入表单窗口（注册、导入、导出、定时共用一套控件）
  window.py     主窗口 + 配色样式 + 悬浮提示

  data/
    Account/<昵称>-<uid8>.json  账号库（一个账号一个文件，hub 凭证档格式）
    Backup/                     快照（auth.info + cli-auth.info + workbuddy.db + manifest）
    CLI/<实例名>/                命名 CLI 实例（home/ + config/ + slot.json + launch.cmd）
    Stats/history.jsonl         积分历史快照（一行一条）
    Reports/                    统计报表（stats-YYYYMMDD-HHMMSS.md）
    settings.json               全局设置（定时任务 schedule、接码平台 sms）
    .trash/<时间戳>/             清理移走的坏文件与重复文件

  tests/
    test_core.py                不联网的回归测试（导入导出 / 统计 / 定时 / 体检）
```

只依赖 Python 3.9+ 标准库和 Tkinter，不需要 pip 安装任何东西。

## 运行

双击 `main.pyw`（需已关联 pythonw），或在命令行里 `pythonw main.pyw`。

## 界面

浅色布局，**国内版 / 国际版 / 全部三页**，切页按钮贴在搜索框右侧，默认停在国内版。
每页内容各自独立、只显示自己那一区的东西；运行日志在底部共用（它是操作流水，
不分区域）。除了输入表单（注册、导入、导出、定时要填东西），没有任何弹窗：确认、
进度、结果、错误全部实时写进日志，操作进行中时日志标题旁显示状态（如「成长任务中…」），
全部按钮禁用。

| | 国内版页 | 国际版页 | 全部页 |
|---|---|---|---|
| 账号 | 只列国内版（cn） | 只列国际版（intl） | 两区都列 |
| 账号表列 | 账号 / 签到 / 连登 / 能量 / 积分 / 最早到期 | 账号 / 积分 / 包数 / 到期 / 登录槽位 | 账号 / 区域 / 状态 / 签到 / 积分 / 到期 |
| 按钮 | 收录 · 恢复 · 刷新 · 续期 · 获取 · 签到 · 活跃 · 任务 · 旅行 · 管家 | 收录 · 恢复 · 刷新 · 续期 · 获取 · 同步 · 新建 · 换号 · 启动 · 停止 · 删除 · 试用 | 注册 · 导入 · 导出 · 收录 · 刷新 · 续期 · 获取 · 签到 · 体检 · 统计 · 定时 · 清理 · 复制 · 全选 |
| 专属控件 | 无 | GUI / CLI 两行当前登录与「切换」、实例下拉 | 无 |

- **切页按钮在搜索行**：搜索框与账号数量右边是三个分段按钮（国内版 / 国际版 /
  全部），高度与搜索框齐平，当前页实心、另两页白底描边；切页不再占右栏一行。
- **右栏是一张控制卡**：里面是一个**不带分组标签的按钮网格**（每行 5 个，等宽
  等距，按上表的顺序从左往右排满，所有按钮同一尺寸——网格按钮、GUI / CLI 的
  「切换」按钮都是同一个宽高）；国际版在「试用」后面接实例下拉、以及压在最后的
  GUI / CLI 两行当前登录；全部页排满 14 个按钮（三行：5 + 5 + 4）。界面没有第二条工具栏，也没有「页面 / 维护 /
  账号 / 操作」这类组名。
- **两页的骨架对齐**：右栏宽度写死成 `SIDE_W`（479px，取这个数是为了让内容宽
  能被 5 整除，按钮不会差 1px），左栏宽度因此由窗口宽度决定、与页面无关，切页
  时分隔线不动；两页的按钮网格都从卡片第一行开始，第一行按钮完全相同（收录 /
  恢复 / 刷新 / 续期 / 获取），第二行才是各页专属的那组。
- **所有按钮都带 1px 边框**（`relief="solid"`，`bordercolor` 比卡片分隔线深
  一档）；禁用态也保留一圈更浅的边框，不会塌成纯文字。
- **默认不选中任何账号**：不选时每个按钮都作用于**当前页全部**账号，直接点就行；
  选中一行则只作用于选中的（多选即批量）。点表格空白处取消选中。
- **积分包明细**：右栏下半张卡，跟着选中的账号走，逐包列出名称、剩余、总量与
  到期（在用包高亮、过期包置灰）。没选中账号时显示提示、不铺空表。点「获取」
  时会把上游的逐包明细一起拉下来，**并写回该账号的 JSON**——账号文件里的
  `credits.packages` 就是这份数据。
- **额度按月刷新**：订阅类积分包的有效期写到十年后（或没有到期时间），真正的
  到期点是每个自然月的周期。这类包的「到期」列**直接写本月底的时间**（如
  `2026-10-31 23:59:59 (3.4 天)`），不再出现「每月刷新」「长期」或 203x 年的日期；
  积分包明细也按这一刻排序（在用包优先、过期包沉底）。账号表的「最早到期」
  仍只统计**有明确到期时间**的包——按月刷新的额度到期即重置，不算到期。
- **到期时间全部精确到秒**：账号表「到期 / 最早到期」、积分包「到期」与 token
  过期时间都显示到秒；各表的到期列随之加宽，其余列相应收窄。
- **到期列按显示的那一刻排序**：账号表点「到期 / 最早到期」表头时，排序键就是
  这一列显示的时间点（不再是 token 的过期时间），没有到期信息的账号沉底。
- **国内版没有登录槽位与实例**：GUI / CLI 两个槽位、命名实例都只有国际版
  用得上，所以这些控件只在国际版页出现。
- **GUI / CLI 两行只在有东西可写时才留字**：程序没装就整行留空（不写「未安装
  GUI 客户端」这类提示），「切换」按钮置灰、悬停才说明缺什么。
- **实例下拉只列身份**：`实例名 · 账号 · 区域`，运行状态不写进下拉（由「启动」
  与「停止」互斥点亮体现），未选中时显示「选择实例」。
- 账号表整卡全高，多选即批量；失效账号整行置灰；`Ctrl+A` 全选当前页账号。
- **运行日志只记录操作与请求结果**：切页、加载账号这类页面状态不写日志，
  只有收录 / 恢复 / 刷新 / 切换 / 续期 / 获取 / 签到 / 任务 / 实例等动作才留痕。

```text
左栏
  [搜索…]   3 / 3   [国内版][国际版][全部]   ← 切页贴在搜索框右侧
  账号表（整卡全高，多选即批量）

右栏（固定 479px，下面是三个页面各自的控制卡）
  国内版
    [收录][恢复][刷新][续期][获取]
    [签到][活跃][任务][旅行][管家]
  国际版
    [收录][恢复][刷新][续期][获取]
    [同步][新建][换号][启动][停止]
    [删除][试用][实例下拉 ................]
    GUI  …                                 [切换]
    CLI  …                                 [切换]
  全部
    [注册][导入][导出][收录][刷新]
    [续期][获取][签到][体检][统计]
    [定时][清理][复制][全选]
  积分包明细（跟着选中的账号）
```

## 切换目标：GUI / CLI

「GUI」「CLI」两行（压在国际版控制卡最下面）行尾的「切换」按钮分别作用于两个
登录槽位，可把桌面与 CLI 切到不同账号，双号并行且互不共享 token：

- **切换 GUI**：写 `auth/workbuddy-desktop*.info`（`$wbEncrypt` 密文槽位），
  停掉并重启客户端，等待登录态生效；客户端内嵌的 CLI 随之切换；
- **切换 CLI**：写独立 CLI 的 `auth/Tencent-Cloud.coding-copilot.info`（明文
  槽位，不需要 at-rest 密钥），只停 `cbnode` 等 CLI 进程，不动、不重启
  GUI 客户端，写入后回读校验，下次启动 CLI 生效。默认槽位只支持国际版
  账号（国内版请用命名实例），会在日志里说明并拒绝。

两个槽位的「切换」按钮只有在**登录态文件存在**且**程序已安装**时才可用：
GUI 要求 `auth/workbuddy-desktop*.info` 在、且能找到 WorkBuddy 客户端 exe；
CLI 要求 `Tencent-Cloud.coding-copilot.info` 在、且 `codebuddy` 命令在 PATH 里。
任一条件不满足就置灰，悬停会说明缺什么（无文件 / 未安装）。命名实例的新建与
启动同样要求 `codebuddy` 命令可用。

CLI 槽位的口径：写进 CLI 的 refreshToken 若被上游轮换，hub 手里的旧
refreshToken 会失效（与「续期凭证」一节同源）；同一账号同时挂在桌面 +
CLI + hub 三处是风控敏感行为，建议错开账号或时段。默认槽位用的是真实
用户目录（`%USERPROFILE%`），其会话与历史是整机一份、不分账号，切号后
会混在一起——要各自独立请用下面的命名实例。

## 命名实例：多个 CLI 并行（各自账号、各自文件夹）

「实例」那组按钮（新建 / 换号 / 启动 / 停止 / 删除，紧挨着实例下拉）在默认 CLI
槽位之外开**命名实例**，每个实例一套独立目录，可以同时运行多个 CLI，账号、
会话与历史互不串扰：

- 目录在 `data/CLI/<实例名>/`：`home/`（实例的 `USERPROFILE`，登录态就落
  在里面）、`config/`（CLI 配置目录，会话历史在内）、`slot.json`（元数据与
  认领的进程 pid 与创建时间）、`launch.cmd`（env 已烘焙的自包含启动脚本，
  可脱离工具双击）。实例名由账号昵称净化而来，重名自动加 `-2`，不用手输。
- 隔离原理：CodeBuddy CLI **没有**「指定登录态文件」的环境变量——登录态路径
  是写死的 `os.homedir()/AppData/Local/CodeBuddyExtension/Data/Public/auth/
  Tencent-Cloud.coding-copilot.info`，而 Windows 上 `os.homedir()` 读
  `USERPROFILE`。所以启动实例时只对该进程把 `USERPROFILE` 指到实例自己的
  `home/`，登录态便落在实例目录里，多个实例各写各的、互不覆盖；再叠加
  `CODEBUDDY_CONFIG_DIR` 指到实例的 `config/`，会话与历史也各自分开。`HOME`
  保持真实用户目录（git / bash 仍读得到用户配置），工作目录设为选定的文件夹。
- 重要：旧版本往 `launch.cmd` 里写的 `CODEBUDDY_CLI_AUTH_FILE` 在 CLI 2.x 里
  根本不存在（整个包搜不到这个变量名），被忽略，所以那时每个实例实际都在用
  **全局账号**。升级后旧实例目录会把根下的 `auth.info` 搬进 `home/…/auth/`
  （原文件留成 `auth.info.legacy`），旧的 `launch.cmd` 在下次「启动 / 换号」时
  重写；想让老实例立刻生效，对着它点一次「启动」即可。
- 「新建」= 选中一个账号 → 选工作文件夹 → 建槽并立即启动（国际版、国内版
  都可以，实测两区在隔离家目录里都能正常跑）；「换号」把选中实例换成选中的
  账号，实例在运行就自动重启（无感换号），没在运行就写入后下次启动生效，
  写入失败用 `.bak` 回滚；「启动 / 停止」只作用于选中实例自己的进程树，
  不碰其他实例与默认 CLI；「删除」把实例目录移进 `data/CLI/.trash/`，可
  手动找回。界面上「启动」与「停止」互斥点亮，不会同时可点。
- 新实例第一次启动**不会**弹「是否信任此文件夹」的菜单：建槽时就把工作目录
  写进实例的 `config/settings.json`（`trustedDirectories`，同时登记
  `目录` 与 `目录/**` 两种写法）。CLI 的信任菜单只认方向键与回车、不认打字，
  以前新建实例后「敲什么都进不去，按一次 Ctrl+C 才恢复正常」就是它。
- 启动实例时会把 `TERM` 归一成 `xterm-256color`、清掉 `NO_COLOR` /
  `FORCE_COLOR`：CLI 源码里 `TERM=dumb` 会直接判成「非交互终端」，
  从 git-bash、CI、某些编辑器终端拉起本工具时会把这类变量带进来，
  新窗口同样表现为按键无响应。`launch.cmd` 里也一并烘焙了 `TERM` / `COLORTERM`。
- `data/CLI$slot/` 这种目录是旧版把 `%CLI_DIR%\$slot` 原样当路径留下的空壳
  （只有 CLI 缓存，没有 `slot.json`，也不含登录态），可以直接删；实例名净化
  现在也会把 `$` 换成 `-`，不会再生成同类目录。
- 默认 CLI 槽位（界面上的「CLI」行）行为不变：写全局 auth 文件，下次
  启动 CLI 生效；切换默认槽位时会绕开命名实例正在运行的进程。CLI 命令
  默认是 `codebuddy`，可用环境变量 `WB_CLI_CMD` 换成别的可执行文件。

## 活动

| 功能 | 范围 | 说明 |
|---|---|---|
| 获取积分 | 双区 | 积分额度 + 国内版连登/能量/抽奖次数/今日签到状态；2 并发 |
| Buddy 加油站签到 | 国内版 | `daily-checkin`；已签到 / 处理中 / 失败分别标注，当天签过直接跳过 |
| 每日活跃 | 国内版 | 发一条 `chat_request_send` 事件点亮连登，每号每天一次；上报后回读连登天数 |
| 一键做任务领积分 | 国内版 | 全自动流水：auto 任务 → 批量接取 → 事件上报点亮 → 领奖 → 猫猫日常 → 刷新余额 |
| 猫猫旅行 | 国内版 | 归来领奖 / 在家派出；没有 Buddy 时先补 first_buddy |
| 连登管家与抽奖 | 国内版 | 补签保连登 → 新手礼包/活动补偿 → 7d/14d/28d 档位兑换 → 抽奖一次抽完 |
| 积分列 | 双区 | 未查询时显示账号文件里的 `credits`，查询后替换为实时值并写回 |

成长任务流水与 hub 的 `wb_tasks.py` 同口径：

- 任务规格（`TASK_SPECS`）、专家 / 团队 id 池、夜猫子窗口（23:00-08:00）一致；
- **专家体验任务按深链现认**：任务 `jump_url` 里的 `expertId`（`workbuddy://experts?
  expertId=ex_xxx`，URL 编码成 `expertId%3D`）就是上游要的事件 id，「」里是专家名。
  所以「体验「中国电商运营专家」」这类没写进 `TASK_SPECS` 的新任务也能直接点亮领奖，
  不必每上一个活动就改一次代码；
- 专家 / 团队事件用互不相同的 id（上游按 `(eventCode, id)` 去重）；
- 只认桌面真实行为的任务（`DESKTOP_ONLY_TASKS`、公众号、公益捐赠）诚实跳过并给出深链；
- 接取失败会复核重试一次，仍未接取的任务跳过上报（未接取上报不计进度）；
- 日志文案逐条对齐 hub（「正在点亮任务 […]」「✓ 领奖成功: +N 积分」「🎉 全部完成…」）。

「签到 / 活跃」都做了当日门禁（与 hub 的 `can_checkin` / `can_daily_chat` 同口径）：
账号文件里今天已经记过就直接跳过，不再打一次上游——重复上报只多一份风控特征。

表格里的「签到」优先用 `checkin-activity-status` 的 `today_checked_in`（查过才准），
没查过就按账号文件里的 `lastCheckin` 判断是否为今天；「连登」只有查过才有值。
获取 / 签到 / 活跃的结果会并回各自的账号文件（活跃记 `lastActivityReport`，与 hub 同名），
下次打开仍能看到上次的值。

## 注册与登录（手机号）

新账号不用先在客户端里登录一遍：全部页的「注册」（或右键菜单「注册 / 手机号登录…」）
直接在本机把腾讯 OneID 的短信登录协议跑完，拿到的凭证与客户端 `.info` 同构，进库后
就能像别的账号一样切换、续期、签到。

- 链路（标准库直连、全程共用同一个 cookie jar）：
  `POST www.workbuddy.cn/v2/plugin/auth/state?platform=workbuddy` 拿设备授权 state →
  `/console/auth/login` 换 Keycloak 地址 → 登录页里正则取 OneID 入口 → broker 跳转拿
  OneID 的 CSRF state → `oauth2.account.tencent.com/v1/auth/sms/code/send` 发码 →
  `…/code/verify` 校验 → `…/v1/auth/accounts` 拿一次性授权码（**只能调一次**，它消耗
  state_token）→ `/auth/realms/copilot/broker/oneid/endpoint` 落 console 会话 → 再让
  console 会话认领设备 state（漏了这步会一直 `11217 login ing...`）→
  `/v2/plugin/auth/token?state=` 换 accessToken / refreshToken。
- **验证码永远由腾讯发给账号主人本人**：工具不代收、不破解，只是替你把协议跑完。同一
  号码短时间发码过多会被要求图形验证码，换号或等一会儿即可。
- **浏览器接力**：短信路径被图形验证码挡住时，点表单里的「浏览器登录」——工具只拿
  state 与登录页地址，用默认浏览器打开，你在官方页面自己登录（验证码由腾讯处理），
  后台轮询 `/v2/plugin/auth/token?state=`（这个端点不需要 cookie）把凭证收回来。
- 「注册后跑日常」选「是」时，新号入库后顺带跑一遍签到 → 猫咪旅行 → 成长任务。
- 自备接码平台（可选，`data/settings.json` 的 `sms` 段）：接口按疾驰短信那一套实现
  （`fcToken` 头 + `/api/user/getPhone | getVerifyCode | releasePhone`，`code == 1`
  为成功），换平台改配置即可；留空就只能手动填验证码。

## 导入

「导入」三种来源共用一套解析（`bulk.py`）：

| 来源 | 说明 |
|---|---|
| 粘贴文本 | 合集包、JSON 数组、JSONL（一行一个凭证）、CSV（带表头按列名认）、每行一个 token |
| 选择文件 | `.json` / `.jsonl` / `.info` / `.csv` / `.txt` / `.zip`（zip 里多个文件逐个解析） |
| 扫本机客户端 | 读客户端 auth 目录里所有 `*.info`（跳过 `.bak` 备份），用本机 at-rest 密钥解开加密字段 |

- 身份只认 uid：平铺档直接读 `uid`，客户端 `.info` 档从 `account.uid` 读，只给 token
  时从 JWT 的 `sub` 反推（写错 uid 会让切换失败，所以永远以 sub 为准）；`realm /
  domain / expiresAt` 缺了就按 JWT 的 `iss` 与 `exp` 补齐，手工粘一串 token 也能建出
  完整记录。
- 同 uid 已在库里就合并（保留积分、档位等旧字段），新凭证更旧时跳过不覆盖；新增 /
  更新 / 跳过 / 失败逐条写日志。
- 收进来的账号文件仍是 hub 凭证档格式，直接丢进 `data/Account/` 也能用。

## 导出

「导出」默认导出选中（没选中就全部），六种格式：

| 后缀 | 内容 |
|---|---|
| `.json` | 合集包 `{version, exportedAt, count, accounts:[...]}`（hub / 旧版合集包同构） |
| `.jsonl` | 一行一个客户端 `.info` 文档（WB2api 备份 / 导出格式，剪贴板同构） |
| `.info` | 单账号 `.info`（account / auth 两段，时间是毫秒） |
| `.csv` | 表格（带 BOM，Excel 双击不乱码） |
| `.txt` | 一行一个账号，制表符分隔，能再导回来 |
| `.zip` | 每个账号一个 `.info`，另附 `accounts.json` 合集包 |

- 「脱敏」把 token 只留头尾、昵称与 uid 打码，用于截图 / 分享；账号文件本身不动。
- 「复制」把选中（或全部）账号按 `.jsonl` 复制到剪贴板。

## 账号体检

`GET /v2/accounts` 是「登录态还有没有效」最省事的一枪：401 / 403 直接判失效，成功还
能拿到上游昵称，顺手纠正改过名的账号。结论分四档——正常 / 注意（缺 refreshToken、
三天内到期、区域与域名不一致）/ 过期 / 失效，逐号写一行日志，再汇总一行「正常 N，
注意 N，过期 N，失效 N」。串行执行，避免把上游打出风控。

## 统计（积分历史）

每次「获取」都往 `data/Stats/history.jsonl` 追加一条快照（时间 / uid / 剩余 / 总量 /
包数 / 签到态），同一天多条只按当天最后一条算。点「统计」生成
`data/Reports/stats-YYYYMMDD-HHMMSS.md`：每个账号的剩余、总量、近 7 天变化与日均消耗，
末尾一行合计。

## 定时任务

`data/settings.json` 的 `schedule` 段：

```json
{"enabled": true, "time": "09:00", "actions": ["checkin", "report"],
 "catchup": true, "last": {"2026-11-21": ["checkin", "report"]}}
```

- 动作可选 `checkin` / `report` / `tasks` / `travel` / `streak`；目标固定是账号库里
  的国内版个人号（企业号、静态 Key 跳过）。
- 每 20 秒醒一次，只做「今天到点且还没做过」的；`catchup` 打开时错过时点当天也补跑
  （上班才开机的那种用法），每做完一个动作就记进 `last`，重启程序不会重复跑。
- 全部在后台线程里跑，结果只写日志、不碰控件；界面「定时」按钮就是这几个字段的表单。

## 清理

「清理」扫描 `data/Account/`：坏 JSON、缺 uid、同 uid 重复文件都挪进
`data/.trash/<时间戳>/`（不真删）。同 uid 两份时按「凭证齐全 → 没过期 → 文件更新」
的顺序留一份。

## 国际版试用

「试用」按钮（国际版页）打 `POST {billing}/billing/ide/trial`：幂等端点，已领过时上游
回 `14051`，按成功处理；国内版没有这个端点。

## 账号库

`data/Account/` 里一个账号一个 JSON，格式与 hub 的 `accounts/` 目录、
`workbuddy-accounts-by-account` 完全一致（`uid / nickname / realm / domain /
accessToken / refreshToken / expiresAt / credits / lastCheckin …`）：

- 文件名是 `<昵称净化>-<uid 前 8 位>.json`，但身份只认文件里的 `uid`，
  随便改名、复制进复制出都不影响；同一 uid 多份文件时保留「有凭证且较新」的那份，
  并在日志里提示；
- 直接把别处的账号 JSON 丢进这个目录，点右栏第一行的「刷新」就出现，不用改代码；
- 「收录」把客户端当前登录态写成同样格式的文件，已有该 uid 就并进原文件
  （保留积分、档位等旧字段），没有才新建；
- 「续期 / 获取 / 签到 / 活跃 / 任务」的结果会并回各自的账号文件，
  下次打开还显示上次的积分与签到时间；
- 旧版把全部账号塞在一个 `data/workbuddy-accounts-*.json` 合集包里；
  只有在 `data/Account/` 整个目录不存在时才会把那个包拆成单账号文件导一次，
  目录一旦存在就完全以它为准（清空也不会把旧包里的账号变回来）。

## 备份与恢复

每次切换 / 同步前，会先停对应的客户端（GUI 或 CLI），再清空并重写
`data/Backup/`：

- `auth.info`（客户端 `auth/*.info`）
- `auth.info.logged-out`（备份时处于登出状态才有）
- `cli-auth.info`（独立 CLI 槽位，存在才收）
- `workbuddy.db` / `-wal` / `-shm`
- `manifest.json`（来源路径与 sha256）

始终只有一份快照；操作失败时自动用同一份快照回滚。「恢复」会同时
恢复两个槽位（备份里有的才恢复）。

## 续期凭证

- `POST {realm}/v2/plugin/auth/token/refresh`，`X-Refresh-Token` 走 refreshToken；
- 成功后才写回 JSON，失败不落盘；
- 上游可能轮换 refreshToken，刷新后立即写回，避免下次用到旧凭证；
- 同一账号若还在 hub 池里使用，独立续期可能让 hub 手里的旧 refreshToken 失效。

## 接口

所有上游请求按 `workbuddy2api-hub` 的口径对齐（端点、请求头、请求体、ID 形态）：

| 用途 | 端点 | 说明 |
|---|---|---|
| 积分汇总 / 逐包明细 | `billing + /v2/billing/meter/get-user-resource` | 两个区都先取这个；CN 再用 summary + free/paid 补逐包明细，INTL 直接用它的 Accounts |
| 每日签到 | `billing + /v2/billing/meter/daily-checkin` | 仅 CN |
| 签到状态（只读） | `chat + /v2/billing/meter/checkin-activity-status` | 仅 CN；一次拿到今日是否已签、连登天数、每日 / 今日积分 |
| 成长 / 任务 / 旅行 / 抽奖 | `chat + /v2/activity/growth/*`、`/activity/growth/*` | 仅 CN |
| 对话活跃上报 | `chat + /v2/report` | 事件形状对齐 hub `build_event("chat")` |
| 凭证续期 | `chat + /v2/plugin/auth/token/refresh` | CN / INTL 两套头 |
| 账号档案 | `chat + /v2/accounts` | 切换登录态时取档案；体检也用它判失效 |
| 试用加油包（国际版） | `billing + /billing/ide/trial` | 无 body；`14051` = 已领过，按成功算 |
| 注册 / 短信登录 | `www.workbuddy.cn` + `oauth2.account.tencent.com` | 见「注册与登录」的 11 步链路 |

- **端点分家**：`chat`（对话 / 成长）与 `billing`（计费 / 积分）分开配置，
  CN 分别是 `copilot.tencent.com` 与 `www.codebuddy.cn`，INTL 都是 `www.workbuddy.ai`。
- **计费头分区域**：CN 是浏览器形态（Origin / Referer / X-Requested-With /
  X-Machine-ID / X-Session-ID / X-Product）；INTL 是客户端内置 axios 形态
  （UA `axios/1.16.1`、无 Origin / Referer / X-Requested-With）。两区都带
  `X-No-Enterprise-Id`（没有企业身份时），与 hub 的 else 分支一致。
- **`X-Device-Token` 只在配了才发**：国内版客户端带的是本机 QIMEI / Turing Shield
  设备 token，工具自己生成不了；把抓包里的一条真 token 放进环境变量
  `WB_DEVICE_TOKEN` 就会带上（国际版本来没有这个头）。
- **对话头**按 hub 的桌面端身份：`X-Agent-*`、`X-IDE-*`、`X-Private-Data`、
  `X-Product`、会话头族齐全，链路头族（`traceparent` / `b3` / `X-B3-*` /
  `X-Trace-ID`）一并带上；ID 用 UUIDv7（会话带连字符、消息 32 位 hex）。

## 测试

```bash
python -m unittest discover -s tests -v
```

不联网：导入导出往返（json / jsonl / info / csv / txt / zip）、CSV 与 JSONL 与纯 token
的解析、合并去重、脱敏、清理、历史统计、定时判断、体检的本地分支都在临时目录里跑，
不碰 `data/` 下的真实账号。

## 边界

- 只合并 `transport='local'` 且有 JSONL 的本地会话；云端会话按原账号鉴权，不能跨账号合并。
- 跨区切换（国际版客户端 ↔ 国内版账号）直接在日志里报错并中止。
- 默认 CLI 槽位只支持国际版账号（写的是全局 `Tencent-Cloud.coding-copilot.info`）；
  命名实例两区都支持。
- 「停进程」只按 `cbnode.exe` / `cbc-prewarm.exe` 这两个 CLI 内部进程名，加上我们自己
  拉起、且在 `slot.json` 里记过 pid 的实例进程树（停之前还会核对进程创建时间，pid 被
  系统回收给别的进程时自动放手）。用 `node` 跑起来的 `codebuddy` 永远不在打击范围内，
  所以你在终端里开着的那个 CLI 不会被这些操作关掉。
- 密钥回收（`at-rest` 静态密钥）依赖 Windows 上从运行中的客户端进程只读取回；客户端必须先启动并登录。
- 签到 / 每日活跃 / 任务流水只对国内版个人号开放：企业号、静态 API Key 没有成长体系。
- 命名实例的登录态靠 `USERPROFILE` 隔离（路径不可改，只能换家目录）；CLI 续期凭证后
  回写的是实例 `home/` 里那份，不会碰全局槽位，工具侧从实例目录读即可。
- 每个实例自带一份 `home/` + `config/`，首次启动会重新生成 CLI 的缓存与日志（约
  几百 KB 起），多开时磁盘与内存占用按实例数线性增长。
- 「恢复」只写全局槽位（GUI auth + 全局 CLI auth + 本地库）；命名实例的登录态在各自
  `home/` 里，进程树也整棵保留，完全不受影响。
- 同一账号同时挂多个实例（或实例 + 默认槽位 + hub）时，上游轮换 refreshToken 会互相
  挤下线；一个账号建议只挂一处，工具会在日志里提示但不阻止。
- 账号 JSON 含 token，属于敏感数据，不要提交到公开仓库。
- 注册 / 登录显式忽略 Windows 系统代理：登录链路走代理常被上游 TLS / WAF 掐断，
  表现为 `SSL: UNEXPECTED_EOF_WHILE_READING`；要经代理跑请自己在网络工具里放行这些域。
- 短信验证码由腾讯下发给账号主人本人，工具不代收、不破解；接码平台只是可选的自备通道。
- 定时任务靠程序自己开着的后台线程，关掉程序就停；不写系统计划任务、不留常驻服务。
- 导入 / 导出只读写 `data/Account` 下的 JSON，不碰客户端登录态；只有「收录」「切换」
  才会写 auth 文件。

## 参考

`docs/refs/` 下是三份源码考古笔记（workbuddy2api-panel / Workbuddy-WB2api /
workbuddy-switch）：注册登录的 11 步链路、导入导出格式、签到与成长接口的口径都出自
它们，只作实现依据，不含任何账号或凭证。
