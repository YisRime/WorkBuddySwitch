# WorkBuddy 号池面板（workbuddy2api-panel）实现级参考

> 只读代码考古，源仓库 `D:\Work\_wb_refs\workbuddy2api-panel`（Go，module `github.com/linguo2625469/workbuddy2api-panel`）。
> 用途：桌面端 WorkBuddySwitch 的「账号状态列 / 签到任务 / 积分历史」设计参考。
> 全文所有字段名、常量、路径、请求头均取自源码字面量，可直接照抄实现。
> 说明：本仓库**没有加密/解密逻辑**——所谓「加解密步骤」实际只有三类：JWT payload 的 base64url 只读解码（取 `token_source`）、`sha256` 稳定派生设备标识、以及 `tmp + rename` 原子写盘（`0600`）。第 3、7 节逐条列出。

---

## 0 结论速览（先看这 12 条）

1. 上游只有 **3 个 CN 域名 + 1 个 global 域名**：chat `https://copilot.tencent.com`、billing `https://www.codebuddy.cn`、web `https://www.workbuddy.cn`、global 全部 `https://www.workbuddy.ai`。
2. 登录是**设备授权流、无 PKCE**：`POST /v2/plugin/auth/state?platform=CLI` → 浏览器登录 → `GET /v2/plugin/auth/token?state=` → `GET /v2/plugin/login/account?state=`。
3. 签到是 `POST {billing}/v2/billing/meter/daily-checkin`，body 就是 `{}`；「今天已签到」靠错误文案 `已签到` / `already` 判定（不是状态码）。
4. global 账号**没有签到/成长体系**，它的唯一天然增益是 `POST {billing}/billing/ide/trial`（幂等码 `14051`）；CN 没有这个端点。
5. 企业版（`enterpriseId` 非空）**没有个人成长体系**：签到/任务/连登/旅行/夜猫子上游一律拒绝（`10001` / `403`）；额度改走 `/v2/billing/meter/get-enterprise-user-usage`。
6. 还有一类更隐蔽的「企业切换会话」（JWT 里 `token_source=enterprise_switch`）：**读端点正常、`/v2/report` 返回 200 但被静默丢弃**——写行为全部零收益，必须门控。
7. 任务体系有**两套口径**：默认（无头）与小程序口径（`X-Client-Platform: miniprogram`）。mp 列表是默认的超集，但 accept/claim 必须带对应头，否则 `task not found`。
8. 任务领奖有**两个不同的域**：普通任务领奖在 `webBase`（`https://www.workbuddy.cn/activity/growth/tasks/{code}/claim`，任务码在**路径**里），mp 任务领奖在 `chatBase` + mp 头（400 时回落 web 域）。
9. 连登（streak）的兑换档位只有 `7d/14d/28d`，兑换幂等令牌 `client_token` 是前端 `randomUUID` 形态（16 随机字节排成 `8-4-4-4-12` hex）。
10. 池子状态是**一个 entry 上多个并列截止时间**：`until`（即时冷却，软/硬）、`breakerUntil`（熔断指数退避）、`degradeUntil`（连败降权）——三者是**或门**，「取更长者不叠加」由或门天然保证。
11. `CHECKIN` 状态**不落 `checkin_done` 布尔值**，而是落 `last_checkin_day` 日期字符串，读时与「今天」比较——跨零点自动过期，重启不丢。
12. 面板 45 条路由里，**账号管理类 31 条**、**API 代理/网关观测类 10 条**、**静态资源 4 条**（`GET /panel/api/overview` 同属 A/B；第 8 节逐行列）。

---

## 1 域名、realm 与头族

### 1.1 域名表（`internal/upstream/client.go:803-816, 827-843, 955-972`）

| 用途 | CN | global |
|---|---|---|
| chatBase | `https://copilot.tencent.com` | `https://www.workbuddy.ai` |
| billingBase | `https://www.codebuddy.cn` | `https://www.workbuddy.ai` |
| webBase | `https://www.workbuddy.cn` | `https://www.workbuddy.ai` |
| Origin / Referer | `https://www.codebuddy.cn` | `https://www.workbuddy.ai` |

- `Client` 字段：`ChatBaseCN / BillingBaseCN / WebBaseCN / ChatBaseGlobal / BillingBaseGlobal`，空值分别回落上述默认；`defaultGlobalBase = "https://www.workbuddy.ai"`（`client.go:828`）。
- `globalOn(a) = c.GlobalEnabled && a.Realm()=="global"`（`client.go:848`）——**双保险**：config 关掉闸门后即便 auth 写了 `realm=global` 也走 CN base 与 CN 路径。
- 登录端点另有一份硬编码常量（`cmd/login/main.go:38-44`、`internal/panel/login.go:26-32`）：CN `copilot.tencent.com`、global `www.workbuddy.ai`、UA `CLI/2.63.2 CodeBuddy/2.63.2`。
- `cmd/credit` 自己一份：`billingBaseCN = https://www.codebuddy.cn`、`billingBaseGlobal = https://www.workbuddy.ai`，判定口径与 `auth.Realm()` 一致但**不看 global.enabled 开关**（`credit/main.go:40-49`）。

### 1.2 realm 判定（`internal/auth/auth.go`）

```go
globalEnabled atomic.Bool   // init(): Store(true)
SetGlobalEnabled(bool) / GlobalEnabled() bool
```

| 函数 | 规则 | 是否受 `globalEnabled` 影响 |
|---|---|---|
| `Realm()` | `!globalEnabled` → 恒 `"cn"`；否则 `realm=="global" || isGlobalDomain(Domain)` → `global`；否则 `cn` | 是（逃生门） |
| `ResolveRealm(explicit, domain)` | `explicit` 非空优先；否则 `isGlobalDomain(domain)`；否则 `cn` | 否 |
| `BackfillRealm()` | `realm` 为空时用 `ResolveRealm("", Domain)` 写回，返回 `(changed, realm)`；已有值幂等不动 | 否（刻意绕过逃生门，防污染落盘） |
| `BackfillRealmFor(a, realm)` | 显式写 `cn/global`，非法值报错 `realm must be cn/global, got %q` | 否 |
| `isGlobalDomain(d)` | `d=="workbuddy.ai" || HasSuffix(d,".workbuddy.ai")`（小写、trim） | — |

### 1.3 四类请求头（`internal/upstream/headers.go`）

常量：`defaultClientVersion = "5.5.4"`、`defaultCliVersion = "2.137.1"`、`originRefererCN = "https://www.codebuddy.cn"`、`originRefererGlobal = "https://www.workbuddy.ai"`。

**① CommonHeaders(req, a)**（`headers.go:147`）
```text
Content-Type: application/json
Accept: application/json
X-Requested-With: XMLHttpRequest
Origin: <origin>            # CN codebuddy.cn / global workbuddy.ai
Referer: <origin>/
User-Agent: WorkBuddy/<clientVersion> <platform>/<clientVersion> CLI/<cliVersion>
            # platform: CN "WorkBuddy" / global "WorkBuddy AI"
X-CodeBuddy-Request: 1
Accept-Language: zh-CN | en-US     # 按 realm
X-Machine-ID: sha256("wb2a:machine:"+uid)[:18] hex  → 36 hex
X-Session-ID: sha256("wb2a:session:"+uid)[:18] hex   # 空 uid 不注入
```

**② ChatHeaders(req, a, clientIP, meta)** = CommonHeaders +（`headers.go:198`）
```text
Accept: application/json, text/event-stream
Authorization: Bearer <accessToken>   # 空则 X-No-Authorization: 1
X-User-Id: <uid>                      # 空则 X-No-User-Id: 1
# CN: X-Enterprise-Id 或 X-No-Enterprise-Id:1；X-Domain 或 X-No-Department-Info:1
# global: X-No-Enterprise-Id: 1 + X-Domain: www.workbuddy.ai
X-Agent-Purpose: conversation
X-IDE-Name / X-IDE-Type / X-IDE-Version / X-Product   # 默认全 "WorkBuddy"
X-Forwarded-For / X-Real-IP / X-Client-IP             # 仅 PassthroughIP
X-Device-Token                                        # 有则发
X-Conversation-ID / X-Conversation-Request-ID / X-Conversation-Message-ID
X-Request-ID / X-Root-Request-ID / X-Trace-ID
X-B3-TraceId / X-B3-SpanId / X-B3-Sampled: 1
```
安全红线：**chat 请求绝不带 `X-Refresh-Token`**。

**③ BillingHeaders(req, a)**（`headers.go:381`）——所有 billing/growth/report/checkin/travel 请求共用
```text
Authorization: Bearer <accessToken>
Accept: application/json
Content-Type: application/json
X-CodeBuddy-Request: 1
Accept-Language: zh-CN | en-US
User-Agent: WorkBuddy/<clientVersion>     # client_name=="SaaS" 时不发 UA
X-User-Id: <uid>
X-Enterprise-Id: <ent> + X-Tenant-Id: <ent>   # 企业号才发
X-Domain: <domain>
X-Device-Token: <token>                        # 有则发
```

**④ RefreshHeaders(req, a)** = CommonHeaders +（`headers.go:409`）
```text
X-Refresh-Token: <refreshToken>
X-Enterprise-Id: <ent>              # 有则发
X-Auth-Refresh-Source: plugin
```

### 1.4 设备标识派生（`headers.go:131-144`）

```go
func deriveAccountStableID(uid, purpose string) string {
    sum := sha256.Sum256([]byte("wb2a:" + purpose + ":" + uid))
    return hex.EncodeToString(sum[:18])   // 36 hex chars
}
// purpose = "machine" → X-Machine-ID；"session" → X-Session-ID
```
跨重启恒定、账号间互异；与 `X-Device-Token`（上游签发的真 token）并存，属不同头族。

---

## 2 CLI 四命令

### 2.1 `cmd/login/main.go` — OAuth 设备授权

子命令：`login [--realm=cn|global] url | poll | realm`。

**常量**
```go
upstreamBaseCN      = "https://copilot.tencent.com"
upstreamBaseGlobal  = "https://www.workbuddy.ai"
clientUA            = "CLI/2.63.2 CodeBuddy/2.63.2"
originRefererCN     = "https://www.codebuddy.cn"
originRefererGlobal = "https://www.workbuddy.ai"
stateFile = filepath.Join(os.TempDir(), "wb2api-login-state.json")  // 0600
http.Client{Timeout: 30 * time.Second, Jar: cookiejar.New(nil)}     // 每流程独立 jar
```

**步骤 A：`url`（`main.go:208-226`）**
1. `POST {base}/v2/plugin/auth/state?platform=CLI`，body 字面量 `{}`。
2. 头：`commonHeaders(origin)` → `Content-Type: application/json`、`Accept: application/json, text/plain, */*`、`X-Requested-With: XMLHttpRequest`、`Origin`、`Referer: origin+"/"`、`User-Agent: CLI/2.63.2 CodeBuddy/2.63.2`。
3. 解信封 `{code,msg,data}`，`code!=0` 报错 `code=%d msg=%s`；HTTP `>=400` 报 `http_error: upstream %d`；`>=300` 报 `redirect`。
4. `data` 形如 `{"state":"...","authUrl":"..."}`；两者任一为空 → fatal `auth state: missing state or authUrl`。
5. 落盘 `{"state":"...","realm":"cn|global"}`，权限 `0600`；stdout 只打印 `authUrl`。

**步骤 B：`poll`（`main.go:231-279`）**
1. 读 state 文件；`validateRealmMatch(state.Realm, cliRealm)`——state 无 realm（旧文件）放行，不一致直接报 `realm mismatch`。
2. `GET {base}/v2/plugin/auth/token?state=<state>`（同 headers，不带 Bearer）。
   - 业务 code 非 0（`"login ing"`）= 还没登录完 → 打印「登录未完成（waiting for login）」并退出 1。
   - 成功 `data = {accessToken, refreshToken, expiresIn, domain}`。
