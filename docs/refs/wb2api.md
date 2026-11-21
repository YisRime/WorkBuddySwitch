# WorkBuddy → WB2api 源码考古报告（移植到 Tkinter 桌面工具用）

> 目标仓库：`D:\Work\_wb_refs\Workbuddy-WB2api`（Python/FastAPI + 原生 JS 管理台）
> 分析方式：只读。所有结论均标注源文件与行号（`文件:行`），可直接对照实现。
> 适用范围：**排除**「账号转 OpenAI 兼容 API」的网关部分（converter.py / proxy.py / pool.py / responses_* 等），
> 只保留注册、登录、导入导出、签到、积分、福利、成长、同步、设备指纹、UI 动作。
> 端口迁移目标：纯 Python + Tkinter（`D:\Work\WorkBuddySwitch`），因此凡涉及 node 子进程的地方都给出「可省略/可替代」说明。

---

## 0. 模块地图（先看这张表再读后面）

| 领域 | 文件 | 职责 | 移植优先级 |
|---|---|---|---|
| 手机号注册/登录协议（核心） | `admin/wb_login.py` (1273 行) | 纯 HTTP 走完 OneID 短信登录 → 换 `.info` 凭证 | P0 必须 |
| 接码平台 | `admin/sms_client.py` (258 行) | 疾驰短信 JichiSMS 取号/读码/释放 | P0 必须 |
| 一键注册编排 | `admin/routers/sms_register.py` (607 行) | 取号→发码→轮询→验证→入库，SSE 输出 | P0 必须 |
| 登录路由 | `admin/routers/login.py` (821 行) | 会话/发码节流/验证入库/官方页面接力 | P0 必须 |
| 登录后自动收尾 | `admin/post_login.py` (238 行) | 后台队列：资源初始化 + 成长/签到 | P1 |
| 风控设备 token | `admin/turing_token.py` + `turing_helper.js` | 取 `X-Device-Token`（需 node + 桌面端） | P2 可省略 |
| 设备指纹（登录侧） | `admin/device_env.py` (134 行) | 每次会话随机 UA/Client-Hints/伪 IP | P1 |
| 设备指纹（网关侧） | `admin/client_profile.py` (31 KB) | UA 三段式、风控头、桌面事件指纹 | P1 |
| 凭证加解密 | `admin/credential_codec.py` (217 行) | 官方 `.info` 的 WBEV1 / AES-GCM | P1（仅「导出给官方客户端」需要） |
| 账号归属（Key 绑定） | `admin/account_binding.py` (56 行) | ApiKey↔Account 独占绑定、可见性 | 可忽略 |
| 提示词脱敏 | `desensitize.py` (24 KB) | **不是凭证脱敏**，是给上游审核做零宽字符混淆 | 不移植 |
| 账号 CRUD/导入导出 | `admin/routers/accounts.py` (83 KB) | 列表/新增/批量/扫描本机/导出/注入 | P0 必须 |
| 线上同步 | `admin/routers/sync.py` (330 行) | HMAC 推送账号/模型/客户端参数 | P2 |
| 客户端参数档案 HTTP | `admin/routers/client_profile.py` (151 行) | 探测/保存/导入导出 UA 参数 | P2 |
| 每日签到 | `admin/scheduler.py:593` `run_daily_checkin` | 遍历活跃账号领积分 | P0 必须 |
| 成长任务 | `admin/routers/growth.py` (37 KB) + `admin/growth_plans.py` | 任务分级/参与/触发/领奖 | P1 |
| 成长计划巡检 | `admin/growth_plan.py` (20 KB) | 逐号补活跃/领礼物 | P2 |
| 福利/补偿 | `admin/compensation.py` + `admin/routers/benefits.py` | DeepSeek 500 积分一次性领取 | P1 |
| 积分明细/到期 | `admin/credits.py` + `admin/routers/credits.py` | 积分包到期时间口径 | P1 |
| 调度器 | `admin/scheduler.py` (55 KB) | 任务注册表/默认时点/重试/冷却 | P0 必须 |
| 健康观测 | `admin/health.py` (93 行) | 15 分钟风险窗 + 24h 模型健康 | P3 |
| 上游调用封装 | `admin/backend.py` (84 KB) `AccountSession` | 每个业务动作一个方法 | P0 必须读 |
| 凭证管理/请求头 | `converter.py:250` `CredentialManager` | 注入鉴权头、刷新 token | P0 必须读 |

### 0.1 域名常量（全部实测值，硬编码即可）

| 常量 | 值 | 出处 | 用途 |
|---|---|---|---|
| `BACKEND` | `https://copilot.tencent.com` | `converter.py:132`、`admin/routers/accounts.py:_UPSTREAM` | 后端业务 API（签名/签到/成长/插件） |
| `DEFAULT_DOMAIN` | `www.codebuddy.cn` | `converter.py:133` | `X-Domain` 兜底值 |
| `HOST` | `https://www.workbuddy.cn` | `admin/wb_login.py:19` | 登录页 / console 会话 / plugin 端点 |
| `OAUTH2` | `https://oauth2.account.tencent.com` | `admin/wb_login.py:21` | 腾讯 OneID 短信接口 |
| `WEB_BASE` | `https://www.workbuddy.cn` | `admin/backend.py:120` | web 域：资料库行为、补偿领取 |
| `BILL_BASE` | `https://www.codebuddy.cn` | `admin/backend.py:121` | billing 域：业务上报 |

### 0.2 三条互斥的登录/注册路径（务必同时实现，互为兜底）

| 路径 | 入口 | 是否需要用户手动操作 | 何时用 |
|---|---|---|---|
| A. 纯协议短信登录 | `wb_login.start_login()` → `send_sms` → `verify_sms` | 只需填手机号 + 验证码 | 默认（桌面工具主路径） |
| B. 接码平台全自动 | `/api/sms-register/run` | 无 | 批量注册（号池铺量） |
| C. 官方页面接力 | `wb_login.start_official_login()` → 轮询 `fetch_credential_by_state` | 用户在官方页面自己过图形验证码 | **路径 A 被 `need_captcha` 挡住时**（唯一出路） |

---

## 1. 注册 / 登录：真实上游接口（`admin/wb_login.py`）

### 1.1 常量与超时（`wb_login.py:96-190`）

```python
HOST = "https://www.workbuddy.cn"
OAUTH2 = "https://oauth2.account.tencent.com"
ONEID_CLIENT_ID = "ztPxdKSUDy6sAUE7azhv5g0v"   # 第 4 步 authorize 里带的 client_id
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0")
SESSION_TTL = 900                # 一次登录会话 15 分钟作废
HTTP_TIMEOUT = 10                # 读超时
HTTP_CONNECT_TIMEOUT = 5         # 连接超时
MAX_HOPS = 12                    # 跟随重定向最大跳数
MAX_SESSIONS = 500               # 会话表上限
GC_INTERVAL_S = 60.0             # 会话回收线程周期
MAX_CONCURRENT_UPSTREAM = 8      # 全局上游并发闸门
_ALLOWED_HOSTS = ("workbuddy.cn", "codebuddy.cn", "tencent.com")   # 重定向白名单
MOBILE_PATTERNS = {"86": r"1[3-9][0-9]{9}", "852": r"[4-9][0-9]{7}", "853": r"6[0-9]{7}"}
_UID_RE  = re.compile(r"^[A-Za-z0-9_.@-]{6,120}$")
_JWT_RE  = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
```

**httpx.Client 建会话参数**（`_new_session`, `wb_login.py:559`）：
`follow_redirects=False`、`timeout=httpx.Timeout(10, connect=5, pool=5)`、
`limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30.0)`、
`headers=env.build_headers(HOST)`（见第 5 章）。**每个会话独立 cookie jar**——
OneID 会话横跨 `workbuddy.cn` 与 `oauth2.account.tencent.com`，共用 client 会串号。

### 1.2 完整 11 步链路（逐步给出 method / path / body / 判据）

> 总原则：所有响应先过 `_envelope_data()`（`wb_login.py:641`）解 `{code,msg,data}`，
> **`code` 不在 `(0, None)` 即抛 `WbLoginError(msg, step=..., upstream=obj)`**。
> 微信/腾讯侧的短信域不是这个信封，它用 `errCode`/`errMessage`（见第 6、7 步）。

#### 步骤 1 — 申请设备授权 state（`start_login`, `wb_login.py:672`）

```
POST https://www.workbuddy.cn/v2/plugin/auth/state?platform=workbuddy
Content-Type: application/json
X-Requested-With: XMLHttpRequest
body: {}
```
判据：`data.state` 非空（存 `sess.plugin_state`），`data.authUrl` 存 `sess.plugin_auth_url`（仅「接力模式」展示用）。
失败文案：`"上游未返回设备授权 state"`。

#### 步骤 2 — 换 Keycloak 授权地址，同时下发 console 会话 Cookie

```
GET https://www.workbuddy.cn/console/auth/login
    ?platform=workbuddy&state=<plugin_state>&domain=www.workbuddy.cn
X-Domain: www.workbuddy.cn
```
判据：读 `Location` 头（`follow_redirects=False`，自己处理），与 HOST 做 `urljoin` → `sess.kc_url`。

#### 步骤 3 — Keycloak 登录页，正则抓 OneID 入口

```
GET <kc_url>            Accept: text/html,*/*
```
正则（**顺序敏感**，必须与下面一致）：
```python
re.search(r'data-idp="oneid"[^>]*href="([^"]+)"', r.text)
```
命中后 `urllib.parse.urljoin(HOST, href.replace("&amp;", "&"))` → `sess.broker_url`。
失败文案：`"登录页未提供 OneID 入口（上游可能已调整页面）"`，`step="keycloak_page"`。

#### 步骤 4 — broker 跳转，取 OneID 的 CSRF state（记 S1）

```
GET <broker_url>        Accept: text/html,*/*      Referer: <kc_url>
```
判据：读 `Location` → 解析 query 里的 `state` → `sess.oneid_state`。
**这个 S1 就是第 9 步回传用的 state，不是 Keycloak 的 session_code。** 失败文案 `"OneID 授权地址缺少 state"`。

#### 步骤 5 — 预访问 OneID 首页（建立 OneID 侧会话，失败不致命）

```
GET <authorize_url 原样>    Accept: text/html,*/*
```
`status_code >= 400` 只记 warning，不抛。

#### 步骤 6 — 发送短信验证码（`_send_sms_locked`, `wb_login.py:757`）

```
POST https://oauth2.account.tencent.com/v1/auth/sms/code/send
Content-Type: application/json
X-TOA-LANG: zh-CN
body: {"client_code":"codebuddy",
       "mobile":"+86 13800138000",          # 注意：区号与号码之间是一个空格
       "scopes":["openid","mobile","profile"]}
```
- 手机号先过 `normalize_mobile(mobile, country_code)`：去空格/括号/横线，去 `+86`/`86` 前缀，再全匹配 `MOBILE_PATTERNS`。
- 同一会话若已有 `sms_state_token` 且号码/区号变了 → 直接拒绝：`"当前会话正在等待验证码，请完成验证后再更换号码"`。
- 响应：`{"state_token": "...", "expires_in": 60, ...}`；`errCode` 非空即为失败（用 `errMessage`）。
- **没有 `state_token` 且带 `captcha` 或 `status=="need_captcha"`** → 抛
  `WbLoginError(step="sms_send", reason="need_captcha")`，前端据此切「路径 C」。这是**按手机号**的风控（换号立刻恢复），不是按 IP。
- 返回给上层：`{"expires_in": int(obj.get("expires_in") or obj.get("expire") or 60)}`。

#### 步骤 7 — 校验验证码（`_verify_sms_locked`, `wb_login.py:906`）

```
POST https://oauth2.account.tencent.com/v1/auth/sms/code/verify
Content-Type: application/json
X-TOA-LANG: zh-CN
body: {"state_token":"<sess.sms_state_token>", "code":"123456"}
```
- 入参 code 先 `re.sub(r"\D", "", code)`，长度必须 == 6，否则 `"请输入 6 位数字验证码"`。
- 响应若带新 `state_token` 则覆盖 `sess.sms_state_token`（实测同值，以返回值为准更稳）。

#### 步骤 8 — 取账号列表 + OneID 授权 code（⚠️ 一次性，只能调一次）

```
GET https://oauth2.account.tencent.com/v1/auth/accounts?state_token=<sess.sms_state_token>
```
- 成功：`{"accounts":[{"id":...}, ...], "code":"<OneID 授权码>"}`（`errCode` 为空）。
- **该调用会消耗 `state_token`**。因此代码里明确要求：先 accounts 拿 code，**只有拿不到 code 才**退到 select_account。
- 退路：
```
POST https://oauth2.account.tencent.com/v1/auth/select_account
Content-Type: application/json
body: {"state_token":"<...>", "account_id":"<accounts[0].id 或 'codebuddy@'>"}
```
- 两条都拿不到 code → `"未能取得 OneID 授权码"`（`step="account_select"`）。

