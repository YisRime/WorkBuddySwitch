# WorkBuddy Switch

WorkBuddy 桌面端账号切换器 + 账号管理工具。只操作本机，纯标准库 + Tkinter，无需安装依赖；
账号库与 hub 的 `accounts/` 同格式（一个账号一个 JSON）。

## 文件

```text
main.pyw  入口        util.py   路径/区域/工具/进程/设置    vault.py  at-rest 信封
client.py 登录态与切换  slot.py   命名实例                    api.py    上游活动接口
store.py  账号库        bulk.py   导入导出合并脱敏清理         ide.py    编辑器登录态导入
signup.py 注册登录      usage.py  本地用量报表                form.py   输入表单
window.py 主窗口        panel.py  账号 / 实例面板
```

`data/` 下：`Account/<昵称>-<uid8>.json` 账号库、`Backup/` 快照、`CLI/<实例名>/` 命名实例、
`Reports/` 报表、`settings.json` 接码与导出目录。

## 运行

双击 `main.pyw`，或 `pythonw main.pyw`。

## 界面

单页：左边账号表，右边一张控制卡，底部运行日志。除输入表单与两块二级面板外没有弹窗，
结果与错误都写日志。

- 搜索框右侧两个筛选 chip：**国内版 / 国际版**；都不选时两区都显示，点一个只看该区，
  再点一次取消。点 chip 会顺手重扫账号目录，3 天内到期的凭证自动续期。
- 账号表：账号 / 区域 / 状态 / 签到 / 连登 / 能量 / 积分 / 到期；点表头排序，
  `Ctrl+A` 全选，点空白取消选中；不选行时按钮作用于全部账号。
- 控制卡 10 个按钮，任何筛选下内容相同：

```text
[账号] [实例] [获取] [签到] [活跃]
[任务] [旅行] [管家] [试用] [导入]
积分包明细（跟着选中的账号）
```

两块面板都是「一行状态 + 输入 + 按钮」，没有内部标题与取消 / 确定：关窗或 Esc 即可。

```text
账号        国内 7 · 国际 5 · GUI 张三        实例   共 3 个 · 运行 1 · GUI 张三 · CLI dev
──────────────────────────────          ──────────────────────────────
手机号    [ ………… ]  [浏览器登录]        实例 [ dev · 张三 · 国内版 ▾ ] 张三 · 国内版 · 运行中
验证码    [ ………… ]  [发送][接码]        [切换 GUI][切换 CLI][ 新建 ][ 换号 ]
接码 token[ ………… ]                       [ 启动 ][ 停止 ][ 删除 ]
──────────────────────────────
[     登录     ][   导出    ]
导入  [文本][文件][客户端][编辑器]
```

## 切换与实例

GUI / CLI 是两个登录槽位，「实例」面板顶部常显这两行现在挂着谁，切换 GUI 会重启客户端，
CLI 只停 CLI 进程、下次启动生效。面板里的实例下拉带账号与区域，右侧跟着显示选中实例的
现状（账号 · 区域 · 运行中 / 已停止）。

「新建 / 换号 / 启动 / 停止 / 删除」是命名实例：每个实例一套独立目录 `data/CLI/<实例名>/`
（`home/` + `config/` + `slot.json` + `launch.cmd`），靠 `USERPROFILE` 隔离登录态，可多开。
国内版与国际版账号都可以建实例，路径相同。

## 活动

| 功能 | 范围 | 说明 |
|---|---|---|
| 获取积分 | 双区 | 额度 + 国内版连登 / 能量 / 抽奖次数 / 今日签到 |
| 签到 | 国内版 | 当天签过直接跳过；已签到 / 处理中 / 失败分别标注 |
| 活跃 | 国内版 | 一条 `chat_request_send` 点亮连登，每号每天一次 |
| 任务 | 国内版 | 接取 → 点亮 → 领奖 → 猫猫日常 → 刷新余额 |
| 旅行 | 国内版 | 归来领奖 / 在家派出；无 Buddy 先补 first_buddy |
| 管家 | 国内版 | 补签 → 礼包 / 补偿 → 档位兑换 → 抽奖 |
| 试用 | 国际版 | `/billing/ide/trial`，幂等（14051 = 已领过） |

企业号、静态 API Key 没有成长体系，自动跳过。

## 注册 / 登录

账号面板三行输入对应三种入口：**浏览器登录**（默认浏览器打开官方页，后台轮询收回凭证）、
**发送 + 登录**（手机号 + 短信码走 OneID → Keycloak → 客户端凭证，填 6 位码后入库）、
**接码**（填接码 token 后一键租号 → 发码 → 取码 → 校验 → 入库，项目号固定 139372）。

## 导入 / 导出

- 导入收在面板的「导入」一行，四个来源：文本（JSON / JSONL / CSV / 每行 token）、
  文件（json / jsonl / info / csv / txt / zip）、客户端 auth 目录（`*.info`，自动解
  `$wbEncrypted`）、编辑器登录态（VS Code / CodeBuddy IDE 的 `state.vscdb`、JetBrains 的
  `secret-storage.xml`）。身份只认 uid，缺的字段按 JWT 补齐。
- 导出：`.json` 合集包 / `.jsonl`（一行一个客户端 `.info`）/ `.info` 单账号 / `.csv` / `.txt` /
  `.zip`，可脱敏。
- 菜单「请求用量（本地会话）」汇总本地会话 token / 积分，写 `data/Reports/`。

## 账号库

`data/Account/` 一个账号一个 JSON，文件名 `<昵称净化>-<uid 前 8 位>.json`，身份只认 `uid`；
同 uid 多份保留有凭证且较新的，收录 / 获取 / 签到结果都并回账号文件；剩余 3 天以内的
凭证在启动与筛选刷新时自动续期。

## 接口

积分 `billing + /v2/billing/meter/get-user-resource`；签到 `daily-checkin` / `checkin-activity-status`；
成长 `chat + /v2/activity/growth/*`、`/activity/growth/*`；活跃 `/v2/report`；
续期 `/v2/plugin/auth/token/refresh`；档案 `/v2/accounts`；试用 `/billing/ide/trial`；
注册 `www.workbuddy.cn` + `oauth2.account.tencent.com`。

## 测试

`python -m unittest discover -s tests` —— 15 条，全部不联网、临时目录内：导入导出往返、
解析、合并去重、脱敏、清理、编辑器 Safe Storage 解密、注册行构造、本地用量统计。

## 边界

跨区不能切换登录态；默认 CLI 槽位只支持国际版（国内版用命名实例）；同一账号挂多处会被
上游挤下线；注册登录直连上游（忽略系统代理），短信码由腾讯发给账号本人；编辑器导入只读，
非 Windows 的 Safe Storage 解码未实现；账号 JSON 含 token，不要提交到公开仓库。