3. `GET {base}/v2/plugin/login/account?state=<state>` + `Authorization: Bearer <accessToken>` → `data = {uid, enterpriseId, nickname}`；失败**不阻断**（仅缺展示名）。
4. 输出（`buildLoginOutput`）：
```json
{"access_token":"...","refresh_token":"...","expires_in":5184000,"domain":"...",
 "realm":"cn|global","uid":"...","enterprise_id":"...","nickname":"..."}
```
   `realm` 永不为空：显式 `--realm` 优先，否则由 `domain` 推断（`auth2.ResolveRealm`）。
5. 删除 state 文件。

**`realm` 子命令**：交互式选域，stdout 只输出 `cn|global`；输入映射 `""|"1"|"cn" → cn`、`"2"|"global" → global`，非法回落 cn；EOF 回落 cn。

**`login.sh` 驱动**：`./login url` 打印 URL（有 `xclip/xsel` 就复制剪贴板）→ `read -rp "完成登录后按 y 继续"` → `./login poll` → Python 拆字段 → 立刻 `POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin`（body `{}`，头：Bearer + `X-User-Id` + 可选 `X-Enterprise-Id/X-Tenant-Id/X-Domain`）→ 落盘 `auths/workbuddy-<uid>.json`。

### 2.2 `cmd/signin/main.go` — 批量签到

流程（逐文件 `auths/workbuddy-*.json`，排序遍历）：
1. `os.ReadFile` → `auth.Parse`；失败记 `LOAD_ERR`（计入 fail）。
2. `a.FilePath = f`。
3. `a.NeedsRefresh(2 * 3600)` 为真 → `up.RefreshToken(a)`：
   - `ErrSessionDead` → 状态 `AUTH_INVALID`；
   - 其它错误 → `FAIL`，detail `refresh: <60 字`；
   - 成功 → `a.SaveAtomic()`（写盘失败只打日志 `signin %s save: %v`）。
4. `up.DailyCheckin(a)`：成功 `OK`；失败且 `isAlready(msg)` 为真 → `ALREADY`；否则 `FAIL`。
   `isAlready` 判据：小写后含 `已签到` / `already` / `checkin` / `code=400`。
5. 顺手 `up.UserResource(a)` → `remain/total`（失败不填）。
6. 输出表格列：`uid | nick | status | remain | detail`，末行 `total=%d ok=%d already=%d fail=%d`。
`signin.sh` = `go build -o signin_bin ./cmd/signin` 后 `exec signin_bin [auths_dir]`。

### 2.3 `cmd/credit/main.go` — 积分查询

- 目录：`./auths` 或环境变量 `WB2A_AUTH_DIR`；文件名 `workbuddy-*.json`。
- 读盘结构：`{auth:{accessToken,domain,realm}, account:{uid,enterpriseId,nickname}}`。
- 请求：
```text
POST {billingBase}/v2/billing/meter/get-user-resource     # CN
POST {billingBase}/billing/meter/get-user-resource        # global（无 /v2 前缀）
Authorization: Bearer <accessToken>
Accept: application/json
Content-Type: application/json
X-User-Id: <uid>
X-Enterprise-Id: <ent>  + X-Tenant-Id: <ent>
X-Domain: <domain>
timeout 20s
```
body：
```json
{"PageNumber":1,"PageSize":100,"ProductCode":"p_tcaca","Status":[0,3],
 "PackageEndTimeRangeBegin":"2006-01-02 15:04:05 格式的现在",
 "PackageEndTimeRangeEnd":"现在 + 365*101 天"}
```
- 响应：`data.Response.Data.Accounts[]`（每项 `CapacityRemain/CapacityUsed/CapacitySize/CycleCapacityRemain/CycleCapacityUsed/CycleCapacitySize`）+ `data.Response.Data.TotalDosage`。
- 聚合 `packageRemainUsed`（与 `upstream/client.go:2441` 逐字一致）：
```text
if CycleCapacitySize > 0:
    remain = clamp(CycleCapacityRemain, 0, size); size = CycleCapacitySize
    used = size - remain; if CycleCapacityUsed > used { used = CycleCapacityUsed; if size>=used { remain = size-used } }
else:
    remain = CapacityRemain; used = CapacityUsed; size = CapacitySize
    if used == 0 && size > remain { used = size - remain }
```
  汇总后再补两刀：`size-remain > used → used = size-remain`；`TotalDosage > size → size = TotalDosage`（同法重算 used）。
- 账号间 `time.Sleep(200ms)`。
- 输出 JSON：`{"service":"workbuddy","ts":N,"total":{remain,used,size,accounts,ok,failed},"accounts":[{uid,nickname,remain,used,size,packages,ok,error?}]}`；`-pretty` 输出四行日报（`📊 WorkBuddy 积分日报` / `账号: x/y` / `总计: r/s` / `剩余: n%` + 每个失败号 `⚠️`）。
- `credit.sh`：默认 `./credit -pretty`，`-json` 直出原始 JSON。

### 2.4 `cmd/trial/main.go` — global trial 加油包

- 目录参数或 `auths`；逐文件 `auth.Parse`。
- **必须** `up.GlobalEnabled = true`，否则请求被路由到 CN base 必然失败。
- `if !a.IsGlobal()` → 状态 `N/A`，detail `"CN account not applicable"`，**不发任何请求**。
- 否则 `up.ClaimTrial(a)`：
```text
POST {billingBase}/billing/ide/trial     # body 为 nil（不带 body）
```
  成功 → `OK` / `"trial granted"`；错误含 `code=14051` 或 `"code":14051` → `ALREADY` / `"already claimed (idempotent)"`；其它 → `FAIL`。
- 输出 `uid | nick | status | detail`，末行 `total/ok/already/na/fail`。

---

## 3 `internal/auth/auth.go` — 凭证、域、企业态

### 3.1 磁盘两形态（`Parse`）

**嵌套形**（插件 OAuth 输出，也是 `SaveAtomic` 的唯一输出形态）
```json
{"auth":{"accessToken":"...","refreshToken":"...","expiresAt":1700000000,"domain":"...","realm":"cn|global"},
 "account":{"uid":"...","enterpriseId":"...","nickname":"..."},
 "device_token":"..."}
```
**扁平形**（手写/旧版）：`accessToken / refreshToken / expiresAt / domain / realm / uid / enterpriseId / nickname / device_token` 全部顶层。
判定：`probe["auth"]` 键存在 → 嵌套形。`accessToken` 为空 → `parse_error: missing accessToken`；JSON 坏 → `storage_parse_error: %w`。

### 3.2 并发约定（实现必须照做）

- `Auth.mu sync.Mutex` 保护 token 字段。
- **出站读一律走** `AccessTokenValue() / DomainValue() / RefreshTokenValue() / Realm() / NeedsRefresh()`（都加锁）。
- 写路径：`RefreshToken`（upstream）两段式（锁内快照 → 锁外 30s I/O → 锁内校验快照一致后写回）；`SaveAtomic` 全程持锁。
- 原因（源码注释实例）：keepalive 定时刷新与在途请求共享同一个 `*auth.Auth` 指针，锁外直读会被 `go test -race` 抓。

### 3.3 `SaveAtomic` 步骤（原子写）

1. 加锁；`AccessToken` 空 → 拒绝：`save refused: empty accessToken (uid=%s)`；`FilePath` 空 → `no FilePath set`。
2. 组文档：`{"auth":{accessToken,refreshToken,expiresAt,domain,realm},"account":{uid,enterpriseId,nickname}}`；`DeviceToken` 非空才追加顶层 `device_token`（旧文件不引入空键）。
3. `json.MarshalIndent(doc, "", "  ")` → 写 `FilePath + ".tmp"`（`0600`）→ `os.Rename` 覆盖。
4. 权限错误给三段可操作提示（`fs.ErrPermission` → Docker PUID/PGID、`chown -R 10001:10001 ./auths ./data ./config.json`、`user: "0:0"`）。

### 3.4 企业态两个 diferente 判据（关键！）

`IsEnterprise() = strings.TrimSpace(EnterpriseID) != ""` —— 真企业版。

`IsEnterpriseSession()` = `IsEnterprise()` **或** JWT `token_source == "enterprise_switch"`。

**JWT 解码步骤（`jwtTokenSource`）**
1. `strings.Split(token, ".")`；`len < 2` → `""`。
2. `parts[1]` 先按 `base64.RawURLEncoding.DecodeString`（无填充）；失败再试 `base64.URLEncoding`（带填充）。
3. `json.Unmarshal` 到 `struct{ TokenSource string `json:"token_source"` }`；任何失败返回 `""`（老 token 零回归）。
4. 不缓存，每次现解（`RefreshToken` 会整体换发 token，企业切换状态可能翻转）。

**门控矩阵（照抄这张表即可）**

| 任务 | 跳过条件 | 依据 |
|---|---|---|
| 签到 `DailyCheckin` | `IsGlobal` 或 `IsEnterprise` | global 无签到；企业 400/10001 |
| 余额 `UserResourceSnapshot` | 从不跳过 | 企业改走 enterprise 端点 |
| 保活 `RefreshToken` | 无 `refreshToken` | — |
| 活跃上报 `/v2/report` | `IsEnterpriseSession` | 200 但静默丢弃 |
| 连登/旅行/夜猫子/成长任务 | `IsGlobal` 或 `IsEnterpriseSession` | growth 403 / 不计分 |
| 补偿 `claim-compensation` | `IsGlobal` 或 `IsEnterprise` | 企业 10001 |

### 3.5 `LoadDir` 启动扫描

- glob `workbuddy*.json`；解析失败**静默跳过**。
- 重复 UID：打 `WARN: uid %s duplicated across %s and %s — 后者覆盖`，行为仍「后载入者胜出」。
- `realm` 为空 → `BackfillRealm()` + `SaveAtomic()`（写失败只 WARN，不阻断启动）；补到 `global` 时打一条迁移日志。

### 3.6 `NeedsRefresh(within)`

`ExpiresAt <= 0 → true`（无过期时间视为需要刷新）；`now.Add(within).Unix() >= ExpiresAt → true`。

---

## 4 `internal/scheduler` — 定时调度与任务状态机

### 4.1 `Config` 字段与默认时点（`scheduler.go:24-69, 103-126`）

```go
type Config struct {
    Pool *pool.Pool; Upstream *upstream.Client
    CheckinHours   []int  // 默认 [9, 21]
    TravelHours    []int  // 默认 [9, 21]
    ActivityHours  []int  // 默认 [10]
    KeepaliveHours []int  // 默认 [22]
    BlackcatHours  []int  // 默认 [23]
    GrowthHours    []int  // 默认 [1]
    ExpiringSoonWindow time.Duration   // <=0 不做快过期路由门槛
    CheckinDisabled/TravelDisabled/ActivityDisabled/KeepaliveDisabled/BlackcatDisabled/GrowthDisabled bool
    IncludeDisabledInTasks bool        // 保号类四任务是否覆盖 disabled 账号（**仍不参与选号**）
    GrowthHook func(); TaskSweepHook func()
}
```
配置文件对应键（`config.example.json`）：`schedule.checkin_hours [9,21]`、`growth_hours [1]`、`travel_hours [9,21]`、`activity_hours [10]`、`keepalive_hours [22]`、`blackcat_hours [23]`、`balance_refresh_enabled true`、`balance_refresh_minutes 5`、`include_disabled_in_tasks false`；另有 `expiring_soon "168h"`、`credit_floor 100`。

### 4.2 运行循环（`Run`）