#### 步骤 9 — 用 code 完成 broker 登录（建立真 console 会话）

```
GET https://www.workbuddy.cn/auth/realms/copilot/broker/oneid/endpoint
    ?code=<urlencode(code)>&state=<urlencode(sess.oneid_state)>
Referer: <kc_url>
```
跟随重定向（`_follow`，白名单域内最多 12 跳）。**成功判据是 cookie 而不是响应体**：
```python
names = {c.name for c in sess.client.cookies.jar}
assert {"KEYCLOAK_SESSION","KEYCLOAK_IDENTITY"} & names
```
不满足 → `"未能建立登录会话（上游可能拒绝了本次授权）"`。

#### 步骤 10 — 让 console 会话认领设备 state（漏了这一步会一直 `11217`）

```
GET https://www.workbuddy.cn/console/auth/login
    ?platform=workbuddy&state=<plugin_state>&domain=www.workbuddy.cn
Referer: https://www.workbuddy.cn/
```

#### 步骤 11 — 换客户端凭证（这就是最终入库的东西）

```
GET https://www.workbuddy.cn/v2/plugin/auth/token?state=<plugin_state>
X-No-Authorization: true
```
判据：`code == 0` 且 `data.accessToken` 非空；否则 `"换取凭证失败：{msg|code}"`。
`data` 字段：`accessToken, refreshToken, expiresIn, refreshExpiresIn, tokenType, sessionState, domain, scope`。

#### 步骤 11b — 用该 token 拉一次账号信息（拿 uid/昵称/手机号）

```
GET https://www.workbuddy.cn/v2/plugin/accounts
Authorization: Bearer <accessToken>
X-Domain: <data.domain 或 www.workbuddy.cn>
X-No-User-Id: true
X-No-Enterprise-Id: true
X-No-Department-Info: true
```
取 `data.accounts[0]`；`code != 0` 时返回 `{}`（不致命，uid 会从 JWT/其它途径补齐，但**入库前 `validate_credential` 会因 uid 缺失而失败**，所以实际必需）。

### 1.3 凭证 JSON 结构（`build_credential_json`, `wb_login.py:986`）

> 与桌面端 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` **同构**，字段名不可改。
> 时间戳**毫秒**（`expiresAt/refreshExpiresAt/lastRefreshTime`），`expiresIn/refreshExpiresIn` 是**秒**。

```json
{
  "account": {
    "uid": "<必填>",
    "nickname": "",
    "uin": "",
    "type": "personal",
    "lastLogin": true,
    "isCreator": false,
    "isAdmin": false,
    "pluginEnabled": true,
    "deployStatus": {"statusCode": 0, "statusMsg": "", "detailMsg": ""},
    "accountType": "",
    "sso": {"domain": "", "domainModifiedTimes": 0},
    "idp": "",
    "areaInfoComplete": false,
    "oneidAccountId": "",
    "isCurrentOneIdEnterprise": false,
    "isCurrentOneIdPersonal": false,
    "isFirstLogin": false,
    "phoneNumber": "13800138000",     // 有值才写
    "enterpriseId": "",              // 有值才写
    "enterpriseName": ""             // 有值才写
  },
  "auth": {
    "accessToken": "<JWT>",
    "expiresIn": 7200,
    "refreshExpiresIn": 2592000,
    "refreshToken": "<必填，缺失即废号>",
    "tokenType": "Bearer",
    "sessionState": "",
    "domain": "www.workbuddy.cn",
    "lastRefreshTime": 1763700000000,
    "expiresAt": 1763707200000,        // expiresIn 存在才写 = now_ms + expires_in*1000
    "refreshExpiresAt": 1766292000000, // refreshExpiresIn 存在才写
    "scope": ""                        // 有值才写
  }
}
```
序列化：`json.dumps(..., ensure_ascii=False, indent=2)`。

### 1.4 结构校验 `validate_credential()`（`wb_login.py:225`）—— 号池唯一入口

按顺序逐条判定，任一失败返回 `CredentialCheck(False, 中文原因)`：

| # | 条件 | 失败文案 |
|---|---|---|
| 1 | 输入是 str（去空白后 `json.loads`）或 dict；顶层是 dict | `内容为空` / `不是合法 JSON —— 本接口只收授权后的凭证 JSON` / `顶层必须是 JSON 对象` |
| 2 | `auth` 与 `account` 同时是 dict | `缺少 auth / account 字段 —— 这不是客户端授权凭证` |
| 3 | `auth.accessToken` 是非空 str 且匹配 `_JWT_RE` | `auth.accessToken 缺失` / `auth.accessToken 不是合法的 JWT` |
| 4 | `auth.refreshToken` 非空 str | `auth.refreshToken 缺失 —— 没有刷新令牌的凭证无法续期` |
| 5 | `account.uid` 非空且匹配 `_UID_RE` | `account.uid 缺失` / `account.uid 含非法字符` |
| 6 | `auth.expiresAt`(ms) 未过期；否则 JWT `exp`(s) 未过期 | `auth.accessToken 已过期，请重新登录` / `accessToken 已过期（JWT exp），请重新登录` |
| 7 | 若 JWT `sub` 存在则必须 == `account.uid` | `accessToken 与 account.uid 不匹配 —— 凭证被拼接过` |

成功返回 `meta = {uid, enterprise_id, domain, nickname, phone}`。

### 1.5 上游存活校验 `probe_credential_live()`（`wb_login.py:1150`）

```
1) AccountSession(cred).fetch_balance()
      → POST https://copilot.tencent.com/v2/billing/meter/get-user-resource  body {}
      → 取 data.Response.Data.Accounts[] 中 CapacityUnit=="credits"
         total += CycleCapacityRemain；total_size += CapacitySize
      → {"total": total_size, "remain": total}
2) AccountSession(cred).fetch_account_profile()
      → GET /v2/plugin/accounts  (Bearer) → uid/nickname/uin/type/phoneNumber/enterpriseId/enterpriseName
```

判定表（源码 `_TOKEN_DEAD_CODES` / `_QUOTA_CODES`）：

| 码 | 语义 | 结论 |
|---|---|---|
| `12153` | 登录态已失效（会话不存在） | **失败** `reason="token_dead"` |
| `12151` | 登录态无效（未选择账号） | **失败** `reason="token_dead"` |
| `6004` / `6008` / `14003` / `14051` | 模型超限/日限/限流/已领取 | **通过** `reason="quota"`（配额问题≠假凭证） |
| `401` / `403` | 网关鉴权拒绝 | **失败** `reason="auth_failed"` |
| 其它 | — | **失败** `reason="upstream_error"` |
| 成功且 `profile.uid != claimed uid` | 张冠李戴 | **失败** `reason="uid_mismatch"` |
| 成功且 uid 一致 | — | 通过，`message="上游验证通过：{who}（积分 remain/total）"`，并带回 `balance` |

**余额为 0 不算伪造**（号池里 0 额度是正常态）。
`_extract_upstream_error()` 从异常文本按 3 个正则依次抠 code：
`HTTP (\d{3}) / (\d{3,6}): (msg)` → `HTTP (\d{3})` → 裸 `(\d{4,6}): (msg)`。

### 1.6 会话状态机（`PhoneLoginSession`，`wb_login.py:307`）

```
[created] --start_login 成功--> [registered] (in _sessions)
   │                                 │
   │                                 ├─ send_sms 成功 → sms_state_token 非空，code_sent_at=now
   │                                 ├─ verify_sms 成功 → result_cache=C, consumed=True, consumed_at=now
   │                                 └─ verify_sms 失败 → 仍可重试（state_token 可能已被消耗，实测会报 E0010072）
   └─ 任一 WbLoginError → sess.close() 且不入表（start_login 的 except 分支）

回收（_gc_locked，每 60s 一次 + 每次新建会话时）：
   expired()            → created + 900s 已过        → 立即移除并 close
   consumed && >60s     → 已消费且超过 60s 宽限期     → 移除并 close
