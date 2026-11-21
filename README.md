# WorkBuddy Switch

WorkBuddy 桌面端账号工具：一个窗口里做注册、导入、切换、签到与成长任务。只操作本机，
纯标准库 + Tkinter，没有第三方依赖；账号库与 hub 的 `accounts/` 同格式（一账号一 JSON）。

## 运行

双击 `main.pyw`，或 `pythonw main.pyw`。

## 文件

```text
main.pyw  入口        util.py   路径 / 区域 / 进程 / 设置   vault.py  at-rest 信封
window.py 主窗口      panel.py  账号 / 实例 / 导出面板      client.py 登录态与切换
store.py  账号库      bulk.py   导入导出与合并去重           slot.py   命名实例
signup.py 注册登录    ide.py    编辑器登录态导入             api.py    上游活动接口
usage.py  本地用量
```

`data/` 下：`Account/<昵称>-<uid8>.json` 账号库、`CLI/<实例名>/` 命名实例、`Reports/` 报表、
`settings.json` 接码与导出目录。

## 界面

单页：左边账号表，右边控制卡，底部日志。除了账号 / 实例 / 导出三块面板，没有别的窗口；
确认、进度、结果、错误一律写进日志。

- 筛选两个 chip：**国内版 / 国际版**，都不选就是两区；点 chip 会重扫账号目录，3 天内到期的
  凭证自动续期。
- 账号表：账号 / 区域 / 状态 / 签到 / 连登 / 能量 / 积分 / 到期。点表头排序，`Ctrl+A` 全选，
  点空白取消；不选行时按钮作用于本页全部可用账号。
- 十个按钮：

```text
[账号] [实例] [导入] [获取] [试用]
[签到] [活跃] [任务] [旅行] [管家]
```

面板都是「标签 + 输入 + 按钮」，关窗或 Esc 即可。

### 账号

```text
手机号     [ ………… ] [网页]
验证码     [ ………… ] [发送]
接码       [ ………… ] [注册]
────────────────────────
[ 登录 ][ 扫描 ][ 导入 ][ 导出 ]
```

### 实例

```text
GUI   张三                                    [切换]
CLI   dev                                     [切换]
实例  [ dev · 张三 · 国内版 · 运行中 ▾ ]      [换号]
[ 新建 ][ 启动 ][ 停止 ][ 删除 ]
```

### 导出

两个下拉（格式 / 脱敏）加一个「导出」，点完再选保存位置。

## 注册 / 登录

三个入口：**网页**（默认浏览器打开官方页，后台轮询把凭证收回来）、**发送 + 登录**（手机号
→ OneID → Keycloak → 客户端凭证，填 6 位码入库）、**注册**（接码平台租号：拿号 → 发码 →
取码 → 校验 → 入库，项目号固定 139372）。

## 导入 / 导出

- 导入：选文件，json / jsonl / info / csv / txt / zip 都认（`Bulk.read`）。
- 扫描：一次读两处——客户端 auth 目录（`*.info`，自动解 `$wbEncrypted`）与编辑器登录态
  （VS Code / CodeBuddy IDE 的 `state.vscdb`、JetBrains 的 `secret-storage.xml`），合并去重
  后入库。身份只认 uid，缺的字段按 JWT 补齐。
- 导出：`.json` 合集包 / `.jsonl`（一行一个客户端 `.info`）/ `.info` 单账号 / `.csv` / `.txt` /
  `.zip`，可脱敏。
- 主页「导入」按钮是另一件事：把当前 GUI 登录态收进账号库（同 uid 只更新）。
- 菜单「请求用量（本地会话）」汇总本地会话 token / 积分，写 `data/Reports/`。

## 切换与实例

GUI / CLI 是两个登录槽位，切换 GUI 会重启客户端，切换 CLI 只停 CLI 进程、下次启动生效。
命名实例一套独立目录 `data/CLI/<实例名>/`（`home/` + `config/` + `slot.json` + `launch.cmd`），
靠 `USERPROFILE` 隔离登录态，可多开；国内版与国际版账号都能建，路径相同。

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

## 账号库

`data/Account/` 一个账号一个 JSON，文件名 `<昵称净化>-<uid 前 8 位>.json`，身份只认 `uid`；
同 uid 多份保留有凭证且较新的，导入 / 获取 / 签到结果都并回账号文件。

## 接口

积分 `billing + /v2/billing/meter/get-user-resource`；签到 `daily-checkin` /
`checkin-activity-status`；成长 `chat + /v2/activity/growth/*`、`/activity/growth/*`；
活跃 `/v2/report`；续期 `/v2/plugin/auth/token/refresh`；档案 `/v2/accounts`；
试用 `/billing/ide/trial`；注册 `www.workbuddy.cn` + `oauth2.account.tencent.com`。

## 边界

跨区不能切换登录态；默认 CLI 槽位只支持国际版（国内版用命名实例）；同一账号挂多处会被
上游挤下线；注册登录直连上游（忽略系统代理），短信码由腾讯发给账号本人；编辑器导入只读，
非 Windows 的 Safe Storage 解码未实现；账号 JSON 含 token，不要提交到公开仓库。