- `nextWake(now)`：对每个未禁用任务算 `nextFire(now, hours)`（每小时的 `HH:00:00`，已过则 `+24h`），取最早时刻，并收集**同一时刻的全部** taskKind。
- `taskKind` 枚举顺序：`taskCheckin, taskTravel, taskActivity, taskKeepalive, taskBlackcat, taskGrowth`。
- 等待：`waitSlot` **分段**睡，每段 ≤ `wallclockCheckStep = time.Minute`，每段醒来用**墙钟**重判（防 macOS/Windows Modern Standby 冻结单调时钟导致漏跑）；三态 `slotFired / slotRearm / slotCancel`。
- 迟到补跑宽限：`wakeupLateThreshold = 1s`（晚于计划 ≤1s 视为准点）；超过则 `awaitWakeupGrace` 先睡 `wakeupGraceDelay = 5s`（等网络栈/DNS 就绪，Windows standby exit 实测 1–2s）。
- `runBatch`：同一时刻的各类任务**并行** goroutine，`wg.Wait()` 后进入下一轮。
- 热改：`Reconfigure(checkinHours, travelHours, activityHours, keepaliveHours, blackcatHours, growthHours, 6×disabled bool)` + `poke(rearmSchedule)` / `poke(rearmBalance)`；空 hours 视为未配置保留旧值。
- 独立 ticker：`StartBalanceRefresh(ctx, interval)`（启动即刷一轮）、`StartTaskSweep(ctx, interval)`；间隔 `atomic.Int64` 热改，`<=0` 表示暂停（等 rearm 通知）。

### 4.3 `RunCheckinNow` 单轮流程（`scheduler.go:466-522`）

对 `Pool.List()`（按 UID 排序）逐号：
1. `st.Disabled && !includeDisabled` → skip。
2. `a := Pool.AuthByUID(uid)`；`a == nil || a.RefreshTokenValue() == ""` → skip。
3. `a.IsGlobal()` → **continue**（不发起任何上游调用）。
4. `!a.IsEnterprise()` 时：`DailyCheckin(a)`
   - 成功 → `Pool.NoteCheckinDone(uid)` + `log "checkin %s: 签到成功"`；
   - 错误且 `upstream.IsAlreadyCheckin(err)` → 也算已签：`NoteCheckinDone` + `log "今天已签到（幂等）"`；
   - 其它错误 → 只记日志，**继续往下查余额**。
   （`IsAlreadyCheckin` 只认 `*upstream.Error`，网络/解析错误不得当幂等成功——否则停机补签会把「其实没签」记成正常。）
5. 企业号：跳过签到但**不 continue**，余额照查（企业额度是该号唯一额度来源）。
6. `UserResourceSnapshot(a, expiringSoon)` → `(remain, total, expiring, earliestAt, earliestRemaining, buckets, err)`；失败记日志 continue。
7. `Pool.ReenableIfCredits(uid, remain, total)`（余额 >0 且非 disabled → 只解冻 **CoolHard** 冷却，不动熔断/软冷却/模型台账）。
8. `Pool.SetCreditsSnapshot(uid, CreditsSnapshot{Remain,Total,Expiring,EarliestAt,EarliestRemaining,ExpiryBuckets})`。
9. 全池结束后 `RunStreakBonusNow()`。

### 4.4 连登管家（`scheduler/streak.go`）

`RunStreakBonusNow` 门控：`st.Disabled` / 无 `AccessToken` / `IsGlobal` / `IsEnterpriseSession` → skip。

`streakBonusAccount(a)` 顺序：
1. **补签**：`HeatmapYesterdayMissed(a)` → 若昨日 `score==0`，再 `GrowthStreakFull` 查 `makeup_cards.balance > 0`，则 `UseMakeupCard(a, 昨天 "2006-01-02")`。任意一步失败静默。
2. **新手礼包**：`ClaimGift(a)`，成功打 `🎊 新手礼包 +%dc`。
3. **补偿**：`claimCompensationQuiet(a)`（见 4.8）。
4. `GrowthStreakFull(a)` → `redemption_status`：
   ```go
   statuses := map[string]string{"7d": Tier7dStatus, "14d": Tier14dStatus, "28d": Tier28dStatus}
   for _, tier := range full.RedemptionStatus.Tiers {
       if statuses[tier.Tier] == "locked" || == "claimed" { continue }
       GrowthRedeemTier(a, tier.Tier)   // 403「连续登录天数不足」属预期，静默
   }
   ```
5. **抽奖**：`LotteryChances(a)` 得 `chances`，循环 `chances` 次 `LotteryDraw(a)`，每次打一行 `🎲 第%d抽 <载荷前 220 字>`；任一次失败即停。

### 4.5 猫猫旅行状态机（`scheduler/travel.go`）

常量：`travelLocationID = 4`（古镇客栈；4 个地点收益/时长区间相同，无最优解）、`travelStateIdle="idle" / travelStateTraveling="traveling" / travelStateArrived="arrived"`、`travelAccountDelay = 800ms`、`adoptReportGap = 1050ms`、`cstZone = FixedZone("CST", 8*3600)`。

`RunTravelNow` 门控：`Disabled` / 无 `RefreshToken` / `IsGlobal` / `IsEnterpriseSession` → skip；账号间 `Sleep(travelAccountDelay)`（首个不睡）。**paused 不跳过**（旅行是纯 RPC，不占模型配额）。

`travelOne(a)`：
```text
BuddyInfo(a) ──err──▶ 记日志返回
   │ nil（无猫）
   ├──▶ travelAdopt(a)
   │ 非 nil
   ▼
TravelStatus(a) ──err──▶ 返回
   ├─ state=="arrived"  → travelClaim(record_id)
   ├─ state=="idle"     → travelDepart(4)      # daily_limit_reached 则跳过
   └─ state=="traveling"→ 记 "skip (traveling record=N)"
```

`travelAdopt(a)`（无猫）：当日已试（`adoptTried[uid] == travelDay(now)`，进程内 map，重启清零）→ 直接返回；否则
1. `ReportChatActivity(a, "wb2api-adopt-<ms>", "")`（解锁 `first_buddy` 前置）→ 成功后 `Sleep(adoptReportGap)`；
2. `BuddyAgreement(a)`（`{"agree":true}`）失败即返回；
3. `BuddyFirst(a)`：
   - 成功 → `adopt ok (+300 credits)`；
   - `IsBuddyTaskIncomplete(err)`（HTTP 400 + `first_buddy task not completed yet`）→ `markAdoptTried` + 「retry tomorrow」；
   - 其它 → 记错误。

### 4.6 夜猫子（`scheduler/blackcat.go` + `upstream/blackcat.go`）

- 窗口：`InNightWindow(now) = h >= 23 || h < 8`（本地时区）。
- 时钟可替换点 `var nowForNightWindow = time.Now`（让「企业门控是否生效」的测试不随墙钟失效）。
- 门控：`Disabled || Paused`（**唯一跳过 paused 的任务**，因为它要发真实模型对话）、无 token、`IsGlobal`、`IsEnterpriseSession`。
- `BlackcatNeed(a)`：从 `ListTasks` 找 `black_cat`，`claimed || current>=target` → 0，否则 `target-current`。
- `RunNightChats(a, need)`：逐次 `ChatStream` 发 `{"model":"glm-5.2","messages":[{"role":"user","content":"1+1等于几？直接回答。"}],"stream":true}`，读干 `io.Copy(io.Discard, io.LimitReader(rc, 1<<20))`，然后 `ReportChatActivityModel(a, "wb2api-night-<ms>-<i>", "", "glm-5.2", "GLM-5.2")`，每次之间 `Sleep(4s)`。

### 4.7 `black_cat` 判据

23:00–08:00 内完成 3 次 `glm-5.2` 真实对话 + `chat_request_send` 事件链；窗口外行为不计分。

### 4.8 补偿可观测化（`scheduler/compensation.go` + `upstream/compensation.go`）

跳过原因枚举（面板直接渲染，**勿改字面量**）：
```go
compSkipDisabled="disabled"; compSkipNoAuth="no_auth"; compSkipGlobal="global"
compSkipEnterprise="enterprise"; compSkipInactive="no_active"
compSkipClaimed="claimed"; compSkipZeroCredit="zero_credit"
```
（**没有 paused**：暂停选号不跳过补偿——补偿是纯领奖 RPC。）

数据结构：
```go
type CompensationRow    { UID, Nickname, Status upstream.CompensationStatus, Skip, SkipMsg, Error }
type CompensationResult { UID, Nickname, Status, Claimed bool, Credit int64, Skip, SkipMsg, Error }
type CompensationSummary{ Total, Claimed, Skipped, Errored int; Credit int64; OK()=Errored==0 }
```

- `compTargets()` 按 **池顺序**生成有序切片（含被跳过项，保留昵称与稳定行序）。
- `CompensationStatusAll()`：并发上限 `sem := make(chan struct{}, 3)`，按下标写入预分配切片（无需锁、行序不漂移）。
- `ClaimCompensationNow()`：**串行**逐号 `claimOne`（每号每活动只能领一次）。
- `claimOne(a)`：`GetCompensationStatus` → 失败记 `r.Error` 并日志「查状态失败」→ `!st.Pending()` 时按 `!Active / Claimed / 默认` 三分支给 Skip 文案 → 待领则 `ClaimCompensation` → 成功 `r.Claimed=true, r.Credit`，日志 `🎊 补偿领取 +%dc（截止 %s）`。
- `SummarizeCompensation`：`Error!="" → Errored`；`Claimed → Claimed + Credit 累加`；否则 `Skipped`。
- 门控口径：`Disabled / 无 AccessToken / IsGlobal / IsEnterprise` 跳过；单号入口 `ClaimCompensationFor(uid)` 用 `Pool.Status` + `AuthByUID` 做 O(1) 判定。

### 4.9 任务补领清扫（`panel/tasksweep.go`，由 scheduler ticker 驱动）

- 只做两件幂等事：`accept` 未接受的、`claim` 已达标未领的；**不跑任何行为链路**。
- 判据只认 `Task.Claimable = !claimed && target>0 && current>=target`，**不用** `accept_status=="completed"`（回读会短暂 completed 后整体回滚，据此领奖会误领）。
- 与单任务/一键完成/队列共用 per-account 锁（`tryLockAccount`），命中即整号跳过。
- 统计结构：`TaskSweepStats{Accounts, Skipped, Accepted, Claimed, Credits, Energy, Errors}`。

---

## 5 `internal/upstream` 非代理端点全表

> 说明：**没有 `checkin.go` / `credit.go`**——签到与额度都在 `client.go`（`2113-2497`）；`idle.go` 是 SSE 流空闲看门狗（不发上游请求），已在末尾单列。

### 5.1 端点总表