上限 500：满了先强清所有 consumed，再满则拒绝新建（reason="busy"）
```
字段清单见 `wb_login.py:307-360`：`sid, client, created, env(DeviceEnv), plugin_state, plugin_auth_url,
kc_url, broker_url, oneid_state, sms_state_token, mobile, country_code, code_sent_at, consumed,
lock(RLock), result_cache, consumed_at`。

**幂等语义**：`verify_sms` 全程持 `sess.lock`，成功后 `result_cache` 复用；用户双击不会变成「成功一次 + 报一次无效 token」。

### 1.7 「官方页面接力」路径（`start_official_login` / `fetch_credential_by_state`）

实测事实（源码注释记录）：
- 官方登录页**没有** `X-Frame-Options`/`frame-ancestors` → 可 iframe 内嵌；
- `GET /v2/plugin/auth/token?state=` **不需要任何 cookie** → 服务端可用零 cookie client 轮询；
- `code=11217`（`login ing...`）**不区分「还没好」与「state 无效」** → 只能靠前端超时收尾。

```
1) POST /v2/plugin/auth/state?platform=workbuddy  → {state, authUrl}      # start_official_login，只做这一步
2) 用户自己在官方页面登录（验证码由腾讯处理）
3) 轮询 GET /v2/plugin/auth/token?state=<state>
     headers: Accept: application/json, text/plain, */*   X-No-Authorization: true
     每次都新建零 cookie httpx.Client，UA = WEB_UA
     code != 0 → 返回 None（前端继续轮，间隔 2.5s）
4) build_credential_from_token(token)：
     GET /v2/plugin/accounts  (Bearer + X-Domain + 三个 X-No-*: true)  → account
     build_credential_json → validate_credential 自检
```

### 1.8 登录侧排队/限流（`routers/login.py`）

| 常量 | 值 | 说明 |
|---|---|---|
| `SMS_MIN_INTERVAL` | 60 s | 同一手机号 + 全局出口的最小发码间隔 |
| `SMS_MAX_PER_HOUR` | 10 | 出口每小时上限（**失败请求也占配额**），同时按客户端 IP 限 |
| `_sms_exit_blocked_until` | now + 15 s | 遇到 `need_captcha`/含「频繁/限制/rate/频率」时额外自锁 15 s |
| 上游并发闸门 | 8 | 满了返回 `503` + `Retry-After: 3`（轮询端点则静默返回 `{"done":false,"busy":true}`） |

`_check_sms_quota(ip, mobile)` 判定顺序（`login.py:76`）：
1. 出口 1h 命中数 `>= 10` → 429 `"短信出口本小时配额已用完"`（带 `Retry-After`）
2. `wait = max(_sms_exit_last+60, _sms_exit_blocked_until) - now > 0` → 429 `"短信出口保护中，请 N 秒后再试"`
3. 号码 60s 内重复 → 429 `"该手机号发送过于频繁，请 N 秒后再试"`
4. 单 IP 1h `>= 10` → 429 `"本小时发送次数已达上限"`
5. 全部通过才记账（`_sms_log[ip]`, `_sms_mobile_last[mobile]`, `_sms_exit_last`, `_sms_exit_hits`）

错误响应体统一形状（`_fail`, `login.py:145`）：`{"detail": {"message","step","reason"?}}`。

### 1.9 登录/注册 HTTP 端点清单（`routers/login.py` + `routers/sms_register.py`）

| Method + Path | 鉴权 | 入参 | 出参 | 备注 |
|---|---|---|---|---|
| `POST /api/login/start` | admin | — | `{sid, auth_url}` | 铺 1-5 步 |
| `POST /api/login/send` | admin | `{sid, mobile, country_code="86"}` | `{ok, expires_in, retry_after:60}` | 过 `_send_guarded` |
| `POST /api/login/verify` | admin | `{sid, code, account_id?, force:false}` | `{ok, duplicated, message, uid, check:{live,live_message,format}, account:{id,name,uid,balance_total,balance_remain}}` | `account_id` 非空=重新登录已有账号 |
| `GET /api/login/status/{sid}` | admin | — | `{alive, mobile, country_code, code_sent, consumed, retry_after, age}`；会话没了只回 `{alive:false}` | 前端轮询 |
| `POST /api/join/start` | 公开 | `{}` | `{sid}` | 公开自助注册 |
| `POST /api/join/send` | 公开 | `{sid, mobile, country_code}` | 同上 | 同一套节流 |
| `POST /api/join/verify` | 公开 | `{sid, sms_code}` | `{ok, already, message:"注册成功", masked}` | **不回显 uid/账号 id** |
| `GET /api/join/status/{sid}` | 公开 | — | 同 status | |
| `POST /api/join/official/start` | 公开 | `{}` | `{sid, auth_url}` | 接力模式 |
| `POST /api/join/official/poll` | 公开 | `{sid}` | `{done:false}` / `{done:true,message,masked}` / `{done:false,busy:true}` | 每 2.5s 一次 |
| `GET /api/sms-register/config` | admin | — | 预置项目/引擎 | |
| `GET /api/sms-register/projects` | admin | `?keyword=&engine=&token=` | `{ok, projects[]}` | |
| `POST /api/sms-register/run` | admin | `AutoRegisterIn`（下节） | **SSE 流** | |

### 1.10 接码平台（`admin/sms_client.py`）— 疾驰短信 JichiSMS

```python
DEFAULT_API_URL = "https://www.jichisms.com"
DEFAULT_TOKEN   = os.getenv("JICHI_SMS_TOKEN", "")
DEFAULT_ENGINE  = "a"
DEFAULT_PROJECT_ID_A   = "139372";  DEFAULT_PROJECT_NAME_A = "workbuddy/codebuddy/腾讯科技二次"
DEFAULT_PROJECT_ID_B   = "77560";   DEFAULT_PROJECT_NAME_B = "深圳市腾讯计算机系统"
```
请求头：`User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)`、
`fcToken: <token>`、`Connection: keep-alive`。
重试：最多 3 次，失败 `sleep(1.5*attempt)`，每次前重建 client（SSL 握手自愈）。
**成功判据统一是响应 `code == 1`**（注意：与 WorkBuddy 的 `code == 0` 相反）。

| Method + Path | 参数 | 响应取用 | 失败语义 |
|---|---|---|---|
| `GET /api/user/projects` | query: `page=1, pagesize=50, project_name=<kw>`；engine=b 时加 `engine=b` | `data[]` | 返回 `[]`（不抛） |
| `POST /api/user/getPhone` | form: `project_id`；engine=b 加 `engine=b, project_name`；engine=a 可选 `isp, province, paragraph, ascription` | `data.phone/sid/sp/phone_gsd`（`data` 可能是 list 或 dict，取第一个）；phone 过 `re.sub(r"\D","",phone)` | `SmsClientError(msg, code)` |
| `POST /api/user/getVerifyCode` | form: `project_id, phone`；engine=b 加 `engine=b` | `msg` 里抠 6 位数字：`\b(\d{6})\b` → 回退 `\b(\d{4,6})\b`；无则用整条 msg | 网络异常**不抛**，返回 `{received:False, code:"", msg:"网络重试中 (...)"}` |
| `POST /api/user/releasePhone` | form: `project_id, phone`（+engine=b） | `code==1` → True | 异常吞掉返回 False |

### 1.11 一键接码注册编排（`routers/sms_register.py`）

入参模型 `AutoRegisterIn`（默认值即推荐值）：

```python
token: str|None = None
engine: str = "a"
project_id: str = "139372"
project_name: str = "workbuddy/codebuddy/腾讯科技二次"
count: int = 1                  # 后端 clamp 到 1..50
interval_between: int = 3       # clamp 1..30
card_type: str = "all"          # "all" | "physical"
isp / province / paragraph / ascription: str = ""
max_wait_seconds: int = 60      # clamp 20..120
max_retries_per_account: int = 5# clamp 1..5
poll_interval: int = 5          # clamp 2..6
proxy: str|None = None
```
虚拟号段黑名单（实体卡模式命中即释放重取）：`VIRTUAL_PREFIXES = ("170","171","162","165","167")`；
实体卡模式下 engine=a 且未指定 isp/ascription 时默认补 `isp="移动"`。

单号流程（失败自动换号重试，最多 `max_retries`）：
```
取号(最多 3 次内部重试, 实体卡命中虚拟号→release+重取)
→ start_login(proxy)  (分配独立设备环境)
→ send_sms(sess, phone, "86")
→ 轮询 get_verify_code(每 poll_interval 秒, 最长 max_wait 秒)
→ wb_login.verify_sms(sess, code)
→ _verify_and_store(...) 入库
finally: 未成功则 release_phone()
```
每次发码失败/入库失败都会 `release_phone` 并 `sleep(2)` 后换号。

**SSE 事件契约**（前端按此渲染，Tkinter 版可用回调替代）：
```
event: batch_start   {"total","engine","project_id","project_name","card_type","message"}
event: account_start {"index","total","message"}
event: step          {"index","total","attempt","max_attempts","step","status","phone","sid",
                      "elapsed","max_wait","message"}
                     step ∈ get_phone|init_session|send_sms|poll_code|rotate|verify|store
                     status ∈ running|success|waiting|error
event: account_done  {"index","total","ok":true,"phone","account":{},"uid","message"}
event: account_fail  {"index","total","ok":false,"phone","error","message"}
event: batch_wait    {"index","total","wait_seconds","message"}
event: batch_done    {"total","success_count","failed_count","success_accounts":[],
                      "failed_accounts":[],"message"}
```
响应头：`Content-Type: text/event-stream`（FastAPI `StreamingResponse`）+ `Cache-Control: no-cache`、
`Connection: keep-alive`、`X-Accel-Buffering: no`。单条格式 `"event: {name}\ndata: {json}\n\n"`。

### 1.12 登录后自动收尾（`post_login.py` + `login.py:_run_post_login_automation`）

两个**独立队列**，各 2 个守护线程：

| 队列 | SystemSetting key | 状态值 | 做什么 |
|---|---|---|---|
| 资源队列 | `account_resources:<account_id>` | queued/running/completed/failed/invalid_credential | 刷余额、积分到期快照、首个账号时初始化模型列表（`_do_sync_models`） |
| 奖励队列 | `post_login_automation:<account_id>` | queued/running/completed/failed | 猫猫旅行 → 每日签到 → 成长任务 → 再刷余额 |

- `enqueue` **先落库再提交**，重启后 `resume()` 会把 queued/running 的重新入队（幂等）。
- 顺序固定：`run_cat_travel()` → `get_checkin_status()`（未领才 `claim_daily_checkin()`）→ `growth.run_accounts([id], None, db)` → `fetch_balance()`。
- 每步独立 try，失败只 append 到 `result["errors"]`，**绝不影响登录结果**。
- 资源队列写入时带乐观锁：`WHERE id=? AND auth_json=<读到的原文>`，避免覆盖刚刷新的 token。

---

## 2. 凭证的存储 / 编解码 / 脱敏

### 2.1 存储形态：**原样存 JSON 文本**

| 位置 | 字段 | 说明 |
|---|---|---|
| `accounts.auth_json` | `Column(Text, nullable=False)` | 原始 `.info` 全文（含 token），**不加密、不压缩、不改结构**（`admin/models.py:26`） |
| `accounts.uid` | `String(120)` | 去重键 |
| `accounts.name` | `String(120)` | 优先完整手机号 → 昵称 → uid → "未命名" |
| `accounts.enterprise_id / domain` | String | 从 `auth`/`account` 抽出，供 `X-Enterprise-Id`/`X-Domain` 用 |

入库前必走两级筛：`normalize_credential()`（解密）→ `validate_credential()`（结构）→ `probe_credential_live()`（上游）。
唯一例外：`/api/accounts/import-local` 跳过上游验证（来源是本机客户端自己写的文件）。

**加密只发生在「导出给官方客户端」方向**（`format=client`）。备份方向（`format=backup`）永远明文。
也就是说：**同一份 auth_json 在库里永远是桌面端能读的明文结构**，加解密是导入/导出时的即时转换。

### 2.2 `admin/credential_codec.py` — 官方 `.info` 加密格式（WBEV1）

#### 2.2.1 密钥来源（三级）

```
1) 环境变量 WORKBUDDY_CREDENTIAL_KEY_FILE 指定的 JSON 文件
2) 仓库根 .workbuddy/credential-key.json（形如 {"keyId":"<16 hex>","key":"<base64 32B>"}）
3) 从本机 WorkBuddy.exe 现场提取（仅 Windows / macOS），成功后自动缓存到第 2 项路径
```
提取脚本（node 单行，`ELECTRON_RUN_AS_NODE=1` 执行，超时 10s，stdout ≤ 4096B）：
```js
const crypto = require('node:crypto');
const payload = JSON.parse(process._linkedBinding('electron_browser_workbuddy_storage').loggerGet());
// 断言 payload.version === 1，payload.atRestSecretKey 是长度为 32 字节的随机串（原样参与 sha256）
const raw = Buffer.from(payload.atRestSecretKey, 'base64');
// 断言 raw.length === 32 且 base64 往返一致 且 不全为 0
const key   = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
const keyId = crypto.createHash('sha256').update(key).digest('hex').slice(0, 16);
process.stdout.write(JSON.stringify({keyId, key: key.toString('base64')}));
```
**关键点：密钥 = `sha256(atRestSecretKey 字符串的 UTF-8 字节)`，不是那 32 字节原文。**
`keyId = sha256(key 的 32 字节).hexdigest()[:16]`（Python 侧同样重算并校验，防篡改）。
缓存失效键 = `(可执行文件路径, mtime_ns, size)` —— 升级客户端后自动重新提取。

> 移植提示：Tkinter 工具如果**不需要**导出成官方加密 .info，整章可跳过（这是 node/Electron 依赖最重的一处）。
> 需要时也可让用户手工指定 `credential-key.json`（第 1/2 级来源），完全避开 node。

#### 2.2.2 字段包装格式（逐字节）

单字段密文包装（`_open_field` / `encrypt_credential`）：

```json
{
  "$wbEncrypted": 1,
  "envelope": "<base64( utf8( '{"suite":1,"keyId":"<16hex>","nonce":"<b64 12B>","authTag":"<b64 16B>","ciphertext":"<b64>"}' ) )>"
}
```
`envelope` 里 JSON 用 `separators=(',', ':')` 紧凑序列化后再 base64。字段集必须**恰好**是
`{suite, keyId, nonce, authTag, ciphertext}`，否则视为损坏。`suite` 必须是 int 1。

#### 2.2.3 AAD 构造（必须一字不差）

```python
def text(v):                       # 4 字节大端长度 + UTF-8
    e = v.encode("utf-8"); return struct.pack(">I", len(e)) + e

aad = (b"WB-AAD\0\x01"            # 固定前缀
       + text("WBEV1")            # 算法族
       + text("sym-v1")           # 套件名
       + struct.pack(">I", 1)     # suite 号
       + text(key_id)             # 16 位 hex keyId
       + b"\x02\x00\x00")        # 尾部固定 3 字节
```

#### 2.2.4 加解密步骤

解密（`_open_field`）：
1. `wrapper.get("scheme") is not None` → 抛「高级加密格式不支持」；
2. 校验顶层键集 `{'$wbEncrypted','envelope'}` 且 `$wbEncrypted == 1`；
3. `base64.b64decode(envelope, validate=True)` → `json.loads`；
4. 校验 `envelope['keyId'] == 本机 keyId`（不等 → 「凭证加密版本与本机 WorkBuddy 不匹配」）；
5. `base64` 解 `nonce`(必须 12B)、`authTag`(必须 16B)、`ciphertext`；
6. `AESGCM(key).decrypt(nonce, ciphertext + authTag, aad).decode("utf-8")`。

加密（`encrypt_credential`）：
1. 先 `normalize_credential()`（保证明文）；
2. 断言顶层是 dict 且 `auth` 是 dict；
3. 对每个字段：`nonce = os.urandom(12)`；`sealed = AESGCM(key).encrypt(nonce, value.encode(), aad)`；
   `ciphertext = sealed[:-16]`，`authTag = sealed[-16:]`；**每次导出、每个字段都换新 nonce**；
4. 受保护字段清单（写死，顺序即源码顺序）：
   - `auth.accessToken`、`auth.refreshToken`
   - `account.phoneNumber`、`account.nickname`、`account.departmentFullName`
   - `accounts[]` / `allAccounts[]` 里的同名字段（若有）

#### 2.2.5 `normalize_credential(raw)`

```
- raw.lstrip("\ufeff") 去掉 BOM（Windows 记事本/浏览器导出常见）
- json.loads 失败 → 原样返回 raw（交给 validate_credential 报格式错）
- 递归遍历（深度上限 32，超限抛 "凭证结构嵌套过深"）：
    dict 含 "$wbEncrypted" → 惰性取密钥（只取一次）→ 解密替换该节点
- 有任何解密发生 → 返回 json.dumps(decoded, ensure_ascii=False)；否则原样返回
```

错误类型统一 `CredentialDecodeError`（`ValueError` 子类），**不允许把密文/密钥带进错误信息**。

### 2.3 `admin/account_binding.py` — 不是凭证编解码，而是「账号归属权」

如果目的是「凭证格式」可以跳过；但它决定了**管理台列表能看到哪些账号**，桌面工具若做多 Key 管理需参考：

| 函数 | 语义 |
|---|---|
| `scope_accounts(query, account_id=None, key_id=None)` | 传 `account_id`：要求存在 active 的 `ApiKey(id=key_id, account_id=该账号)`；不传：过滤掉**所有已被任何 Key 独占**的账号（含「别的账号记录 uid 相同」的旧重复行） |
| `validate_binding(db, account_id, key_id=None)` | `account_id` 允许 `0/None/""/0`（= 不绑定）；否则必须正整数且存在；对同 uid 的所有 peer 行 `SELECT ... FOR UPDATE` 串行化后检查冲突 → `HTTPException(409,"该账号已被其他 API Key 独占绑定，请先解除原绑定")` |
| `binding_owners(db)` | 返回 `{account_id: ApiKey}`，重复 uid 映射到同一个 Key |

### 2.4 `desensitize.py` — **注意：与凭证无关**

它是**提示词/消息体脱敏**，用于绕过上游内容审核（`desensitize_messages` / `desensitize_body`）：
- `_ZWSP = "\u200b"` 零宽空格插入到合规高频词内部，打断后端关键词匹配；
- 命中集是英文「拒绝作恶」类术语（`_PATTERN` 由 `_TERMS` 按词长降序编译，`\b` 边界 + 忽略大小写）；
- 另有 Codex/harness 运行时块裁剪（`_HARNESS_USER_MARKERS`、`_RUNTIME_BLOCK_REPLACEMENTS` 等）。

**移植结论：桌面账号管理工具完全不需要这个文件。** 真正用于「凭证/敏感字段展示脱敏」的是零散小函数：

| 函数 | 位置 | 规则 |
|---|---|---|
| `_mask_name(name)` | `routers/login.py:520` | 空 → `"***"`；≤2 → 首字+`*`；≤6 → 首 + `*`*(n-2) + 尾；>6 → 前 2 + `****` + 后 2 |
| `secret_masked` | `routers/sync.py:get_config` | `"*"*8 + secret[-4:]`；长度 ≤4 → `"****"` |
| `_clean_nick(v)` | `routers/login.py:152` | `"null"/"none"/"undefined"`（忽略大小写）一律当空串 |
| `_clean_phone_display(v)` | `routers/login.py:161` | 去掉 `+86`/`86` 前缀；是 `^1[3-9]\d{9}$` 才返回，否则原样 |

---

## 3. 账号导入 / 导出 / 转移

### 3.1 导出（`admin/routers/accounts.py`）

#### 3.1.1 批量导出 API

```
POST /api/accounts/export          (需 admin)
body: {"ids": [], "include_disabled": false, "format": "backup" | "client"}
resp header: Cache-Control: no-store
```
选行逻辑：
- `ids` 非空 → 只取这些 id（**忽略 include_disabled**）；
- `ids` 为空且 `include_disabled=False` → 只取 `status == "active"`；
- 否则全部；按 `Account.id` 升序。

响应：
```json
{"format":"client","total":12,"count":11,"skipped":1,
 "items":["<单个 .info 的 JSON 字符串>", "..."],
 "summary":[{"id":1,"name":"138****","uid":"...","status":"active",
             "balance_total":1000,"balance_remain":800}]}
```
- `items` 里每项都是**独立可再导入的 JSON 文本**；
- `skipped` = 记录里 `auth_json` 为空或不是合法 JSON（`_export_items` 静默跳过，不让整批失败）；
- `format="client"` 时每项过 `encrypt_credential()`，失败抛 `409`（`_client_export`）。

#### 3.1.2 前端落盘格式（`static/index.html:openExport/doExport`）

```js
const text = d.items.map(x => JSON.stringify(JSON.parse(x))).join("\n");   // 逐行一个 JSON = JSONL
const name = `workbuddy-accounts-${d.format === 'client' ? 'encrypted' : 'backup'}-${exportStamp()}.jsonl`;
// exportStamp() → YYYYMMDD-HHmm，例：20260914-1530
mime = 'application/x-ndjson'
```
**没有压缩、没有额外加密层**（client 格式的加密在服务端已逐字段完成）。
下载方式优先级：`window.WBMobile.saveText` → `pywebview.api.save_text` → `<a download>` + Blob（`downloadText`）。
浏览器模式下导出格式下拉**不提供 client 选项**（`${window.WBMobile ? '' : '<option value="client">'}`）。

#### 3.1.3 剪贴板格式（`copyExport`）

```js
const text = d.items.map(x => JSON.stringify(JSON.parse(x))).join("\n");   // 与文件完全相同
await navigator.clipboard.writeText(text);
// 失败（非 HTTPS / 无权限）→ 弹模态框 + 只读 textarea 让用户手动复制
```
即：**剪贴板 = .jsonl 全文（每行一个凭证 JSON）**，可直接粘回「批量上传」文本框。

#### 3.1.4 单账号导出

```
GET /api/accounts/{acc_id}/export?format=backup|client
→ {"id","name","uid","format","item":"<单条 JSON 文本>"}
```
前端保存为 `<安全文件名>-<id>.info`，内容 `JSON.stringify(JSON.parse(item), null, 2)`（**2 空格美化**）；
文件名清洗：`String(name||d.name||d.uid||id).replace(/[\\/:*?"<>|]/g,"_").slice(0,40)`。
移动端强制 `format=backup`。

### 3.2 导入（`admin/routers/accounts.py`）

#### 3.2.1 支持的输入形态（前端解析，`submitUpload`）

1. **单个 JSON 对象** `{"auth":{...},"account":{...}}`；
2. **JSON 数组** `[{...},{...}]`；
3. **JSONL**：每行一个对象（`.info.bak-时间戳` 备份文件也走这条）；
4. **多文件**（`<input type="file" multiple>`）：逐个文件独立解析，**不做拼接**（避免跨文件格式混淆）；
5. 一律先 `stripBOM(s) = s.replace(/^\uFEFF/, '')`。

组装请求体（**注意 auth_json 是对象的字符串化结果**）：
```js
{ items: allItems.map(x => ({
    name: [x.account?.nickname, x.name].find(v => typeof v === "string" && v.trim()) || "",
    auth_json: JSON.stringify(x)
}))}
→ POST /api/accounts/batch
```

#### 3.2.2 `AccountIn` / `AccountBatchIn`（`accounts.py:28`）

```python
class AccountIn(BaseModel):
    name: Optional[str] = None      # 留空则自动取真实昵称/uid
    auth_json: str
    # validator: name 若是 {"$wbEncrypted":1,"envelope":str} 这种加密包装 → 视为 None
                                    # （旧页面会把 account.nickname 直接当名字提交）

class AccountBatchIn(BaseModel):
    items: list[AccountIn]
    verify: bool = True             # 是否逐条真调上游验证；结构校验永远执行
```
单条上传 `AccountIn` 的流程（`add_account`, `accounts.py:715`）：
```
normalize_credential → validate_credential → probe_credential_live
→ 按 uid 查重：命中则 _apply_meta 覆盖 + 刷余额（返回 duplicated:true）
→ 未命中：_apply_meta → 入库 → _refresh_balance(with_expiry=True) → 触发登录后自动任务
```

#### 3.2.3 批量 `POST /api/accounts/batch` 返回

```json
{"added": 5, "skipped": 2, "errors": ["跳过一条：不是有效的客户端凭证（auth.refreshToken 缺失 —— ...）"]}
```
- 每条先 `normalize_credential`（解密失败 → 记 error 继续）；
- 再 `validate_credential`（失败 → error 继续）；
- `verify=True` 时再 `probe_credential_live`（失败 → error 继续），并用**上游返回的真实 uid** 覆盖凭证里声明的 uid；
- `uid in seen`（seen = 库里已有 uid 索引 + 本批已加）→ 覆盖更新并计入 `skipped`；
- 余额刷新失败不算拒绝，只 append 一条警告。

### 3.3 扫描/导入本机客户端登录态

目录解析（`_client_auth_dir`, `accounts.py:248`）：
```
1) 环境变量 ADMIN_CLIENT_AUTH_DIR（支持 ~ 与 %VAR% 展开）
2) macOS: ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth
3) Windows: %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth
```

`GET /api/accounts/scan-local` → 只读、不返回 token：
```json
{"dir":"...", "exists":true, "active_uid":"<workbuddy-desktop.info 里的 uid>",
 "items":[{"file":"workbuddy-desktop.info","uid":"...","nickname":"...","domain":"...",
           "is_active":true,"expires_at":"2026-09-14 15:30:00","expires_ts":1763000000000,
           "credential_error":""}]}
```
- 遍历 `*.info`，跳过 `*.bak` 与含 `.bak-` 的备份；
- 用 `utf-8-sig` 读（自动去 BOM）；
- 每份先 `normalize_credential`（失败记 `credential_error`，仍列出）；
- **按 uid 去重**（保留先遇到的）；昵称过滤字面量 `"null"`；
- `expires_at` = `datetime.fromtimestamp(expiresAt/1000)`。

`POST /api/accounts/import-local` body `{"files":["a.info"],"all":false}`：
```
all 或 files 含 "all" → 列目录下全部 *.info（排除 .bak / .bak-）
逐个：读 → normalize_credential → validate_credential（**不做上游验证**）
     → uid 已存在则 skipped++ → _apply_meta → 入库 → _refresh_balance(with_expiry=True)
返回 {"added":n,"skipped":n,"errors":[...]}
```

### 3.4 注入本机客户端（可选功能）

```
POST /api/accounts/{acc_id}/inject
body: {"confirm": false, "restart": true, "sync_history": true}
```
- `restart=true`（默认）：必须先退出客户端 → 写 `workbuddy-desktop.info` → 再拉起（客户端只在启动时读登录态，运行中还会把旧会话写回同一文件）；
- `sync_history`：是否把 `.info.bak-<时间戳>` 备份一并处理；
- 全局 `_CLIENT_SWITCH_LOCK`（非阻塞 `acquire`）防并发，抢不到 → `409 "客户端正在切换账号..."`；
- 另有 `POST /api/accounts/client/relaunch`（`body {"restart":...}`）。

### 3.5 线上同步（`admin/routers/sync.py`）— 「转移」的真实含义

不是账号转移给别的 Key，而是**本地实例 → 线上实例**的单向推送（本地为 source of truth）。

配置项（`SystemSetting`）：`sync_remote_url`、`sync_secret`。
密钥：`secrets.token_hex(32)`（64 位 hex，256 bit）。首次 `GET /api/sync/config` 自动生成并标记 `is_auto_generated:true`。

#### 推送 `POST /api/sync/push`

```
POST <remote_url>/api/sync/receive
Content-Type: application/json
X-Sync-Signature: hex( HMAC-SHA256(secret, raw_body_bytes) )
body: {
  "source": "workbuddy2api-admin",
  "pushed_at": <unix 秒>,
  "accounts": [{"uid","name","enterprise_id","domain","status","auth_json",
                "balance_total","balance_remain"}],
  "models":   [{"level","model_id","enabled","credit_multiplier","credits_raw","note"}],
  "client_profile": { ...只放非空字段... },
  "client_profile_detected": true|false
}
```
`client_profile` 字段白名单（`sync.py:push`，值为 `None/""/0/{}` 的会被剔除）：
`desktop_version, cli_version, user_agent, ide_name, ide_type, product, fp_product, ext_name,
os, arch, os_version, cpu_cores, memory_size, timezone, report_delay, commit, release_date_ms,
turing_channel_id, turing_product_name, headers`。
> 刻意**不传** `machineId/sessionId`（账号级、应由接收端按自己库里的 uid 派生，否则所有账号共用一台「设备」）。

#### 接收 `POST /api/sync/receive`

```
secret = SystemSetting["sync_secret"]；空 → 400 "线上实例未配置同步密钥，无法接收"
raw = await request.body()
hmac.compare_digest(header X-Sync-Signature, hex(HMAC-SHA256(secret, raw)))  # 失败 401 "签名校验失败"
```
写入策略：
- accounts：按 `uid` upsert（`uid` 或 `auth_json` 空则跳过）；更新 `auth_json/name/enterprise_id/domain/status/balance_*`；
- models：按 `model_id` upsert（`enabled/credit_multiplier/credits_raw/note`）；
- client_profile：`cprofile.save(prof, new_source="saved", db, merge=False)` —— **强制钉成 saved**（线上探测不到客户端）。
返回：`{"ok":true,"accounts":{"added","updated"},"models":{...},"client_profile":{"applied","source","fields","errors"}}`。

### 3.6 客户端参数档案的导入/导出（`profile-transfer.js` + `routers/client_profile.py`）

文档格式（浏览器与两个 app shell 共用同一份）：
```json
{"format":"workbuddy-client-profile","version":1,"source":"saved","profile":{ ... }}
```
- `GET /api/client-profile/export` → 上述文档；导出前 `profile.pop("user_agent")`（UA 由版本号现拼，避免版本错位）；
- `POST /api/client-profile/import`：
  - 必须是 `format=="workbuddy-client-profile"` 且 `type(version) is int and version==1`；
  - 整体 ≤ 256 KB（`len(json.dumps(...).encode('utf-8'))`）；
  - `profile` 过 `cprofile.normalize()`，非空；
  - 任一 header 名或值含 `\r`/`\n` → 400「请求头不能包含换行」；
  - 全部校验通过后才 `save(clean, new_source="saved", merge=False)`（整体替换 + 立即生效）。
- 前端：`<input type=file accept=".json,application/json">`，`file.size > 256*1024` 直接拒，
  `JSON.parse(text.replace(/^\uFEFF/,''))`，校验 `data.format/version/profile`（`profile` 不能是数组）。
- 文件名：`workbuddy-client-profile-<exportStamp()>.json`。

---

## 4. 签到 / 积分 / 福利 / 成长 / 调度

### 4.0 上游端点总表（`admin/backend.py` `AccountSession`）

| 方法 | 完整 URL | body | 走哪个封装 | 返回/判据 |
|---|---|---|---|---|
| POST | `{BACKEND}/v2/billing/meter/get-user-resource` | `{}` | `_request_backend` | `data.Response.Data.Accounts[]`；`total=Σ CapacitySize`，`remain=Σ CycleCapacityRemain`（仅 `CapacityUnit=="credits"`） |
| POST | `{BACKEND}/v2/billing/meter/checkin-activity-status` | `{}` | `_request_backend_soft` | `data`：`active / today_checked_in / end_time / activity_name` |
| POST | `{BACKEND}/v2/billing/meter/daily-checkin` | `{}` | `_request_backend_soft` | `code==0` → `{ok:true, credit, streak_days}`；否则 `{ok:false, code, status, msg}` |
| POST | `{BACKEND}/billing/meter/get-user-request-usage` | `{startTime,endTime,pageNum,pageSize}` | `_request_backend` | 模型请求用量 |
| POST | `{WEB_BASE}/billing/meter/compensation-status` | `{}` | 自带 client | `code==0` 且 HTTP 200；只保留 `active/claimed/credit_num/validity_period/end_time` |
| POST | `{WEB_BASE}/billing/meter/claim-compensation` | `{}` | 同上 | 同上 |
| GET | `{BACKEND}/v2/plugin/accounts` | — | `_request_backend` | 令牌归属（uid/nickname/phone…） |
| GET | `{BACKEND}/activity/growth/tasks` | — | `_growth` | 任务列表 |
| POST | `{BACKEND}/activity/growth/tasks/accept` | `{"task_codes":[...]}` | `_growth` | `{task_code: "accepted"|"already_accepted"|...}` |
| GET | `{BACKEND}/activity/growth/profile` | — | `_growth` | 成长画像 |
| GET | `{BACKEND}/activity/growth/buddy/info` | — | `_growth` | `data.buddy` 为 null = 未领养 |
| POST | `{BACKEND}/activity/growth/buddy/agreement` | `{"agree":true}` | `_growth` | 幂等 |
| POST | `{BACKEND}/activity/growth/buddy/first` | `{}` | `_growth` | **首次领养 +300 积分** |
| GET | `{BACKEND}/activity/growth/buddy/travel/status` | — | `_growth` | `state: idle|traveling|arrived`、`record_id`、`reward_credit` |
| POST | `{BACKEND}/activity/growth/buddy/travel/depart` | `{"location_id":4}` | `_growth` | 4 个地点收益/时长相同，固定用 4 |
| POST | `{BACKEND}/activity/growth/buddy/travel/claim` | `{}` | `_growth` | **必须空对象**（带 record_id 会被网关拒） |
| GET/POST | `{BACKEND}/activity/growth/energy`、`/buddy/quota`、`/buddy/open`、`/heatmap`、`/streak`、`/redeem`、`/lottery/chances`、`/lottery/draw` | — | `_growth` | 成长计划用 |

`_growth(method, path, body)` 的行为（`backend.py:381`）：
- 不抛异常，统一返回 `{"ok": bool, "status": int, "code": int|None, "msg": str, "data": dict}`；
- `ok = (status==200 and code==0)`；
- **growth 域的业务失败走 HTTP 400**（如领养门槛），需读 `msg`；
- 门槛关键词：`BUDDY_TASK_INCOMPLETE_MARKER = "first_buddy task not completed yet"`。

签到业务码映射（`backend.py:1731` `_map_checkin_status`）：

| code | status | 含义 |
|---|---|---|
| `1001` | `already_claimed` | 今日已领取 |
| `1002` | `not_eligible` | 无领取资格 |
| `1003` | `event_ended` | 活动已结束 |
| 其它 | `unknown` | |

### 4.1 每日签到 `run_daily_checkin(db, schedule)`（`scheduler.py:593`）

```
若 schedule.stop_after 已过 → 直接返回 {"skipped":"已超过停止领取时间，不再请求"}，不发任何请求
取 status=="active" 的账号，按 id 升序
for 每个账号:
    with AccountSession(auth_json) as sess:
        st = sess.get_checkin_status()
        if st["today_checked_in"]:  skipped_already++ ; detail += {status:"already", reason:"今日已领过"}
        else:
            res = sess.claim_daily_checkin()
            if res.ok:
                credit = int(res["credit"] or 0); claimed++; total_credit += credit
                record_result(db, a.id, {"ok":True,"credit":credit}, source="schedule", action="每日打卡")
                随后: fetch_balance() → 写 balance_total/balance_remain/last_sync_at
                      attach_balance(db,[reward_record], balance_remain)
                      _sync_credit_snapshot(a, sess.fetch_credit_details())
                      （这段失败只记 "{label}:奖励已领取，余额或到期快照刷新失败"，不影响已领取事实）
            elif res["status"] == "event_ended": ended=True; failed++ ; detail {status:"event_ended"}
            else: failed++ ; reason = res["status"] or res["msg"]
        a.auth_json = sess.updated_json()   # 签到可能触发 token 刷新，必须写回
        db.commit()
若 ended 且 schedule 存在 → schedule.stop_after = utcnow()，之后不再请求（防风控）
```
返回结构（前端/Tkinter 直接渲染这张表）：
```json
{"task":"daily_checkin","ok":true,"total":N,"claimed":N,"skipped_already":N,"failed":N,
 "credit":N,"activity_ended":false,"errors":["账号A:活动已结束", "..."],
 "detail":[{"account_id":1,"account":"13800138000","ok":true,"status":"claimed",
            "credit":100,"streak_days":3,"reason":"+100 积分"}],
 "summary":"今日新领 3 个账号，共 +300 积分，1 个今日已领过"}
```

### 4.2 积分（到期口径，`admin/credits.py`）

```python
EXPIRING_SOON_DAYS = 7          # 「7 天内到期」阈值
EXPIRY_CYCLE_OVERRIDE_DAYS = 365# DeductionEndTime 比 CycleEndTime 晚超过 1 年 → 前者是占位值
FAR_FUTURE_EXPIRY_DAYS = 730    # 到期时间距 now 超过 2 年 → 视为长期有效（返回 None）
```
`resolve_expire_at(raw)` 判定顺序（**必须照抄，这是「剩余额度永不过期」bug 的修正点**）：
1. `deduction_end = parse_timestamp_ms(首个非空 of DeductionEndTime, deductionEndTime, ExpiredTime, expiredTime)`
2. `cycle_end = parse_timestamp_ms(首个非空 of CycleEndTime, cycleEndTime)`
3. 两者都有且 `deduction_end - cycle_end > 365 天` → 用 `cycle_end`；否则用 `deduction_end`
4. 只有其一 → 用它
5. `expire_at - now > 730 天` → 返回 `None`（长期有效）

容量字段多套并存，取值键（`admin/credits.py:239`）：
`_SIZE_KEYS  = ("CycleCapacitySizePrecise","CycleCapacitySize","CycleTotalCapacity","CapacitySizePrecise", ...)`
`_REMAIN_KEYS= ("CycleCapacityRemainPrecise","CycleCapacityRemain","CycleRemainCapacity","CapacityRemainPrecise", ...)`
`_USED_KEYS  = ("CycleCapacityUsedPrecise","CycleCapacityUsed","CycleUsedCapacity","CapacityUsedPrecise", ...)`
取数用宽松解析（上游常把容量写成字符串 `"459.49000428"`）。
快照落库字段：`credits_expiring / credits_expiring_soon / credits_evergreen / credits_expired /
credits_soonest_expire_at / credits_snapshot(JSON 文本) / credits_synced_at`。
`_sync_credit_snapshot(acc, packages)` 在 `accounts.py:351`，来源不完整时**保留上次完整结果**（不覆盖成 0）。

### 4.3 福利 / 补偿（`admin/compensation.py` + `admin/routers/benefits.py`）

活动常量（写死即可，过期自然失效）：
```python
CAMPAIGN = "deepseek-202610"
TITLE    = "DeepSeek 模型服务异常额外补偿"
CREDIT   = 500
TASK     = "claim_compensation"          # 旧任务名，会被 ensure_schedule 迁移
DAY_TZ   = timezone(timedelta(hours=8))
END      = datetime(2026, 10, 22, 23, 59, 59, tzinfo=DAY_TZ)
RECHECK_SECONDS = 600                     # 单账号复检冷却
```
每账号流程：`compensation_status()` → 若 `active and not claimed` → `claim_compensation()`；
`_refresh_resources(account, session, result)` 在领取成功后顺带刷余额。
幂等键 `_key(account, offer)` + `cached(db, account)`（DB 缓存，避免重复打上游）。

HTTP 端点（`router = APIRouter(prefix='/api/benefits', dependencies=[Depends(require_admin)])`）：

| 端点 | 入参 | 说明 |
|---|---|---|
| `GET /api/benefits/compensation` | — | `{campaign:{...}, items:[{id,name,enabled,...,cached状态}]}` |
| `POST /api/benefits/compensation/accounts/{id}/refresh` | — | 只查不领（`claim=False`） |
| `POST /api/benefits/compensation/accounts/{id}/claim` | `{"confirm":true}` | 未 confirm → 400 |
| `POST /api/benefits/compensation/batch` | `{"confirm":true,"ids":[...]|null}` | 异步 job，key=`compensation-claim`；`ids` 最多 5000；活动过期 → 400；已有 running job → 复用返回 `{reused:true}` |

每日福利发现任务（`admin/benefits.py`）：
```
TASK = 'claim_benefits'；ensure_schedule: 若不存在则建 Schedule(name='每日检查并领取权益',
    interval_minutes=1440, next_run_at=utcnow()+45s)，并把所有旧的 claim_compensation 计划删掉
run_scheduled(db) → compensation.run_batch(db, source='schedule', discover=True)
```

### 4.4 成长任务（`admin/routers/growth.py` + `admin/growth_plans.py`）

#### 4.4.1 任务分级

```python
AUTO   = "auto"     # 可直接自动完成
MULTI  = "multi"    # 需重复触发
MANUAL = "manual"   # 暂不可自动（例如需要客户端 GUI）
SKIP   = "skip"     # 跳过
```
`classify(task)` 输出（`growth_plans.py:342`）：
```python
plan = plan_for(task_code, task)
level = plan.level
if reward <= 0 and level != SKIP: level = SKIP      # 奖励为 0 一律降级
if task.get("locked"): level = MANUAL
actionable = plan.actionable and level in (AUTO, MULTI)
need_times = (target - current) if 是 int 且 target > current else (plan.times or 1)
→ {**task, "level","level_label","actionable","strategy":plan.reason,"need_times"}
```

#### 4.4.2 节流与预算常量（照抄这些数字）

```python
_TASK_CACHE_TTL      = 60.0    # 任务定义缓存
_ACCT_TASKS_TTL      = 3.0     # 单账号任务列表缓存
_EVENT_GAP           = 1.2     # 同一任务内两次触发之间
_ACCOUNT_GAP         = 1.0     # 两个账号之间
_POLL_INTERVAL       = 0.5     # 轮询间隔
_ACCEPT_TIMEOUT      = 6.0     # 等 accept 状态落库
_CLAIM_WAIT          = 8.0     # 等领奖结果
_MAX_TIMES           = 10      # 单任务最多触发次数
_ACCOUNT_BUDGET      = 300.0   # 单账号预算 5 分钟
_SYNC_SOFT_BUDGET    = 50.0    # 同步请求软预算
_SCHEDULE_SOFT_BUDGET= 1500.0  # 定时任务整批软预算 25 分钟
_PREFERRED_MODELS    = ["hy3", "hunyuan-chat"]   # 优先免费模型（credit_multiplier == 0）
```

#### 4.4.3 `run_accounts(account_ids, task_codes, db, ...)` 主循环（`growth.py:269`）

```
model = _pick_free_model(db)          # 0 倍率优先：hy3 → hunyuan-chat → 任一 0 倍率 → "hy3"
for aid in _ordered_ids(account_ids, db):
    if 整批软预算已用完 → 该账号 ok=False, msg="整批已用满本次时间预算，剩余账号留待下次"
    deadline = min(now+300s, batch_start+soft_budget)
    with AccountSession(auth_json) as s:
        tasks = s.growth_tasks()
        gate = by_code["first_buddy"]
        if gate and gate.accept_status not in ("completed","claimed"):
            s.run_cat_travel()            # 前置必须真有一只猫，不能只看任务状态
            tasks = s.growth_tasks()      # 重新拉
        wanted = task_codes or [t.code for t in tasks
                                if classify(t).actionable and accept_status not in ("claimed","completed")]
        accept_codes = [c for c in wanted if actionable and accept_status == "not_accepted"]
        accepted = s.growth_accept(accept_codes) if accept_codes else {}
        # 若被 first_buddy 挡：再跑一次猫猫旅行并重试 accept
        jobrunner.wait_for(所有 accepted 的 accept_status != not_accepted, 6s, 0.5s)
        for code in wanted:
            accept_status == "claimed"    → ok=True, skipped="已领取"（不发请求）
            accept_status == "completed"  → ok=True, status="completed"（等领奖）
            not actionable                → ok=True, skipped=plan.reason
            accept 失败                   → ok=False, accept_failed, accept_message
            accept_status == "not_accepted"→ ok=False, skipped="参与状态尚未落库，留待下次"
            超 deadline                   → ok=False, skipped="本账号超时，剩余任务留待下次" + budget_exceeded
            times = clamp(plan.times or (target-current), 1, 10)
            for i in range(times):
                r = getattr(s, plan.firer)(**plan.firer_kwargs) 或 s.growth_fire_event(plan.event_codes, model=...)
                if not r.ok: 记 last_error 并 break（不要把一个超时的触发重复 10 次）
                fired += 1; sleep(_EVENT_GAP)
        # 触发完再统一领奖：claim_accounts(...)（不把「事件上报成功」当成任务完成）
    sleep(_ACCOUNT_GAP)
```
特殊：`plan.firer == "fire_design_canvas"` 前必须先 `s.ensure_ardot_connected()`，否则报「账号未能绑定 Ardot」。

#### 4.4.4 成长相关 HTTP 端点

| 端点 | 入参 | 出参/说明 |
|---|---|---|
| `GET /api/growth/tasks?refresh=false` | — | `{tasks:[classify 后], synced_at, source_account_id, cached}`；刷新失败回退旧缓存并带 `stale:true, error` |
| `POST /api/growth/accept` | `{account_ids[], task_codes[]}` | 逐账号 `{account_id, ok, results}`，账号间隔 1.0s |
| `POST /api/growth/run` | `{account_ids[], task_codes?}` | 同步执行 `run_accounts` |
| `POST /api/growth/run-async` | 同上 | 返回 job（`JOB_KEY="growth_run"`），`GET /api/growth/job/{job_id}` 查进度 |
| `POST /api/growth/claim` | `{account_ids[], task_codes?}` | 只领奖 |
| `GET /api/growth/accounts/{id}/tasks?refresh=` | — | 单账号任务（带 level/actionable/need_times） |
| `GET /api/growth/plans` | — | 策略表视图 |
| `GET /api/growth-center/accounts` / `/accounts/{aid}` | — | 成长中心总览 |
| `POST /api/growth-center/accounts/{aid}/refresh` | — | 现场拉取 + 落库 |
| `GET /api/growth-center/accounts/{aid}/records?before=&limit=20` | — | 本地奖励流水 |
| `GET /api/growth-center/accounts/{aid}/official-records?kind=travel&page=` | — | 上游流水 |
| `POST /api/growth-center/accounts/{aid}/action` | `ActionIn` | 单动作（签到/旅行等） |

### 4.5 调度器（`admin/scheduler.py`）

#### 4.5.1 `Schedule` 表字段（`models.py:294`）

```
id, name, task, interval_minutes(default 60), timing_config(JSON 文本，空=系统预设),
enabled(0/1), last_run_at, next_run_at, last_result(文本摘要，_RESULT_LIMIT=20000 截断),
stop_after(DateTime，签到「停止领取时间」), created_at, updated_at
```

#### 4.5.2 `run_task(task, db, schedule)` 任务注册表（`scheduler.py:83`）

| task | 处理函数 | 默认周期 | 说明 |
|---|---|---|---|
| `refresh_balances` | 遍历 active 账号 `_refresh_balance(with_expiry=True)` | 60 min | 结果含 `refreshed/failed/skipped/skip_reasons/credits_expiring` |
| `refresh_credits` | 逐号 `fetch_credit_details` + `_sync_credit_snapshot` | 120 min（+3min 偏移） | 选号「先用快过期」的数据来源 |
| `sync_models` | `models._do_sync_models(db)` | 1440 min | |
| `daily_checkin` | `run_daily_checkin(db, schedule)` | 1440 min | 见 4.1 |
| `claim_benefits` | `benefits.run_scheduled(db, schedule)` | 1440 min | 见 4.3 |
| `refresh_growth_tasks` | `run_refresh_growth_tasks()` | 1440 min | 只用一个账号拉任务定义 |
| `run_growth_tasks` | `run_growth_tasks()` | 1440 min，排刷新后 **180s** | 整批 `soft_budget=_SCHEDULE_SOFT_BUDGET` |
| `run_growth_plan` | `growth_plan.run_growth_plan(db)` | 60 min | 独立线程 + 10 分钟租约 |
| `keepalive_tokens` | `run_keepalive_tokens(db)` | 每天指定整点（默认 22:00） | token 保活 |
| 未知 | — | — | `{"task":..., "error":"未知任务类型"}` |

**WAF/出口保护早退**：任何任务开头都判 `POOL.waf.active()`，成立则返回
`{"ok":true,"deferred":true,"retry_after":N,"summary":"出口保护期间暂停后台上游任务"}`。

#### 4.5.3 默认种子（`seed_defaults`，仅当 `Schedule` 表为空）

| name | task | interval_minutes | next_run_at |
|---|---|---|---|
| 整点刷新平台总积分 | `refresh_balances` | 60 | now |
| 每 2 小时更新积分到期快照 | `refresh_credits` | 120 | now + 3 min |
| 每日同步模型列表 | `sync_models` | 1440 | now |
| 每日签到领取积分 | `daily_checkin` | 1440 | now |
| 每日更新成长任务列表 | `refresh_growth_tasks` | 1440 | now |
| 每日自动做成长任务 | `run_growth_tasks` | 1440 | now + 180 s |

`ensure_*` 系列（每个都幂等，用于老库补齐）：`ensure_credit_schedule`、`ensure_growth_plan_schedule`、
`ensure_daily_checkin`、`ensure_growth_schedules`（**强制** `run.next_run_at >= refresh.next_run_at + 180s`）、
`ensure_keepalive_schedule`、`benefits.ensure_schedule`。

#### 4.5.4 调度循环与重试/冷却语义

```
_loop(): 每 15 秒
    schedules = enabled==1 的全部，排序：run_growth_plan 优先（先起线程，别被长任务拖住）
    for s: if s.next_run_at is None or s.next_run_at <= now: _run_one(s, db, now)
    DB 异常 → 静默跳过本轮（不能从 except 里再次抛出把线程干掉）

_run_one(s):
    WAF 激活且非 keepalive → next_run_at = now + max(15, waf.remaining())，写 deferred 摘要，返回
    task == run_growth_plan → start_growth_plan(s, db)（线程 + 租约，next_run_at = now+10min）
    task == keepalive_tokens 且 timing 为 preset:
        若 SystemSetting["keepalive:pending"] 为空 且 not _keepalive_due(s, now)
            → next_run_at = _next_keepalive_at(now)（正常等待，不算跳过）
        否则执行；执行后 next = now+1min（若 retry_pending）否则 _next_keepalive_at(now)
    其它: result = run_task(...)；last_result = json 摘要（截断 2000/20000）；
          last_run_at = now；next_run_at = next_run_at(s, now)
```
**没有指数退避**：失败也是按 `interval_minutes` 正常推进，只有 keepalive 的 `retry_pending` 用 1 分钟快重试。
keepalive 的「到期窗口」语义（修过的 bug）：错过整点只「晚跑」，不会「永远不跑」——
判据是「距上次成功执行超过一个周期，或已过今天计划时刻」。

#### 4.5.5 健康观测（`admin/health.py`，可选）

```
risk_health(db, now): 近 15 分钟 UsageLog 按 error_kind 分组 → {window_minutes:15, request_count,
    observations:{kind:{requests,accounts}}, waf_active, waf_remaining_seconds, maintenance, source:"gateway_only"}
model_health(db, hours=24): 逐模型 → eligible_accounts / available_accounts / limited_accounts /
    earliest_model_recovery / success_rate / failure_reasons / ttfb_ms{p50,p95} / latency_ms{p50,p95}
    （分位数只取最近 2000 条样本；free = credit_multiplier == 0）
```
判定「健康账号」：`status=="active"` 且 `last_err_msg` 非登录失效标记 且 `cool_until/breaker_until/degrade_until` 均未到期。

---

## 5. 设备指纹 / UA / device_id 的构造

### 5.1 登录侧：`admin/device_env.py`（每次登录会话一个全新身份）

```python
@dataclass DeviceEnv:
    session_id = uuid4().hex
    device_id  = uuid4().hex
    client_uuid= str(uuid4())
    platform   = "macOS" | "Windows"     # macOS 概率 0.65
    user_agent, sec_ch_ua, sec_ch_ua_platform, sec_ch_ua_platform_version,
    accept_language, client_ip, proxy
```
生成规则：
- `chrome_major = randint(130,134)`；`chrome_patch = "{major}.0.{randint(6700,6999)}.{randint(50,199)}"`
- macOS: `Macintosh; Intel Mac OS X {10_15_7|14_7_1|15_2_0|15_3_1}`，`Sec-Ch-Ua-Platform-Version` 取 `"14.7.1"|"15.2.0"|"15.3.1"`
- Windows: `Windows NT 10.0; Win64; x64`，platform-version 取 `"10.0.0"|"15.0.0"`
- `Sec-Ch-Ua` 两种模板随机（见 `device_env.py:99`）：`"Chromium";v="{M}", "Google Chrome";v="{M}", "Not?A_Brand";v="99"` 或
  `"Google Chrome";v="{M}", "Chromium";v="{M}", "Not-A.Brand";v="24"`
- `client_ip`：从 20 个国内公网段前缀随机抽一个 + 两段随机数（仅用于 `X-Forwarded-For` 等 header，**不真的改路由**）

`build_headers(base_host, referer=None, extra=None)` 输出的完整头集（顺序无关）：

```http
User-Agent: <生成值>
Accept-Language: <6 种 zh-CN 变体之一>
Accept: application/json, text/plain, */*
Origin: <base_host>
Referer: <referer or base_host + "/">
Sec-Ch-Ua: <生成值>
Sec-Ch-Ua-Mobile: ?0
Sec-Ch-Ua-Platform: "macOS" | "Windows"
Sec-Fetch-Site: same-site
Sec-Fetch-Mode: cors
Sec-Fetch-Dest: empty
X-Device-Id: <device_id>
X-Client-Id: <client_uuid>
X-Request-Id: <uuid4().hex>
X-Forwarded-For / X-Real-IP / Client-IP: <client_ip>     # 仅在未配置 proxy 时添加
```
调用方（`wb_login`）再按端点覆盖 `Accept`（html 时 `text/html,*/*`）、加 `Content-Type`、`X-TOA-LANG`、
`X-Requested-With`、`X-No-Authorization` 等；每个请求都会重新生成 `X-Request-Id`。

### 5.2 网关侧：`admin/client_profile.py`（全局共用的「客户端身份」）

`DEFAULTS`（`client_profile.py:79`，逐字段照抄）：
```python
desktop_version = "5.5.6"
cli_version     = "2.137.1"
turing_product_name = "WorkBuddy"
ide_name  = "WorkBuddy"
ide_type  = "WorkBuddy"
product   = "SaaS"            # ⚠️ X-Product 是部署形态，不是产品名；填 "WorkBuddy" 是错的
application_name = "WorkBuddy"
fp_product = "SaaS"           # 桌面事件指纹里的 product（遥测口径）
headers = {
    "X-CodeBuddy-Request": "1",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "zh-CN",
    "X-Private-Data": "false", # false=允许用于模型优化（官方默认）；true=禁止
}
ext_name = "workbuddy-desktop"; os = "win32"; arch = "x64"; os_version = "10.0.26220"
cpu_cores = 20; memory_size = 24; timezone = "Asia/Shanghai"; report_delay = 2000
```
取值优先级：**环境变量 > （现场探测 / 已保存，取决于 source）> DEFAULTS**。
`source ∈ {"auto","saved"}`；`_CACHE_TTL = 30.0` 秒，保存时立即失效。
环境变量映射（`_ENV_FOR`）：`WORKBUDDY_DESKTOP_VERSION`、`WORKBUDDY_CLI_VERSION`、`WORKBUDDY_TURING_CHANNEL_ID`、
`WORKBUDDY_TURING_PRODUCT_NAME`、`WORKBUDDY_COMMIT`、`WORKBUDDY_RELEASE_DATE_MS`、`WORKBUDDY_OS`、`WORKBUDDY_ARCH`、
`WORKBUDDY_OS_VERSION`、`WORKBUDDY_CPU_CORES`、`WORKBUDDY_MEMORY_SIZE`、`WORKBUDDY_IDE_NAME`、`WORKBUDDY_EXT_NAME`、
`WORKBUDDY_USER_AGENT`（唯一「整条 UA 覆盖」入口）。

**UA 拼接（三段式，必须一字不差）**：
```python
f"{brand}/{v} {brand}/{v} CLI/{c}"     # brand=application_name，例：
# WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1
```

**风控归属头 `risk_headers()`**（只在 `/v3/config` 这类请求上加，`converter.py:470`）：
```python
{"X-IDE-Name": ide_name, "X-IDE-Type": ide_type, "X-Product": product, "X-IDE-Version": desktop_version}
# 再把 profile["headers"] 用 setdefault 合并（不能覆盖上面四个）
```

**桌面事件指纹 `fingerprint(uid, machine_id, session_id)`**（遥测上报用）：
```json
{"timezone":"Asia/Shanghai","reportDelay":2000,"product":"SaaS","ideName":"WorkBuddy",
 "ideType":"WorkBuddy","ideVersion":"5.5.6","machineId":"<36 hex>","sessionId":"<36 hex>",
 "extName":"workbuddy-desktop","extVersion":"5.5.6","os":"win32","arch":"x64",
 "osVersion":"10.0.26220","cpuCores":20,"memorySize":24,
 "releaseDate":<ms，有值才写>,"commit":"<有值才写>"}
```

### 5.3 device_id 的稳定派生（`backend.py:184`）

```python
def stable_device_id(uid: str, salt: str) -> str:
    return hashlib.md5(f"{salt}:{uid}".encode()).hexdigest()[:36]     # 36 位 hex
machine_id = stable_device_id(uid, "machine")
session_id = stable_device_id(uid, "session")
```
理由（源码注释）：同一账号在服务端眼里必须始终是同一台设备；每次随机=频繁换设备=异常；
所有账号共用一个常量更糟（最容易被批量识别）。**不要用随机 UUID 当 machineId。**

### 5.4 每个上游业务请求的头（`converter.py:392` `_build_headers_from`）

```http
Content-Type: application/json
Accept: application/json
Authorization: Bearer <auth.accessToken>
X-User-Id: <account.uid>
X-Enterprise-Id: <account.enterpriseId>
X-Tenant-Id: <account.enterpriseId>
X-Domain: <auth.domain 或 www.codebuddy.cn>
User-Agent: <client_profile.ua()>
X-Device-Token: <turing token，取不到就不加>
```
代理/调用方还可 `extra` 注入：`X-Agent-Purpose: conversation`、`X-IDE-Name`、`X-IDE-Type`、`X-Product`、
`X-Forwarded-For`/`X-Real-IP`/`X-Client-IP`（`converter.py:82-105`）。

### 5.5 token 刷新（签到等长任务必须能自己续期）

```
触发条件：now_ms >= (expiresAt - 60_000)   # 提前 60 秒判过期
POST {BACKEND}/v2/plugin/auth/token/refresh
headers: <上述基础头> + X-Refresh-Token: <auth.refreshToken> + X-Auth-Refresh-Source: plugin
body: {}
成功: data 即新 auth；若无 refreshToken 字段 → 沿用旧的；合并 domain/lastRefreshTime，
      缺 expiresAt 用 now+expiresIn*1000 补，缺 refreshExpiresAt 同理
校验: 新的 {'auth': new_auth} 必须再过一次 _validate_auth（不完整则保留原凭证并报错）
落盘: 原子写（<path>.tmp → os.replace）
```

### 5.6 `X-Device-Token`（Turing Shield，**可省略**）

```
链路：Python → subprocess.run([node, turing_helper.js], timeout=8s) → stdout 单行 JSON {"token":"v3:..."}
SDK 目录发现顺序：
  1. WORKBUDDY_TURING_SDK_DIR（可直接指 turing-sdk 目录或安装基目录）
  1b. WORKBUDDY_INSTALL_DIR
  2. %LOCALAPPDATA%/%APPDATA%/%ProgramFiles%/%ProgramFiles(x86)%/%USERPROFILE% 下的 WorkBuddy|workbuddy
  3. 各盘根（WORKBUDDY_TURING_DRIVES，默认 C,D,E,F）下 workbuddy|WorkBuddy
SDK 相对路径候选：resources/app.asar.unpacked/native/turing-sdk、
                 resources/native/turing-sdk、Contents/Resources/app.asar.unpacked/native/turing-sdk、
                 Contents/resources/app.asar.unpacked/native/turing-sdk
配置读取：resources/app.asar.unpacked/cli/product.json 的 config.turingSdk.channelId、genieVersion；
         其次 resources/install-manifest.json 的 appVersion
兜底值：FALLBACK_DESKTOP_VERSION="5.5.6"，FALLBACK_TURING_CHANNEL_ID=109144
缓存：成功 600s（_CACHE_TTL），失败负缓存 300s（_FAIL_TTL），单次硬超时 8s
```
**重要事实（源码实测注释）**：`X-Device-Token` 缺失时上游 `spi_extra` 退化成不带风控态，**短信照常下发**，
签到/常规业务也不强制。→ Tkinter 版可以整块跳过，只保留「取不到就不发该头」的优雅降级。
（若坚持要，Windows 上也可直接用 `WinHTTP`/读安装目录 + node，但会引入 Electron 依赖。）

---

## 6. 管理台 UI 动作清单（按钮 → 函数 → API），用于对齐功能完整性

> 全部来自 `admin/static/index.html`（单文件 439 KB，内联全部 JS）。管理端鉴权头是 `X-Admin-Token`，
> 前端从 `localStorage[TOKEN_KEY]` 取。`desktop.js` **只负责窗口/更新**（pywebview 原生窗口、检查更新、下载安装包），
> 与账号功能无关，Tkinter 版无需移植。

### 6.1 账号页顶部工具栏

| 按钮文案 | 前端函数 | 触发 API |
|---|---|---|
| 一键接码注册 / 自动注册 | `openAutoRegister()` | `POST /api/sms-register/run`（SSE）+ `/api/sms-register/config`、`/api/sms-register/projects` |
| 手机号登录 | `openPhoneLogin([accountId])` | `POST /api/login/start` → `/send` → `/verify`；`GET /api/login/status/{sid}` 恢复 |
| 导入账号 | `openUpload()` → `submitUpload()` | `POST /api/accounts/batch` |
| 扫描本机 | `scanLocal()` | `GET /api/accounts/scan-local` → `POST /api/accounts/import-local` |
| 导出 | `openExport()` → `doExport()` / `copyExport()` | `POST /api/accounts/export` |
| 手动添加 | `openAddAccount()` | `POST /api/accounts`（body `{name?, auth_json}`） |
| 筛选：全部 / 已启用 / 已禁用 | `setAccountFilter('all'\|'active'\|'disabled')` | 纯前端过滤 `_accData` |
| 执行中 · 查看进度 | `showAccountJob()` | 读 job（`refresh-batch` / `cat-travel/batch-async` / `growth_run`） |
| 批量刷新额度 | `accountBatchAction(refreshAll)` | `POST /api/accounts/refresh-batch` |
| 领取 DeepSeek 补偿 | `WBBenefits.claimAll()` | `POST /api/benefits/compensation/batch` `{confirm:true}` |
| 模型额度与冷却 | `openModelLimits([id])` | `GET /api/accounts/model-limits`；行内 `POST /api/accounts/{id}/model-limits/clear` |
| 批量做成长任务 | `accountBatchAction(growthRunBatch)` | `POST /api/growth/run-async`（job `growth_run`，轮询 `GET /api/growth/job/{job_id}`） |
| 批量猫猫旅行 | `accountBatchAction(catTravelBatch)` | `POST /api/accounts/cat-travel/batch-async` |

### 6.2 账号行内动作（行菜单 / 行内按钮）

| 动作 | 前端函数 | API |
|---|---|---|
| 刷新余额/积分 | 行内刷新 | `POST /api/accounts/{id}/refresh?with_expiry=true` |
| 积分明细 | `creditDetails(id)` | `GET /api/accounts/{id}/credit-details?refresh=` |
| 成长任务 | `growthTasks(id, name)` | `GET /api/growth/accounts/{id}/tasks?refresh=`；单任务 `完成`→`POST /api/growth/run` `{account_ids:[id],task_codes:[code]}`；`领奖`→`POST /api/growth/claim` |
| 猫猫旅行 | `catTravel(id)` | `POST /api/accounts/{id}/cat-travel` |
| 导出该账号 | `exportAcc(id, name)` | `GET /api/accounts/{id}/export?format=client\|backup` |
| 重新登录（保号） | `openReauthAccount(id)` → `submitReauthAccount(id)` | `POST /api/accounts/{id}/reauth` `{auth_json}`（或先走手机号登录并带 `account_id`） |
| 改名/改手机号 | `renameAccount(id, cur)` | `PATCH /api/accounts/{id}` |
| 启用/禁用 | 行内开关 | `PATCH /api/accounts/{id}` `{status:"active"\|"disabled"}` |
| 删除 | 行内删除 | `DELETE /api/accounts/{id}` |
| 清除网关冷却 | `clearAccountCooldown(id)` | `POST /api/accounts/{id}/cooldown/clear` |
| 请求用量 | `requestUsage(id)` | `GET /api/accounts/{id}/request-usage` |
| 注入本机客户端 | 行内 | `POST /api/accounts/{id}/inject` `{confirm,restart,sync_history}` |

### 6.3 手机号登录弹窗（3 步状态机，Tkinter 版建议照抄）

```
Step1 (plStep1): 地区下拉 86/852/853 + 手机号输入（86 → maxlength 11，852/853 → 8）
   按钮「获取验证码」→ plSend()
       sid 为空时先 plStart(): POST /api/login/start → {sid, auth_url}
       再 POST /api/login/send {sid, mobile, country_code}
       成功: 切到 Step2；按钮进入 N 秒倒计时（N = 响应里的 retry_after，默认 60）
       429: 读 detail 文本 + Retry-After 继续倒计时
   按钮「收不到验证码？使用官方安全通道登录」→ plOfficialStart()
       POST /api/join/official/start → {sid, auth_url} → 内嵌/外开 auth_url
       轮询 POST /api/join/official/poll {sid} 每 2.5s
         {done:false} / {done:false,busy:true} → 继续
         {done:true, masked} → 成功，回到账号列表
Step2 (plStep2): 6 位验证码输入 + 「重新获取」+「登录并上传」→ plVerify()
       POST /api/login/verify {sid, code[, account_id]}
       成功: Step3 显示 message / live_message，然后 loadAccounts()
Step3 (plStep3): 结果页 + 关闭

会话持久化: localStorage['wb_admin_phone'] = {sid, mobile, country, retryUntil}
   重开弹窗时若 sid 存在 → GET /api/login/status/{sid}
     alive && !consumed && code_sent → 直接进 Step2 并回填手机号/倒计时
     !alive || consumed → 清掉本地状态
弹窗关闭策略: dataset.closeOnly='1'（点遮罩不关，防误触丢失会话）
```

### 6.4 一键接码注册弹窗

前端参数（`localStorage` 键 → 请求字段）：

| UI 控件 | localStorage | 请求字段 | 默认 |
|---|---|---|---|
| 注册数量（1..50，快捷 1/2/5/10） | `ar_count` | `count` | 1 |
| 通道与项目下拉（a_139372 / b_77560 / a_142759 / b_71139 / custom） | `ar_project_id`,`ar_project_name` | `engine`,`project_id`,`project_name` | a / 139372 |
| 卡号类型（physical / all） | `ar_card_type` | `card_type` | all |
| 超时换号（20..120 秒） | — | `max_wait_seconds` | 60 |
| 接码平台 Token | `ar_token` | `token` | 空（后端用 `JICHI_SMS_TOKEN`） |
| 独立代理 | — | `proxy` | 空 |
| 隐藏字段 | — | `max_retries_per_account`,`interval_between`,`poll_interval:3` | 5 / 3 / 3 |

请求头：`Content-Type: application/json` + `X-Admin-Token`；用 `AbortController` 支持「停止」。
流式读取：`response.body.getReader()` + `TextDecoder` 逐块解析 `event: / data:` 行。
UI 元素：`#arStatusArea`、`#arLogBox`、`#arProgressBadge`（`i / total`）、`#arProgressText`、`#arSubText`、
`#arStartBtn`、`#arStopBtn`、`#arCloseBtn`、`#arSpinner`、`#arSuccessItems`。

### 6.5 导出弹窗（`openExport`）

| 控件 | 行为 |
|---|---|
| 导出格式下拉 | `client`=官方新版加密凭证集（.jsonl，浏览器模式隐藏此选项）/ `backup`=系统明文备份（.jsonl） |
| 包含已禁用账号 | `include_disabled` |
| 复制到剪贴板 | `copyExport()`：等宽 JSONL 全文 → `navigator.clipboard.writeText`；失败降级为只读 textarea |
| 下载文件 | `doExport()`：`workbuddy-accounts-{encrypted|backup}-{YYYYMMDD-HHmm}.jsonl`，mime `application/x-ndjson` |

### 6.6 其余相关页面

| 页面/按钮 | API |
|---|---|
| 客户端参数：探测本机客户端 | `POST /api/client-profile/detect` |
| 客户端参数：取值策略（auto/saved 切换） | `PUT /api/client-profile` `{profile:{}, source:"auto"\|"saved"}` |
| 客户端参数：保存 / 重置 | `PUT /api/client-profile` / `POST /api/client-profile/reset` |
| 客户端参数：导出参数 / 导入并启用 | `GET /api/client-profile/export` / `POST /api/client-profile/import` |
| 线上同步：保存配置 / 同步到线上 / 重新生成密钥 | `POST /api/sync/config` / `POST /api/sync/push` / `GET /api/sync/config`（缺密钥时自动生成） |
| 定时任务页：新建任务 | `POST /api/schedules`；行内启停/删除 `PATCH|DELETE /api/schedules/{id}` |
| 定时任务：任务类型中文名 | `daily_checkin`→签到领取积分、`refresh_balances`→刷新余额、`refresh_credits`→积分到期快照、`sync_models`→同步模型、`refresh_growth_tasks`→更新成长任务列表、`run_growth_tasks`→自动做成长任务、`run_growth_plan`→成长计划巡检、`claim_benefits`→每日领取权益、`keepalive_tokens`→token 保活 |
| 签到结果渲染 | 读 `last_result` 的 `detail[]`（账户/状态/积分/原因）与 `summary`；`stop_after` 只在 `daily_checkin` 时显示为「停止领取时间」 |
| 积分页 | `GET /api/credits/expiry`、`GET /api/credits/usage`、`POST /api/credits/selection-order` |
| 积分同步按钮 | `POST /api/accounts/credits/sync` |

---

## 7. 状态机汇总

### 7.1 账号（`accounts.status` + 各路冷却字段）

```
status: active | disabled        （disabled = 人工禁用）
运行期附加状态（都在 Account 行上）：
  cool_until   + cool_kind       # hard_credit | soft_rate | error_threshold | not_found
  breaker_until + breaker_fails  # 熔断
  degrade_until                  # 降级
  session_dead_fails             # 登录态失效累计（阈值 3 → 禁用/待重登）
  consecutive_fails, err_count   # 连续失败
重新登录成功（任何路径）= 人工恢复：status=active、清零 err_count/breaker_fails/consecutive_fails/
  session_dead_fails、清空 degrade_until/breaker_until/cool_until/cool_kind（仅当 cool_kind ∈
  {session_dead, transport, upstream_5xx}）/last_err_msg/last_err_at/last_upstream_error
  ⚠️ 但**不清**模型级日限（AccountModelCooldown）与上游真实额度
```

### 7.2 登录会话：见 1.6。

### 7.3 签到任务（`Schedule.stop_after` 驱动的自动停机）

```
正常循环 → 某账号 claim 返回 1003(event_ended) → ended=True
→ 本轮结束把 schedule.stop_after = utcnow()
→ 之后每轮进入 run_daily_checkin 立刻返回 {"skipped":"已超过停止领取时间，不再请求"}
（目的：活动下线后继续打接口会触发风控）
```

### 7.4 成长任务单任务状态（上游 `accept_status`）

```
not_accepted → (accept) → accepted/already_accepted → (进度达标) → completed → (claim) → claimed
分支：locked=true → 前端显示 manual，不可自动
```

---

## 8. 移植到 Tkinter 的最小可行清单

| # | 要做的事 | 依赖 | 说明 |
|---|---|---|---|
| 1 | `Credential` 数据结构 = 那份 `{account, auth}` JSON | 无 | 字段见 1.3，序列化 `ensure_ascii=False, indent=2` |
| 2 | `validate_credential` + JWT payload 解析（不验签） | 无（`base64`+`json`） | 见 1.4 |
| 3 | 手机号登录 11 步（`httpx.Client`，独立 cookie jar） | `httpx` | 见 1.2；`follow_redirects=False` 自己跟 |
| 4 | 设备环境生成器（UA/Client-Hints/伪 IP） | 无 | 见 5.1，纯随机即可 |
| 5 | 接码平台客户端（可选） | `httpx` | 见 1.10，`code==1` 判成功 |
| 6 | 一键注册编排（用 Tkinter 回调替代 SSE） | 1-5 | 事件语义见 1.11 |
| 7 | 凭证导入/导出（JSON / JSONL / 剪贴板） | 无 | 见 3.1-3.2；加解密可选（3.1 的 client 格式） |
| 8 | 签到 `get_checkin_status` + `claim_daily_checkin` | 3 | 见 4.1；必须写回 `auth_json` |
| 9 | 余额/积分明细 | 3 | 见 4.0、4.2 |
| 10 | 福利补偿（一次性活动） | 3 | 见 4.3，注意活动已过期 |
| 11 | 成长任务（分级 + 参与 + 触发 + 领奖） | 3 | 见 4.4，节流常量别改小 |
| 12 | 调度器（APScheduler 或 threading.Timer） | 8-11 | 任务表与时点见 4.5 |
| 13 | 设备指纹档案（版本号/风控头） | 无 | 见 5.2-5.3，可固定值 |
| 14 | 线上同步（可选） | 无 | 见 3.5，HMAC 签名 |
| 15 | `X-Device-Token` | node + 桌面端 | **建议跳过**，见 5.6 |

---

## 9. 坑位清单（都是源码里踩过并写进注释的真实故障）

1. **`/v1/auth/accounts` 只能调一次**：它会消耗 `state_token`，先 accounts 再 select_account 必得 `E0010072 无效的token`。
2. **跨域 cookie 必须同一个 jar**：OneID 横跨 `workbuddy.cn` 与 `oauth2.account.tencent.com`，拆 client 会静默丢会话（表现为最后一步一直 `11217 login ing...`）。
3. **第 9 步的 `state` 是 OneID 的 S1**，不是 Keycloak 的 `session_code`。
4. **漏掉第 10 步 `/console/auth/login?state=` 绑定**，`/v2/plugin/auth/token` 永远 `11217`。
5. **`11217` 无法区分「未完成」与「state 无效」**，只能靠前端超时收尾。
6. **`need_captcha` 是按手机号的风控**，不是按 IP —— 换号立刻恢复；不要以为是 IP 被封。
7. **凭证去重必须先加锁**：8 并发注册同一账号曾建出 6 条重复记录（SELECT 再 INSERT 无保护）。按 uid 串行 + `_find_dup` 兜底。
8. **`credits_expiring` 长期为 0**：因为 `with_expiry` 被「省开销」关掉，导致选号规则静默失效。故 `refresh_credits` 独立成任务。
9. **`DeductionEndTime` 有 2034/2049 占位值**：必须与 `CycleEndTime` 比对（差 > 365 天取后者），否则「永不过期」。
10. **昵称缺失时上游给的是字面量 `"null"`**：不清理会导致满屏叫「null」的账号。
11. **`travel/claim` 必须发空 JSON**：带 `record_id` 会被部分网关拒绝。
12. **签到/成长必须写回 `auth_json`**：这些请求会触发 token 刷新，不写回下次就用过期 token。
13. **keepalive 的「每天 22 点」曾因错过整点就永远跳过**（连续 18 天没跑而界面显示正常）：改成「到期窗口」语义。
14. **`verify_sms` 必须持会话锁 + 结果缓存**：用户双击会变成「成功一次 + 报一次无效 token」。
15. **`sync_secret` 不要用主机特征推导**（旧实现 `sha256(hostname-mac...)[:32]` 只有 128 bit 且可预测）：改 `secrets.token_hex(32)`。

---

## 10. 常量 / 字段速查

### 10.1 关键常量

| 名称 | 值 | 出处 |
|---|---|---|
| 会话 TTL / HTTP 超时 / 闸门 | 900s / 10s(连接 5s) / 8 | `wb_login.py` |
| 发码间隔 / 每小时上限 | 60s / 10 次 | `routers/login.py` |
| 接码默认项目 | a:139372、b:77560（备用 a:142759、b:71139） | `sms_client.py` / `sms_register.py` |
| 虚拟号段 | 170/171/162/165/167 | `sms_register.py` |
| 签到/成长业务码 | 1001 已领 / 1002 无资格 / 1003 活动结束 | `backend.py` |
| 凭证失效码 / 配额码 | 12153,12151 / 6004,6008,14003,14051 | `wb_login.py` |
| 成长节流 | 事件 1.2s、账号 1.0s、轮询 0.5s、accept 6s、领奖 8s、单任务 ≤10 次 | `growth.py` |
| 成长预算 | 单号 300s、同步批 50s、定时批 1500s | `growth.py` |
| 积分到期阈值 | 7 天 / 365 天 / 730 天 | `credits.py` |
| 补偿活动 | 500 积分、截止 2026-10-22 23:59:59 +08:00 | `compensation.py` |
| 调度循环 | 15s 轮询；`_RESULT_LIMIT=20000` | `scheduler.py` |
| 成长刷新→执行间隔 | 180s | `scheduler.py:_GROWTH_MIN_AFTER_REFRESH` |
| 客户端参数 | desktop 5.5.6 / cli 2.137.1 / product SaaS | `client_profile.py` |
| 参数档案上限 | 256 KB；缓存 30s | `client_profile.py` / `routers/client_profile.py` |

### 10.2 SystemSetting 键（用配置文件/本地 JSON 等价替换）

```
sync_remote_url, sync_secret
client_profile, client_profile_source        # JSON 文本 + "auto"|"saved"
post_login_automation:<account_id>           # {status, updated_at, result}
account_resources:<account_id>               # {status, errors[], updated_at}
keepalive:pending                            # 保活待办
```

### 10.3 建议在 Tkinter 工具里建的表

```
accounts(id, name, uid UNIQUE, enterprise_id, domain, auth_json, status,
         balance_total, balance_remain, last_sync_at, last_used_at,
         err_count, cool_until, cool_kind, last_err_at, last_err_msg,
         breaker_until, breaker_fails, session_dead_fails, consecutive_fails, degrade_until,
         credits_expiring, credits_expiring_soon, credits_soonest_expire_at,
         credits_evergreen, credits_expired, credits_snapshot, credits_synced_at,
         created_at, updated_at)
schedules(id, name, task, interval_minutes, timing_config, enabled,
          last_run_at, next_run_at, last_result, stop_after)
scheduled_states(key, value, updated_at)      # 等价 SystemSetting
job_runs(id, kind, status, total, done, result_json, started_at, finished_at)
```

---

*报告生成方式：只读源码 + 行号定位；未运行任何上游请求。*