| # | 功能 | 方法 + 路径 | base | 信封/头 | body |
|---|---|---|---|---|---|
| 1 | 签到 | `POST /v2/billing/meter/daily-checkin`（global 先试无 `/v2`） | billingBase | `billingJSON` | `{}` |
| 2 | 额度（个人） | `POST /billing/meter/get-user-resource`（global 先）/ `/v2/.../get-user-resource`（CN） | billingBase | `billingMeterJSON` | 见 5.3 |
| 3 | 额度（企业） | `POST /v2/billing/meter/get-enterprise-user-usage` | billingBase | `billingJSON` | `{}` |
| 4 | trial 加油包 | `POST /billing/ide/trial` | billingBase | `billingJSON` | 无 |
| 5 | 新手礼包 | `POST /billing/meter/claim-gift` | billingBase | `billingJSON` | `{}` |
| 6 | 活动补偿 | `POST /billing/meter/claim-compensation` | billingBase | `billingJSON` | `{}` |
| 7 | 补偿状态 | `POST /billing/meter/compensation-status` | billingBase | `billingJSON` | `{}` |
| 8 | 活跃上报 | `POST /v2/report` | billingBase | `billingJSON` | 事件数组（5.6） |
| 9 | 连登状态 | `GET /activity/growth/streak` | chatBase | `growthJSON` | — |
| 10 | 连登兑换 | `POST /activity/growth/redeem` | chatBase | `growthJSON` | `{"tier":"7d","client_token":"<uuid>"}` |
| 11 | 抽奖次数 | `GET /activity/growth/lottery/summary` | chatBase | `growthJSON` | — |
| 12 | 抽奖 | `POST /activity/growth/lottery/draw` | chatBase | `growthJSON` | `{"client_token":"<uuid>"}` |
| 13 | 热力图 | `GET /activity/growth/heatmap` | chatBase | `growthJSON` | — |
| 14 | 用补签卡 | `POST /activity/growth/makeup-cards/use` | chatBase | `growthJSON` | `{"target_date":"2006-01-02"}` |
| 15 | 猫信息 | `GET /activity/growth/buddy/info` | chatBase | `growthJSON` | — |
| 16 | 领养首猫 | `POST /activity/growth/buddy/first` | chatBase | `growthJSON` | `{}` |
| 17 | 猫协议 | `POST /activity/growth/buddy/agreement` | chatBase | `growthJSON` | `{"agree":true}` |
| 18 | 旅行状态 | `GET /activity/growth/buddy/travel/status` | chatBase | `growthJSON` | — |
| 19 | 派猫出行 | `POST /activity/growth/buddy/travel/depart` | chatBase | `growthJSON` | `{"location_id":4}` |
| 20 | 旅行领奖 | `POST /activity/growth/buddy/travel/claim` | chatBase | `growthJSON` | `{"record_id":<int64>}` |
| 21 | 任务列表 | `GET /v2/activity/growth/tasks` | chatBase | `growthJSON` / `growthJSONMP` | — |
| 22 | 接受任务 | `POST /v2/activity/growth/tasks/accept` | chatBase | `growthJSON` / MP | `{"task_codes":["..."]}` |
| 23 | 任务领奖（Web） | `POST /activity/growth/tasks/{code}/claim` | **webBase** | 手写头（5.7） | 无 |
| 24 | 任务领奖（mp） | `POST /activity/growth/tasks/{code}/claim` | chatBase + `X-Client-Platform: miniprogram` | `doJSON` | 无（400 回落 23） |
| 25 | 开学季券码 | `GET /portal/activity/school/vouchers` | billingBase | `schoolJSON` | — |
| 26 | 小程序埋点 | `POST /v2/report` | **BillingBaseCN**（写死 CN） | 手写 mp 头（5.8） | 事件数组 |

三个发射器：
- `growthJSON(a, method, path, body)` = `http.NewRequest(method, c.chatBase(a)+path, json(body))` + `c.BillingHeaders(req, a)` + `c.doJSON(req)`。
- `growthJSONMP` = 同上 + `req.Header.Set("X-Client-Platform", "miniprogram")`。
- `billingJSON(a, method, path, body)` = `c.billingBase(a)+path` + `BillingHeaders` + `doJSON`。
- `billingMeterJSON(a, paths, method, body)` = 依次试 `paths`，**仅 `ErrNotFound`（404）换下一路径**，其它错误立即返回。
- `schoolJSON(a, method, path, body, out)` = `billingBase + "/portal/activity/school" + path`，头：`Authorization: Bearer` + `Content-Type` + `Accept: application/json` + `X-User-Id`。

`billingMeterPaths(a)`：global → `["/billing/meter/get-user-resource", "/v2/billing/meter/get-user-resource"]`；CN → `["/v2/billing/meter/get-user-resource"]`。
`checkinMeterPaths(a)`：global → `["/billing/meter/daily-checkin", "/v2/billing/meter/daily-checkin"]`；CN → `["/v2/billing/meter/daily-checkin"]`。

### 5.2 `doJSON` 与错误分类（照抄这段即可复刻错误语义）

```go
raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
if resp.StatusCode >= 400 { return &Error{Kind: Classify(status, string(raw)), Status: status, Msg: truncate(raw,200)} }
var env struct{ Code int; Msg string; Data json.RawMessage }
if json.Unmarshal(raw, &env) != nil { return fmt.Errorf("parse failed: %w (body: %s)", ...) }
if env.Code != 0 { kind := Classify(status, env.Msg); if kind == ErrNone { kind = ErrClient }
                   return &Error{Kind: kind, Status: status, Msg: fmt.Sprintf("code=%d msg=%s", code, truncate(msg,160))} }
return env.Data, nil
```
`ErrKind` 枚举（`client.go:31-48`）：`ErrNone, ErrHardCredit, ErrSoftRate, ErrSessionDead, ErrNotFound, ErrServer, ErrContentBlocked, ErrBadParams, ErrAccountFault, ErrChannelRefused, ErrModelBlocked, ErrWafBlock, ErrPromptTooLong, ErrImageInvalid, ErrUpstreamBusy, ErrClient`。
关键 marker 常量：
- `alreadyCheckinMarkers = []string{"已签到", "already"}`（只匹配 `*Error.Msg`）
- `sessionDeadMarkers = []string{"Offline user session not found", "12153"}`
- 业务码：`11102` 无此模型、`11158` 无权限、`11115` prompt too long、`11135` 图片无效、`11101` 参数解析失败、`11128` 通道校验、`11140` 账号限制、`6004` 模型级限流、`6021` 队列满、`14018` 积分耗尽、`14051` trial 已领、`10001` 企业不支持、`12153` session 失效。

### 5.3 额度请求/响应细节（`client.go:2023-2129, 2266-2393`）

请求 body（与 `cmd/credit` 完全一致）：
```json
{"PageNumber":1,"PageSize":100,"ProductCode":"p_tcaca","Status":[0,3],
 "PackageEndTimeRangeBegin":"YYYY-MM-DD HH:MM:SS","PackageEndTimeRangeEnd":"...（now+365*101 天）"}
```
响应（`doJSON` 已剥 `{code,msg,data}`，所以从 `Response` 开始解）：
```text
data.Response.Data.Accounts[] {
  PackageName, CapacityRemain, CapacityUsed, CapacitySize,
  CycleCapacityRemain, CycleCapacityUsed, CycleCapacitySize,
  ExpiredTime, PackageEndTime, CycleEndTime,      // 前两者实测恒空
  DeductionEndTime (epoch ms),                    // 真正失效时刻
  CreateTime (epoch ms), PackageCode, SubProductCode, SubProductName
}
data.Response.Data.TotalDosage                     // size 下限
```
- `packageEffectiveEnd`：`DeductionEndTime>0` 优先 → 否则依次 `ExpiredTime / PackageEndTime / CycleEndTime`（`"2006-01-02 15:04:05"` 按 UTC+8 解析）。
- 单包聚合用 `packageRemainUsed`（见 2.3）。
- 企业额度：`{"credit":812.45,"limitNum":2000,"limit_num":...,"used_num":...,
  "cycleStartTime":"...","cycleEndTime":"...","cycleResetTime":"..."}`
  `credit` 是**本周期已用**（与个人口径相反）→ `remain = limitNum - round(credit)`、`total = limitNum`；`limitNum<0` 视为不限量 → `remain = 1<<40`、`total = -1`（面板显示「不限」）；到期批次用 `cycleEndTime`（缺失回落 `cycleResetTime`）。
- 快过期分桶：`soon>0` 且到期 ≤ `now+soon` 的余额计入 `expiring`（是 `remain` 的子集）；批次按自然日聚合成 `[]ExpiryBucket{Date,ExpireAt,Remain}`（升序）。

### 5.4 trial（`upstream/trial.go`）

```go
trialPath = "/billing/ide/trial"
trialAlreadyMarkers = []string{"code=14051", "`"code":14051`"}
func (c *Client) ClaimTrial(a) (claimed bool, err error) {
    if a == nil || a.Realm() != "global" { return false, fmt.Errorf("claim trial: only global accounts") }
    _, err = c.billingJSON(a, POST, trialPath, nil)
    if err != nil { if *Error && trialAlreadyErr(ue.Msg) { return false, nil }; return false, err }
    return true, nil
}
```
语义：`claimed=true` 新领成功；`false + nil` = 幂等已领；`err` = 真失败。

### 5.5 连登 / 抽奖（`upstream/streak.go`）

```go
streakRedeemPath   = "/activity/growth/redeem"
lotterySummaryPath = "/activity/growth/lottery/summary"
lotteryDrawPath    = "/activity/growth/lottery/draw"
func clientToken() string   // 16 random bytes → "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```
响应结构（`StreakFull`）：
```text
streak { days, month_total_days, next_tier, next_tier_remaining }
makeup_cards { balance, max }
redemption_status {
  tier_7d_status, tier_14d_status, tier_28d_status, remaining_days,
  tiers[] { tier, days, credit, energy, cards, chances }
}
lottery/summary { chances, module { enabled } }
```
`GrowthRedeemTier(a, "7d"|"14d"|"28d")` 未解锁返回 HTTP 403（`连续登录天数不足`），调用方按 locked 跳过。
`LotteryDraw` 返回原始 `data`（奖品载荷形状随活动期变化，透传）。

### 5.6 活跃上报 `POST /v2/report`（`upstream/report.go`）

body 是**单元素数组**，元素字段（一条 `chat_request_send`，共 40 个）：
```text
eventCode="chat_request_send"  timestamp=<ms>  reportDelay=0  mode="craft"
conversationId  requestId  inputLength=12  requestModelId  requestModelName
isPlan=false isAutoExecuteTerminal=false isAutoModify=false codebaseEnable=false
maxToken=0 maxSteps=0 temperature=0 maxRetries=0
mentionContexts=[] knowledgeId=[] knowledgeName=[] codebaseId=""
mentionContextCount=0 command="" expertId="" recommendId="" skillId=""
skillCount=0 totalCount=0 fileUri="" presentAt=<ms> traceId="" rootRequestId=requestId
parentConversationId=conversationId agentName="default" agentType="conversation"
userId=<a.UID>      // 必填！缺失 → 上游 200 但静默丢弃
```
- `ReportChatActivity(a, cid, rid)` → 默认模型 `deepseek-v4-flash / "DeepSeek V4 Flash"`；`rid==""` 时回落 `cid`。
- `ReportChatActivityModel(a, cid, rid, modelID, modelName)` 可指定模型（如 `glm-5.2`）。
- 调用方生成的会话 id 形如 `wb2api-<ms>` / `wb2api-night-<ms>-<i>` / `wb2api-adopt-<ms>`，服务端不校验一致性。
- 风控口径：每号每天 1 次即可。

### 5.7 任务（`upstream/tasks.go`）

```go
tasksListPath   = "/v2/activity/growth/tasks"
tasksAcceptPath = "/v2/activity/growth/tasks/accept"
mpPlatform      = "miniprogram"
```
`Task` 对外字段：`task_code, title, description, task_desc, credit, energy, has_reward,
reward_buddy, task_type, tag, jump_url, locked, target, current, accept_status, status,
claimable, claimed`；其中 `credit/energy` 来自上游 `reward_credit / reward_energy`；
`target/current` 恒输出（0 是有效进度）；`progress` 若为 `{current,target}` 对象则覆盖平铺字段；
`claimable = !claimed && target>0 && current>=target`；`claimed = accept_status=="claimed"`。

**领奖端点差异（踩过的坑，务必照抄）**
```text
Web（普通任务）: POST {webBase}/activity/growth/tasks/{taskCode}/claim     # 任务码在路径，无 body
  头: Authorization: Bearer
      Accept: application/json, text/plain, */*
      Content-Type: application/json
      Origin: https://www.workbuddy.cn
      Referer: https://www.workbuddy.cn/profile/growth-center
      x-client-platform: web
      User-Agent, X-User-Id, X-Enterprise-Id + X-Tenant-Id, X-Domain
mp（小程序任务）: POST {chatBase}/activity/growth/tasks/{taskCode}/claim   # 同样无 body
  BillingHeaders + X-Client-Platform: miniprogram
  → HTTP 400 时回落 Web 端点（部分任务/租户形态）
```
**不存在**的端点：`/v2/activity/growth/tasks/reward/claim`（任务码放 body）——一直返回 400 `task not completed`。
领奖响应 `data = {"already_claimed":bool,"credit":n,"energy":n}`；`already_claimed` → 记 `0/0`（幂等，不算错误）。

**mp 专属任务码白名单**（`autotask.go:201-213`）：
`school_season, Sequential_Tasks_1..7`（`isMPTaskCode` 决定 accept/claim/回读走 mp 变体）。

### 5.8 小程序埋点（`upstream/school.go`）

```go
schoolBase = "/portal/activity/school"
mpReportPath = "/v2/report"
schoolOpenDayActivityID = "school_open_day_2026"
```
`mpEventBase(a)` 公共指纹（每条事件合并）：
```text
timestamp=<ms> ideType="WorkBuddy_MP" ideVersion="2.4.0"
extName="workbuddy-mp" extVersion="2.4.0" product="SaaS"
ideName="wx_app_cloud" platform="mini_program" os="windows" osVersion="11" arch="x64"
machineId="0655736a-607f-4d9d-b430-58176ee9a090" timezone="Asia/Shanghai"
userId=<uid> userNickname=<nickname>
```
`ReportMPEvent` 头：`Authorization: Bearer` + `Content-Type: application/json` + `Accept: application/json` + `X-User-Id` + `X-Client-Product: workbuddy-mp` + `X-Client-Version: 2.4.0` + `X-Client-Platform: mp-weixin` + `X-Platform: wechatmp`；URL 写死 `c.BillingBaseCN + "/v2/report"`。

事件构造函数（必须按形状照抄）：
- `SchoolChatTimesEvents(cid)`：`chat_request_send` + `inputLength 14` / `maxSteps 500` / `agentName "mp"` / `agentType "main"` / `traceId=rootRequestId="wb2api-"+uuid` / `messageId "msg-"+rid 尾 8 位` / `codebuddy.session_id=cid` / `codebuddy.conversation_request_id=rid`。
- `SchoolSeasonChatEvent(cid)` = 上者 + `activityId="school_open_day_2026"`（无 `activityId` 不点亮）。
- `MiniExpertUseEvent(expertID, expertName, expertType)`：`eventCode "expert_actual_use"`、`reportDelay 0`、`extVersion "2.2.8"`、`source "mini_program"`、`id/name=expertID`、`expertTitle=expertName`、`type "send_message"`、`characterCount 12`、`expertType（默认 "agent"）`；**不带** `conversationId/activityId`。
- `MiniChatModelEvent(cid, modelID, modelName)` = `SchoolChatTimesEvents` + `requestModelId / requestModelName`。
- `MiniPlaybookEvents(caseID, caseName)`：两条 `playbook_cta_click`（`source "discover"`, `position 1`, `extVersion "2.2.8"`）→ `playbook_prompt_send`（`source "discover"`, `promptLength 96`, `isOfficial 1`, `conversationId "wb2api-mp-pb-"+uuid`, `extVersion "2.2.8"`）。

券码：`GET /portal/activity/school/vouchers` → `data.items[] = {grant_id, draw_uuid, sku_code, prize_name, code, valid_from, valid_to, granted_at}`（`skus：kfc_ice_cream / voucher_luckin / voucher_kugou …`）。注释记载但**未实现**：`POST /tasks/share-complete {channel:"wechat"}`、`POST /wheel/draw {draw_uuid}`。

### 5.9 `idle.go` —— 不是上游端点

`internal/upstream/idle.go` 是 **SSE 流空闲看门狗**：包装 `io.ReadCloser`，每次 `Read` 到 `n>0` 刷新 `lastRead`；后台 ticker 周期检查 `time.Since(lastRead) > idle` 就 `cancel()` 断流、释放租约。
- `monitorBody(rc, idle, cancel)`：`idle<=0` 直接返回原流（禁用监控）。
- `idleTick(idle) = clamp(idle/4, 10ms, 1s)`。
- 首字节阶段由 `Transport.ResponseHeaderTimeout` 管，这里不抢跑。
- `Close()`：`stopOnce` 关 `stopCh` → `cancel()` → `rc.Close()`（无泄漏）。

---

## 6 `internal/panel` — 导入 / 任务中心 / 自动任务 / 积分历史

### 6.1 导入账号 `POST /panel/api/import/cockpit`（`import.go`）

multipart，字段名必须 `file`（`ParseMultipartForm(32<<20)`）。文件是**数组**：
```json
[{"id":"...","email":"...","uid":"...","nickname":"...",
  "access_token":"...","refresh_token":"...","token_type":"...",
  "expires_at":1700000000000,"domain":"...",
  "dosage_notify_code":"...","payment_type":"...","status":"...",
  "usage_updated_at":0,"last_checkin_time":0,"checkin_streak":0,
  "created_at":0,"last_used":0}]
```
逐条规则：
1. `uid/access_token/refresh_token` 任一 trim 后为空 → skip，errors 追加 `missing required fields (id=%s)`。
2. `validImportUID(uid)`：非空、`len<=64`、仅 `[A-Za-z0-9_-]`（防路径穿越）→ 否则 skip `invalid uid`。
3. `realm = auth.ResolveRealm("", acc.Domain)`。
4. `expiresAt = acc.ExpiresAt / 1000`（cockpit 是毫秒）；`<=0` → `now+365d`。
5. `nickname` 空则回落 `email`。
6. `FilePath = filepath.Join(AuthDir, "workbuddy-<uid>.json")`；global 走 `BackfillRealmFor(a,"global")`，CN 走 `BackfillRealm()`；然后 `SaveAtomic`（失败 skip）。
7. `Pool.Add(a)` + `Pool.Revive(uid)`（新导入即「人工恢复」口径：清旧禁用/冷却/熔断）。
8. 顺带动作（失败只记日志，不阻断）：global → `GlobalCompleteRegistration` + `ClaimTrial`；CN → `DailyCheckin`；最后 `UserResource` → `ReenableIfCredits`。
返回：`{"ok":true,"total":N,"imported":M,"skipped":K,"errors":["..."]}`。

### 6.2 任务中心（`taskcenter.go`）

**扫描** `POST /panel/api/tasks/scan_all`（只读）
- 逐号并发（跳过 `Disabled`），门控 `IsGlobal` / `IsEnterprise`（**这两处用 IsEnterprise，不是 Session**）。
- 拉 `ListTasks` + `ListTasksMP`，合并去重（按 `task_code`），只保留 `growthPending`：
```go
func growthPending(t upstream.Task) bool {
    if t.Claimed { return false }
    if t.Locked  { return false }        // Sequential 族每日零点解锁一环
    return autoActionFor(t.TaskCode) != nil   // 必须在 25 项白名单里
}
```
- 返回：`{"ok":true,"accounts":[{uid,nickname,growth:[Task],growth_error?}],"pending_count":N}`。

**执行队列** `POST /panel/api/tasks/run_queue` body `{"concurrency":1-4,"growth":bool}`（`growth` 缺省强制 true）
- `concurrency` 钳到 `[1,4]`。
- `startGrowthQueue`：先置 `running=true` 占位（防并发双启）→ 扫描待办（门控含 `IsEnterpriseSession`，命中时打一条 WARN 并跳过）→ 组装 `items` → 空则回滚并返回 `"全部账号没有待办任务"` → `q.seq++` → 起 goroutine `runQueueItems`。
- 队列项：`queueItem{uid, nickname, kind:"growth", code, status, message}`；`status ∈ pending|running|done|skipped|error`。
- `runQueueItems`：`sem := make(chan struct{}, concurrency)`；每账号 `tryLockAccount`（拿不到 → 该号全部项标 skipped 「该账号有其它任务动作在执行」）；先 `acceptPendingTasks`（成功后 `Sleep(reportGap)`）；逐项 `runGrowthQueued`，项间 `Sleep(reportGap)`。
- 状态查询 `GET /panel/api/tasks/queue` → `{"running","total","conc","started","started_at","seq","items":[...]}`。

**调度器联动**：`panel.RunGrowthQueueOnce()` = `startGrowthQueue(1, true)`，挂到 `scheduler.GrowthHook`（默认 01:00）。

### 6.3 一键完成 / 自动任务（`autotask.go`）

**`autoActions` 表（顺序即依赖顺序，`task_code` 必须逐字一致）**

| # | task_code | 动作要点 |
|---|---|---|
| 1 | `chat_5` | 累计上报 5 条 `chat_request_send`（自动补差额） |
| 2 | `first_buddy` | 上报解锁 → `agreement` → `buddy/first`（+300） |
| 3 | `Model_chat_GLM5.2` | accept → `glm-5.2` 真实对话 1 次 → 带模型字段上报 |
| 4 | `RichMeow_Chat` | 桌面指纹（`workbuddy-desktop`）完整对话事件链 |
| 5 | `Buddy_App` | buddyapp 五连事件 |
| 6 | `Buddy_App_QQ` | 企鹅教师助手事件链 |
| 7 | `automation_1` | `automated_task_create_suc` 事件 |
| 8 | `Library_read` | `web_element_click(library_doc_intro_click)`（web 域） |
| 9 | `template_5` | `template_used` 事件组 ×5 |
| 10 | `playbook_prompt` | `playbook_prompt_send` 事件组 |
| 11 | `create_canvas` | `wbx_design_canvas_*` 事件组（+300） |
| 12 | `expert_5` | 真实专家列表 + 召唤链 + 真实 chat ×5 |
| 13 | `Expert_team_use_3` | 专家团链 ×3 |
| 14 | `Hp_Appearance` | `appearance/set` + `appearance_skin_apply` 事件 |
| 15 | `skill_1` | 真实对话 + `skill_info` 技能加载事件 |
| 16 | `Expert_lighthouse` | 轻量云专家召唤+使用链（chat 链带 `has_expert`） |
| 17 | `black_cat` | `Attempt:true`；23:00–08:00 窗口内补足 `glm-5.2` 对话 |
| 18 | `school_season` | mp：accept → mini 对话 + `activityId` 上报 → 领奖 |
| 19 | `Sequential_Tasks_1` | mp 首对话 |
| 20 | `Sequential_Tasks_2` | mp 选专家（`expert_actual_use`） |
| 21 | `Sequential_Tasks_3` | mp 五次对话 |
| 22 | `Sequential_Tasks_4` | mp 定时任务（判据待验证） |
| 23 | `Sequential_Tasks_5` | mp 用 GLM5.2（模型字段） |
| 24 | `Sequential_Tasks_6` | mp 十次对话 |
| 25 | `Sequential_Tasks_7` | mp 灵感功能 |
| — | 未实现 | `Expert_lighthouse` 之外的 `Expert_Philanthropy`（需真实捐款）、`skill_1` 之外的未知码 |

**关键节流常量（照抄）**
```go
reportGap        = 1050 * time.Millisecond   // 连续上报/项间
acceptBatchGap   = 1050 * time.Millisecond   // panel/tasks.go 批量 accept 批间
acceptBatch      = 20                        // 每次 accept 的 task_codes 上限
mpActionGap      = 2 * time.Second           // mp accept/上报/领奖之间
mpChatEventGap   = 45 * time.Second          // mp 对话事件间隔（+0~10s 抖动）
claimPollAttempts= 4; claimPollGap = 3 * time.Second   // 达标回读预算 ≈12s
```
**为什么 45s**：`Sequential_Tasks_3`「5 次有效对话」有反作弊——2s 连发 4 条会被先记进度再整体回滚（claim 返回 400 `task not completed`）；45s 间隔逐条上报全部存活。

**单任务入口** `POST /panel/api/accounts/{uid}/tasks/auto` body `{"task_code":"..."}`：
1. `IsEnterpriseSession()` → 422，文案指明「企业切换会话」与重登路径。
2. `autoActionFor(code)` 为空 → 501「该任务需要客户端内交互」。
3. `tryLockAccount` 失败 → 409。
4. `taskByCode`（双口径）→ `nil` 404；`Claimed` → 200 `{"ok":true,"skipped":true}`。
5. `act.run` → 失败 502。
6. 回读（mp 走 `taskByCodeMP`，普通走 `taskByCodeWaiting` 有界轮询）。
7. 响应：`{"ok","message","progress_before","progress_after","claimable","attempt","verify_supported":true}`；`claimable` 时自动领奖并补 `"claimed","credit","energy"`（领奖失败补 `"claim_error"`）。

**全量入口** `POST /panel/api/accounts/{uid}/tasks/auto_all`：同一把锁；`context.WithTimeout(5*time.Minute)`；锁在流水线 goroutine 内释放（HTTP 超时返回 504 但后台继续跑）。
`runAutoAll` 阶段：
- 阶段 0：批量 `AcceptTasks`（未 `claimed/locked/accepted/completed` 的全接受）→ `Sleep(reportGap)`；
- 阶段 0b：`AcceptTasksMP` 同理；
- 主循环：逐 `autoActions` 项 → 查任务（无 → `skipped`「该账号无此任务」）→ 已完成/达标（`claimed || current>=target`）→ **先补领**再 skipped（`issue #174`：不改这段会把奖卡死）→ 否则 `act.run` → 回读 → 达标自动领奖 → `Sleep(reportGap)`。
- 每项结果 map：`{"task_code","desc","status"(done|skipped|error),"message","progress_after","claimable","claimed","credit","energy","claim_error"}`。

### 6.4 积分历史（`internal/credithist`）

数据流：`upstream.Client.SetCreditObserver(fn)` —— 每次**成功**查到余额后回调 `fn(uid, credits)`（失败不回调，防假变动）→ `credithist.Ledger.Observe(uid, credits)`。

```go
type Entry struct { Time time.Time; UID string; Delta int64; Before int64; After int64 }  // Delta = After-After
const maxDelta = 1_000_000          // |Delta| 超限只更新基线不留痕
const defaultMaxEntries = 2000      // 环形上限（丢最旧）
const fileVersion = 1
type diskFile { Version int; Saved string; Snapshot map[string]int64; Entries []Entry }
```
`Observe` 语义：首次见 uid → 只建基线；余额不变 → 直接返回；`|Delta|<=maxDelta` → 追加；否则只更新基线。
HTTP：`GET /panel/api/credit_history?limit=200&uid=xxx`（limit ≤1000；有 uid 过滤时先全量读再筛，避免"看起来历史缺失"）→ `{"entries":[{time,uid,account,delta,before,after}],"limit":N}`；`account` 是读取时用池快照补的昵称。

### 6.5 面板登录（`panel/login.go`）

- `POST /panel/api/login/start` body `{"realm":"cn|global"}`（缺省 cn）→ `POST {base}/v2/plugin/auth/state?platform=CLI` body `{}` → 进程内 `p.logins[state] = loginSession{created, realm}`，顺手回收 `>loginTTL(15min)` 的会话 → `{"ok":true,"url":"<authUrl>","state":"...","realm":"cn"}`。
- `GET /panel/api/login/poll?state=` → `GET .../auth/token?state=`：
  - 未完成 → `{"done":false,"message":"code=..."}`（**HTTP 200**，前端继续轮询，前端间隔 3s）；
  - 成功 → `GET .../login/account?state=` + Bearer；`uid` 为空 → 502；`!validUID(uid)` → 502（防路径穿越）；
  - 落盘 `auths/workbuddy-<uid>.json`（global 写 `auth.realm=global`，CN 也显式补 realm）→ `Pool.Add` + `Pool.Revive`；
  - 顺带：global → `GlobalCompleteRegistration` + `ClaimTrial`；CN 且非企业 → `DailyCheckin`；最后 `UserResource` → `ReenableIfCredits`；
  - `IsEnterpriseSession() && !IsEnterprise()` → 打 WARN 提示「企业切换会话」；
  - 返回 `{"done":true,"uid","nickname","realm","credits","credits_total","checkin_message"}`。
- `GET /panel/api/login/regions` → 静态白名单（**不发上游**）：`HK Hong Kong, MO Macao, SG Singapore, TH Thailand, PH Philippines, MY Malaysia, ID Indonesia`。

---

## 7 `internal/pool` — 状态字段、状态机与持久化（桌面端状态列直接照这张表）

### 7.1 `Status` 对外字段（JSON tag 逐字，`entry.go:71-132`）

| 分组 | 字段 | 类型 | 含义 |
|---|---|---|---|
| 身份 | `uid` | string | 账号 UID |
| | `nickname` | string | 昵称 |
| | `realm` | string | `cn` / `global`（= `auth.Realm()`，含开关闸） |
| | `enterprise` | bool | `auth.IsEnterprise()`（计算值，不落盘） |
| 额度 | `credits` | int64 | 剩余积分 |
| | `credits_total` | int64 | 总额度；0=未知；**-1=企业不限量** |
| | `credits_expiring` | int64 | 窗口内（默认 168h）将过期积分（credits 子集） |
| | `credits_earliest_expiry` | time | 最早到期批次时刻 |
| | `credits_earliest_remaining` | int64 | 最早批次剩余 |
| | `credits_expiry_buckets` | []ExpiryBucket | `[{date,expires_at,remain}]` 按自然日升序 |
| 冷却 | `cooling` | bool | `now < until || now < breakerUntil` |
| | `cool_kind` | string | `hard_credit` / `soft_rate` / `breaker` |
| | `cool_remaining_sec` | int64 | 剩余秒（向上取整，取 until/breakerUntil 更晚者） |
| | `until` | time | 即时冷却截止 |
| | `reason` | string | 冷却/禁用原因（**仅 cooling 或 disabled 时输出**） |
| | `soft_streak` | int | 连续软冷却次数（指数退避指数） |
| | `rate_limited_models` | []{model,kind,until,reset_at,reason}` | 模型级限流台账；`kind ∈ rate_limit|model_unavailable` |
| 运维态 | `disabled` | bool | 终态禁用（需人工 revive） |
| | `disabled_reason` | string | 仅 disabled 时输出 |
| | `paused` | bool | 暂停选号（保号任务照跑） |
| 失败态 | `consecutive_fails` | int | 连败计数（**零值也输出**） |
| | `degrade_until` | time | 降权截止（非零且未过 = 降权中） |
| | `breaker_fails` | int | 熔断器连续失败数（运行态） |
| | `breaker_until` | time | 熔断截止（运行态） |
| 统计 | `success_count` / `err_total` | int64 | 累计成功/错误（err_total 不清零） |
| | `last_success` / `last_err` | time | 最近成功/错误 |
| | `token_usage` | TokenUsage | 见 7.2 |
| | `model_costs` | []{model,cost_per_1k,last_seen,samples}` | 实测单价台账（6h TTL） |
| 签到 | `checkin_done` | bool | `lastCheckinDay == 今天（本地时区）` |
| 运行态 | `in_flight` | int | 在途请求数 |

`TokenUsage`：`request_count, usage_count, prompt_tokens, completion_tokens, total_tokens, last_latency_ms, last_tokens_per_second, last_used_at, last_model`。

### 7.2 `entry` 内部字段（持久化 vs 运行态）

| 字段 | 持久化 | 说明 |
|---|---|---|
| `a *auth.Auth` | 否（凭证在 auths/*.json） | 池内同一指针 |
| `credits / creditsTotal` | 是 | 余额/总额 |
| `creditsExpiring / creditsEarliestExpiry / creditsEarliestRemaining / expiryBuckets` | 是 | 到期快照（落盘/恢复均惰性清洗） |
| `lastCheckinDay` | 是 | `"2006-01-02"` |
| `coolKind CoolKind / until / reason` | 是 | 即时冷却 |
| `disabled / paused` | 是 | 两个正交运维态 |
| `successCount / errTotal / lastErr / lastSuccess` | 是 | 统计 |
| `softStreak` | 是 | 软冷却退避指数 |
| `sessionDeadFails` | 是 | 连续 12153 计数 |
| `consecutiveFails / degradeUntil` | 是（`degradeUntil` 仅未过期写） | 连败降权 |
| `modelCooldowns map[string]modelCooldown` | 是（`stateModelCooldown{until,reset_at,reason,audit_only}`，过期不写；`Hits` 不落盘） | 模型级冷却/负缓存 |
| `modelCost map[string]modelCostEntry` | 是（`stateModelCost{cost_per_1k,last_seen,samples}`，6h 外不写） | 实测成本 |
| `tokenUsage` | 是 | 用量摘要 |
| `fails / retryCount / breakerUntil` | `fails` 否；`breakerUntil+retryCount` 是（未过期才写） | 熔断 |
| `lastUsed / usedSeq / inFlight` | 否 | 运行态（`usedSeq` 提供严格全序，避免 time.Now 精度问题） |

### 7.3 状态机（一个 entry 上的**并列或门**）

```text
                 ┌──────────── disabled=true（终态，需 Revive/ReviveDisabled）
                 │
entry.healthy(now) ── false 的条件（任一成立）：
   e.disabled || e.paused
   || (until != 0 && now < until)          // 即时冷却：CoolSoft / CoolHard
   || (breakerUntil != 0 && now < breaker) // 熔断：指数退避
   || (degradeUntil != 0 && now < degrade) // 连败降权
```
- **不叠加**：三者是并列或门，任一未到期即不可选 → 生效的必然是「最远者」，无需显式比较。
- `healthyForModel(now, model)` = `healthy` + 额外 `!modelCooled(now, model)`（该模型 6004 独立冷却）。
- `expiry(now)` = `min(until, breakerUntil, degradeUntil` 中仍在未来者`)` —— 全冷却兜底「最早到期优先」用它。

**转移函数（照抄调用点）**

| 事件 | 调用 | 效果 |
|---|---|---|
| 硬冷却（余额耗尽） | `CooldownUntilTomorrow4AM(uid, reason)` | `Cooldown(uid, CoolHard, nextDay4AM(now)-now, reason)` |
| 软冷却（429，有重置墙钟） | `CooldownSoftRate(uid, base, resetAt, reason)` | `until = min(resetAt, now+softRateMax)`，不堆指数 |
| 软冷却（429，无时间） | 同上，`resetAt` 零值 | 首次/恢复后：`softStreak++` + `base << (streak-1)` 封顶 `softRateMax`；**已在软冷却中不推进不延长** |
| 模型级 429 | `CooldownSoftForModel(uid, base, resetAt, model, reason)` | 写 `modelCooldowns[model]`，**不写 `until`**（切模型即可用） |
| 模型不可用 11102 | `BlockModelBackoff(uid, model, reason)` | `Until = now + 6h * 2^min(hits-1,4)` 封顶 24h |
| 模型恢复 | `BlockModelClear(uid, model)` | 只删 `reason` 前缀 `11102` / `11158` 的条目，不动 6004 |
| 审计模型限流 | `RecordModelRateLimitAudit(uid, model, reason)` | `AuditOnly=true`，不参与路由 |
| 一次失败（5xx 等） | `NoteError(uid)` | `errTotal++`、`lastErr=now`、`fails++`；达 `breakerThreshold` → 熔断 |
| 连败（未知 4xx/传输失败） | `NoteFailures(uid)` | `consecutiveFails++`；达 `degradeThreshold` → `degradeUntil = now + degradeCooldown`（已在降权期则不延长） |
| 成功 | `NoteSuccess(uid)` | 清 `fails/retryCount/breakerUntil/softStreak/sessionDeadFails/consecutiveFails/degradeUntil`；**不清** `modelCooldowns$（6004 独立） |
| 12153 | `NoteSessionDead(uid)` | `sessionDeadFails++`；达 `sessionDeadThreshold` → `Disable(uid, "12153 session dead")` 并清零 |
| 12153 恢复 | `ClearSessionDead(uid)` | 计数归零 |
| 签到/余额刷新 | `ReenableIfCredits(uid, remain, total)` | `remain>0 && !disabled` → 只解冻 **CoolHard**（`until/coolKind/reason`），不动熔断/软退避/模型台账；并清空到期明细（等 `SetCreditsSnapshot` 重写） |
| 全量复活 | `Revive(uid)` | 清 `disabled/paused/until/coolKind/reason/softStreak/modelCooldowns/sessionDeadFails/fails/retryCount/breakerUntil` |
| 只复活禁用 | `ReviveDisabled(uid)` | 只清 `disabled/reason/sessionDeadFails` |
| 暂停/恢复 | `Pause(uid)` / `Resume(uid)` | 只置 `paused`，不碰任何计数 |

### 7.4 关键常量（`pool/entry.go, cooldown.go, degrade.go`）

```go
// 熔断
defaultBreakerThreshold   = 3
defaultBreakerCooldown    = 30 * time.Minute
defaultBreakerCooldownMax = 6 * time.Hour        // 退避 = base * 2^retryCount，封顶
// 连败降权（issue #114）
defaultDegradeThreshold   = 5
defaultDegradeCooldown    = 10 * time.Minute
defaultDegradeCooldownMax = 2 * time.Hour
degradeReason             = "consecutive failures"
// 软冷却
defaultSoftRateMax        = 2 * time.Hour
softStreakShiftMax        = 16             // 1<<streak 溢出兜底
// 12153
sessionDeadThreshold      = 3
sessionDeadReason         = "12153 session dead"
// 模型负缓存（11102）
modelBlockBaseTTL = 6 * time.Hour; modelBlockShift = 4; modelBlockMaxTTL = 24 * time.Hour
// 其它
modelCostTTL = 6 * time.Hour
defaultCostExploreInterval = 30 * time.Minute
defaultIdleWeightPerHour = 0.5; defaultIdleWeightMax = 5.0
flushInterval = 5 * time.Second          // persist.go
persistLogEvery = 12                     // 连续落盘失败每 12 次打一条
```
`CoolKind`：`CoolHard`（余额不足 → 次日 04:00）、`CoolSoft`（429 → 短冷却）；`String()` 输出 `hard_credit` / `soft_rate`。

**`nextDay4AM(now)`**：`now.Hour() < 4` 返回**当天** 04:00，否则**次日** 04:00（凌晨触发等当天签到即恢复，返回次日会白冷一天）。

### 7.5 积分保底 `creditFloor`（`pool.go:45-51, pick.go:277-304`）

```go
creditFloor int64   // SetCreditFloor(n) 注入；0 = 关闭
func (p *Pool) floorBlockedForRealmModel(e *entry, model, realm string, now time.Time) bool {
    if p.creditFloor <= 0 || model == "" || e.credits >= p.creditFloor { return false }
    if mc, ok := e.modelCostOf(model, now); ok { return mc.CostPer1k > 0 }   // 实测台账优先
    if p.modelRateOf == nil { return false }
    rate := p.modelRateOf(realm, model)
    if rate == "" { return false }        // 目录未覆盖 = 未知 → 放行
    v, err := strconv.ParseFloat(rate, 64); if err != nil { return false }
    return v > 0
}
```
即：**触底 + 该模型收费 → 拦**；免费模型（tier 0 / 倍率 0）不拦。三条选号路径都判：普通轮换、粘性 `PickByUIDForModel`（拦则解绑粘性命中并打 WARN）、全冷却兜底 `pickEarliestExpiryLocked`。

### 7.6 `state.json` 格式（`persist.go`）

```json
{"accounts": {
   "<uid>": {
     "credits": 1234, "credits_total": 2000,
     "disabled": false, "paused": false, "reason": "", "until": "0001-01-01T00:00:00Z",
     "cool_kind": 0,
     "success_count": 0, "err_total": 0, "last_success": "...", "last_err": "...",
     "last_checkin_day": "2026-11-21",
     "token_usage": {...},
     "soft_streak": 0, "session_dead_fails": 0, "consecutive_fails": 0,
     "degrade_until": "...", "breaker_until": "...", "retry_count": 0,
     "credits_expiring": 0, "credits_earliest_expiry": "...", "credits_earliest_remaining": 0,
     "credits_expiry_buckets": [{"date":"2026-11-30","expires_at":"...","remain":500}],
     "model_cooldowns": {"glm-5.2": {"until":"...","reset_at":"...","reason":"6004 ...","audit_only":true}},
     "model_costs": {"deepseek-v4-flash": {"cost_per_1k":0.0,"last_seen":"...","samples":3}}
   }
 },
 "scheduling": {"cnglm-5.2": {"strategy":"round_robin","uid":"..."}}}
```
- 落盘：`json.MarshalIndent(sf, "", "  ")` → `stateFp+".tmp"`（`0600`）→ `Rename`；目录 `MkdirAll(0755)`。
- `scheduling` 键 = `realm + "\x1f" + model`，值 `{strategy, uid}`（`weighted_random / round_robin / sequential_exhaust`）。
- 旧字段迁移：`err_count`（连续错误）读入映射到 `err_total`，二者取较大者；不再回写 `err_count`。
- Redis 快照（可选）：`snapshot{stateFile, saved_at}`；`RestoreFromSnapshot` 择新——本地不可用 → 采用快照；快照 `saved_at` 不早于本地 mtime → 采用快照；否则本地优先（每种情形一条日志）。
- `breaker_until / degrade_until` 用 `*time.Time`（Go 的 `omitempty` 对非指针 `time.Time` 无效，零值会序列化成 `0001-01-01`）。

### 7.7 计数口径（设计状态列必看）

```go
CountsDetailed()            // total, healthy, cooling, disabled+paused, inFlightFull
CountsDetailedWithPaused()  // total, healthy, cooling, disabled, paused, inFlightFull
countsDetailedForRealm(realm)
  遍历：disabled → disabled++；paused → paused++；!healthy(now) → cooling++；否则 healthy++（再判 inFlightFull）
ServableNow()/ServableForRealm(realm)  // 存在 healthy 或 modelExempt 且未在途占满
```
`cooling` 含 `until` 与 `breakerUntil`（**不含** `degradeUntil`——降权号在计数里仍算 healthy，但选号会被 `healthy()` 排除；这点面板展示要单独说明）。

---

## 8 面板 HTTP handler 全清单（`panel.go:153-199`）

分类：**A=账号管理类**、**B=API 代理/网关观测类**、**S=静态资源**。

| # | 方法与路径 | 类 | 说明 |
|---|---|---|---|
| 1 | `GET /panel/{$}` | S | 返回 `index.html` 页面 |
| 2 | `GET /panel/app.js` | S | 前端主脚本（`index.go` 内嵌） |
| 3 | `GET /panel/mobile.css` | S | 移动端样式 |
| 4 | `GET /panel/mobile.js` | S | 移动端脚本 |
| 5 | `GET /panel/api/overview` | A/B | 池计数 + 每账号 `Status[]` + `model_locks` + 余额刷新节奏/时刻 + 版本/uptime/redis_mode/sticky_sessions |
| 6 | `GET /panel/api/logs` | B | 日志环形缓冲快照（`Ring(500)`，频道 chat/task/sys） |
| 7 | `GET /panel/api/request_metrics` | B | 进程内请求指标 + 最近 100 条 + 归档状态（nil → 501） |
| 8 | `GET /panel/api/request_logs` | B | JSONL 归档查询：`limit≤1000`，过滤 `outcome/account/model/client_ip/user_agent/from/to` |
| 9 | `GET /panel/api/credit_history` | A | 积分变动流水（`limit≤1000`，可 `uid` 过滤，昵称现场补） |
| 10 | `GET /panel/api/models` | B | 直连上游查模型目录（CN 加 `cn:` / global 加 `global:` 前缀；全空 → 502/503） |
| 11 | `POST /panel/api/login/start` | A | 发起 OAuth 设备授权，返回授权 URL + state + realm |
| 12 | `GET /panel/api/login/poll` | A | 轮询登录；完成即落盘 + 热加载 + 签到/激活 + 余额 |
| 13 | `GET /panel/api/login/regions` | A | global 注册可选地区静态白名单（7 个） |
| 14 | `POST /panel/api/import/cockpit` | A | 导入 cockpit 导出 JSON（multipart `file`） |
| 15 | `POST /panel/api/accounts/{uid}/revive` | A | 人工无条件恢复：清禁用+冷却+熔断+降权 |
| 16 | `POST /panel/api/accounts/{uid}/disable` | A | 人工禁用（`reason="manual disable (panel)"`） |
| 17 | `POST /panel/api/accounts/{uid}/pause` | A | 暂停选号（保号任务照跑） |
| 18 | `POST /panel/api/accounts/{uid}/resume` | A | 解除暂停（幂等） |
| 19 | `POST /panel/api/accounts/{uid}/checkin` | A | 单号签到 + 余额刷新（企业号跳过签到只刷额度） |
| 20 | `POST /panel/api/accounts/{uid}/balance` | A | 单号余额刷新（不动冷却） |
| 21 | `POST /panel/api/accounts/{uid}/remove` | A | 出池（立即落盘）后删 `auths/workbuddy-<uid>.json` |
| 22 | `GET /panel/api/accounts/{uid}/tasks` | A | 单号任务列表（默认 + mp 合并去重） |
| 23 | `POST /panel/api/accounts/{uid}/tasks/accept` | A | 接受任务 `{"task_codes":[...]}` |
| 24 | `POST /panel/api/accounts/{uid}/tasks/accept_all` | A | 接受全部未接受任务（每批 20 + 批间 1050ms，再单独跑 mp 批） |
| 25 | `POST /panel/api/accounts/{uid}/tasks/claim` | A | 领取单任务奖励（mp 码自动走 mp 端点；0/0 视为已领） |
| 26 | `POST /panel/api/accounts/{uid}/tasks/auto` | A | 一键完成单任务（动作 + 回读 + 自动领奖） |
| 27 | `POST /panel/api/accounts/{uid}/tasks/auto_all` | A | 一键完成全部可自动任务（5 分钟兜底超时，后台继续） |
| 28 | `POST /panel/api/tasks/scan_all` | A | 全账号任务扫描（只读，输出待办清单） |
| 29 | `POST /panel/api/tasks/run_queue` | A | 启动执行队列 `{"concurrency":1-4,"growth":true}` |
| 30 | `GET /panel/api/tasks/queue` | A | 队列状态轮询（含 `seq` 防串轮） |
| 31 | `GET /panel/api/school/vouchers` | A | 逐 CN 号查开学季券码（并发 3；global 标 error） |
| 32 | `POST /panel/api/checkin_all` | A | 触发全量签到（异步；末尾跑连登管家） |
| 33 | `POST /panel/api/travel_all` | A | 触发全量猫猫旅行巡检（异步） |
| 34 | `POST /panel/api/activity_all` | A | 触发全量活跃上报（异步） |
| 35 | `POST /panel/api/keepalive_all` | A | 触发全量 token 保活（异步） |
| 36 | `POST /panel/api/balance_all` | A | **同步**全量刷余额 + 昵称同步，返回最新 `accounts[]` |
| 37 | `GET /panel/api/packages` | A | 逐号查积分包构成（并发 3，按 remain 降序） |
| 38 | `GET /panel/api/usage` | B | 逐请求用量聚合（窗口 `from/to` 或 `hours≤1440`，默认 72h） |
| 39 | `POST /panel/api/usage/save` | B | 立即落盘用量桶 |
| 40 | `GET /panel/api/model_probes` | B | 只读透传 `probe_max_tokens.py` 结果文件（缺失 → 空集） |
| 41 | `GET /panel/api/config` | B | 读取配置（含 api_key 等，前端配置页） |
| 42 | `POST /panel/api/config` | B | 保存配置（热改调度/策略/保底等） |
| 43 | `GET /panel/api/compensation` | A | 全账号补偿状态（只读，并发 3） |
| 44 | `POST /panel/api/accounts/{uid}/compensation` | A | 单号领取补偿（查状态 → 待领才领）+ 刷余额 |
| 45 | `POST /panel/api/compensation_all` | A | 全量领取补偿（串行）+ 汇总 `{total,claimed,skipped,errored,credit}` |

统计：**A 类 31 个**、**B 类 10 个**、S 类 4 个（合计 45 行，其中 #5 overview 跨 A/B）。
统一鉴权：`withAuth` → `httpauth.VerifyBearer(r, apiKey())`，失败 `401 {"ok":false,"error":"invalid_api_key"}`；`api_key` 为空则放行；密钥经 `livecfg` 快照读取（改配置下一个请求即生效）。
统一响应：`writeJSON(w, status, v)` / `writeErr(w, status, msg) → {"ok":false,"error":msg}`；入口 `ServeHTTP` 先 `setSecurityHeaders(w)`。

前端视图（`index.html` 的 7 个 `<section class="view">`）：`view-accounts`（#accounts）、`view-usage`、`view-packages`、`view-taskscenter`、`view-models`、`view-config`、`view-logs`；添加账号弹窗两个页签 `data-tab="login"`（浏览器登录）/ `data-tab="import"`（导入 JSON）。

---

## 9 桌面端状态列设计（可直接落地）

### 9.1 建议列定义（按 `pool.Status` 字段直出，无需二次计算）

| 列 | 取值来源 | 展示规则（照抄可复刻语义） |
|---|---|---|
| 账号 | `nickname` + `uid` 尾 8 位（`logfmt.UID8`） | 昵称空回落 uid |
| 域 | `realm` | `cn` = 蓝，`global` = 紫；企业号再叠 `enterprise` 徽标 |
| 可用性 | 计算：`disabled ? "禁用" : paused ? "暂停" : cooling ? "冷却" : degraded ? "降权" : "可用"` | 判定顺序**必须**同 `countsDetailedForRealm`（disabled → paused → !healthy → healthy），`degraded` 用 `degrade_until` 非零且未过期补判 |
| 签到 | `checkin_done`；`realm=="global"` 或 `enterprise` 显示 `N/A` | true = 绿勾「已签」，false = 灰「签到」 |
| 积分 | `credits` / `credits_total` | `credits_total==-1` 显示「不限」；`==0` 显示「未知」 |
| 快过期 | `credits_expiring` + `credits_earliest_expiry` | 仅当 `>0` 时红字「N 分 / M 天后到期」 |
| 冷却 | `cool_kind` + `cool_remaining_sec` | `hard_credit` 红、`soft_rate` 橙、`breaker` 深红；剩余秒转 `1h23m` |
| 原因 | `reason`（仅 cooling/disabled） | tooltip |
| 模型锁 | `rate_limited_models[]` | 每模型一个 chip：`model` + `kind` + `until` |
| 失败 | `consecutive_fails` / `breaker_fails` / `success_count` / `err_total` | 成功率 `success/(success+err_total)` |
| 在途 | `in_flight` | 与 `Pool.inFlightFull` 阈值对齐 |
| 用量 | `token_usage.total_tokens` / `last_tokens_per_second` / `last_model` | 吞吐空值显示 `—`（失败时被置 nil） |
| 成本 | `model_costs[]` | `cost_per_1k<=0` = 免费（tier 0），`>0` = 收费（tier 2），缺失 = 未知（tier 1） |

### 9.2 刷新与数据新鲜度

- 页面级轮询 `5s` 拉 `/panel/api/overview`（源码注释明确：5 秒轮询 ≠ 数据 5 秒新鲜）。
- 余额/签到/到期快照只随**后台余额刷新**更新（默认 `balance_refresh_minutes = 5`）→ overview 已透出 `balance_refresh_sec` 与 `balance_refreshed_at`，界面**必须**标注「x 分钟前更新」。
- 签到状态只随签到排程（09:00 / 21:00）或手动按钮变化。
- 任务进度**不在 `Status` 里**，必须 `GET /panel/api/accounts/{uid}/tasks` 现拉（有 `ListTasks + ListTasksMP` 两次上游调用，别放轮询里）。

### 9.3 门控提示（UI 必须做的 4 处）

1. `realm=="global"` → 隐藏「签到 / 旅行 / 连登 / 夜猫子 / 成长任务」全部按钮。
2. `enterprise==true` → 同上，且额度显示「企业额度（周期重置）」。
3. `IsEnterpriseSession`（企业切换会话）→ 成长任务按钮置灰并提示「行为事件会被上游静默丢弃，请以个人身份重新登录」。
4. `paused` → 只灰「参与选号」，保号类按钮（签到/上报/保活/余额）保持可用；夜猫子是例外（跳过 paused）。

---

## 10 常量速查（跨包）

| 常量 | 值 | 位置 |
|---|---|---|
| `defaultClientVersion` / `defaultCliVersion` | `"5.5.4"` / `"2.137.1"` | upstream/headers.go |
| 登录 UA | `"CLI/2.63.2 CodeBuddy/2.63.2"` | cmd/login, panel/login |
| 登录 state 文件 | `os.TempDir()/wb2api-login-state.json` `0600` | cmd/login |
| 登录/授权 client 超时 | `30s` | cmd/login, panel/login |
| auth 文件权限 | `0600` | auth.SaveAtomic |
| token refresh I/O 超时 | `30s`（`refreshIOTimeout`） | upstream/client.go |
| `refreshTokenExpiresInMax` | `10 * 365 * 24h` | upstream/client.go |
| upstream HTTP 超时 | `120s`（chat 用 `Timeout=0` + 首字节/空闲约束） | client.go:806 |
| `billingRetryDelay` | `2s`（最多补打 2 次：2s、4s） | upstream/report.go |
| `claimPollAttempts` / `claimPollGap` | `4` / `3s` | panel/autotask.go |
| `reportGap` / `acceptBatchGap` | `1050ms` / `1050ms` | panel/autotask.go, tasks.go |
| `mpActionGap` / `mpChatEventGap` | `2s` / `45s` | panel/autotask.go |
| `acceptBatch` | `20` | panel/tasks.go |
| `travelAccountDelay` / `activityAccountDelay` | `800ms` / `800ms` | scheduler/travel.go |
| `adoptReportGap` | `1050ms` | scheduler/travel.go |
| `travelLocationID` | `4` | scheduler/travel.go |
| `wakeupGraceDelay` / `wakeupLateThreshold` | `5s` / `1s` | scheduler/scheduler.go |
| `wallclockCheckStep` | `1min` | scheduler/scheduler.go |
| `loginTTL` | `15min` | panel/panel.go |
| `Ring` 容量 | `500` | panel/panel.go |
| `credithist maxDelta / maxEntries` | `1_000_000` / `2000` | credithist |
| `flushInterval` | `5s` | pool/persist.go |
| `creditFloor`（example config） | `100` | config.example.json |
| `expiring_soon`（example config） | `"168h"` | config.example.json |
| 并发上限（补偿/包/券码/昵称） | `3` | panel, scheduler |

---

## 11 实测红线（照抄这些结论能省掉整轮 debug）

1. **global 打 CN billing 恒 401**：`www.codebuddy.cn` 不认 `workbuddy.ai` 的 token；global 必须走 `https://www.workbuddy.ai` 且**无 `/v2` 前缀**。
2. **global 的 `/v2/report` 可用**（code 0），能点亮连登——不要按「D4：global 无活跃体系」跳过（早期实现踩过，国际版账号永远点不亮连登）。
3. **企业号 `get-user-resource` 恒空 `Accounts`**（code 0 但 null），不是余额 0；必须分流到 `get-enterprise-user-usage`。
4. **企业切换会话「读得见、记不上账」**：`/v2/report` 200 但连登不涨、15 个成长任务全部 0/x——判据是 JWT `token_source=enterprise_switch`，不是 `enterpriseId`。
5. **`daily-checkin` 的「已签到」是业务错误**（code 10001 / 14001），HTTP 200 或 4xx 都可能；只认 `Msg` 含 `已签到` / `already`。
6. **领奖端点写错域会一直 400 `task not completed`**：正确是 `{webBase}/activity/growth/tasks/{code}/claim`（任务码在路径），不是 `{chatBase}/v2/activity/growth/tasks/reward/claim`。
7. **mp 任务缺 `X-Client-Platform: miniprogram` 会 `task not found`**，列表/accept/claim 三处都要带。
8. **`accept` 存在「200+OK 但没登记」形态**：必须回读 `accept_status != "not_accepted"` 验证，未生效重试一次（`acceptWithVerifyMP`）。
9. **计分是异步的**：行为上报后 5–8 秒进度才跳；一次性回读会误判未达标 → 必须 `claimPoll`（4×3s）。
10. **`accept_status=="completed"` 不可作领奖判据**（会短暂 completed 后整体回滚），只认 `current>=target`。
11. **mp 五次对话任务禁用 2s 连发**：会被反作弊整体回滚，必须 `45s + 0~10s 抖动`。
12. **`until` / `reason` 不主动清理**：`statusOf` 只在 `cooling||disabled` 时输出 `reason`，否则会把历史残留的「429 rate limit」与「可用」并排展示。
13. **`paused` 与 `disabled` 正交**：`/status` 口径合并，面板必须分开（`CountsDetailedWithPaused`）。
14. **`degradeUntil` 不计入 `cooling` 计数**，但 `healthy()` 会排除——桌面端状态列要单独补判，否则「5 个 healthy 却一个都选不上」。
15. **`breaker_until / degrade_until` 必须用 `*time.Time` 落盘**，否则 `omitempty` 对零值 `time.Time` 无效，会写出 `0001-01-01T00:00:00Z`。
16. **`reload token` 并发必须单飞**：上游对 `refreshToken` 反重放，并发第二发撞 `12153` 并作废整条 RT 链（账号从此恒 401，只能人工重登）。
17. **`/v2/plugin/auth/state` 是唯一取授权 URL 的入口，无 PKCE**：state 由服务端签发，poll 时把 state 原样回传即可。
18. **`uid` 拼文件名前必须校验 `[A-Za-z0-9_-]{1,64}`**（登录与导入两处都做了，路径穿越实测可利用）。

---

## 附：本次分析的源码覆盖清单

`cmd/login/main.go`(336) · `cmd/signin/main.go`(140) · `cmd/credit/main.go`(294) · `cmd/trial/main.go`(133) · `internal/auth/auth.go`(445) · `internal/scheduler/{scheduler.go(827), streak.go(121), travel.go(173), blackcat.go(60), compensation.go(277)}` · `internal/upstream/{client.go(签到/额度段), streak.go, travel.go, tasks.go, trial.go, report.go, school.go, blackcat.go, compensation.go, idle.go, headers.go}` · `internal/panel/{panel.go, import.go, tasks.go, taskcenter.go, autotask.go, tasksweep.go, login.go, index.go}` + `index.html` 视图结构 · `internal/pool/{state.go, entry.go, cooldown.go, degrade.go, persist.go, strategy.go, pick.go, modelview.go}` · `internal/credithist/credithist.go` · `config.example.json` · `login.sh / signin.sh / credit.sh`。
