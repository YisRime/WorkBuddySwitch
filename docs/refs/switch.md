# workbuddy-switch 实现级考古报告

> 只读代码考古，源仓库 `D:\Work\_wb_refs\workbuddy-switch`（Rust + Tauri v2 + React，账号切换器）。
> 用途：Python 版对标实现——本文所有字段名、常量、路径、请求头、加解密步骤、状态机均取自源码字面量，可直接照抄。
> 代码结构：`crates/wb-switch-core/src/modules/*.rs`（核心逻辑，三宿主复用）、`src-tauri/src/commands.rs`（Tauri 薄包装，共 **67** 条命令）、`src/pages|components|lib`（React 前端）。
> 约定：`~/.wb-switch` 一律指 `home_dir()/.wb-switch`，且 `home_dir()` 受环境变量 `WB_SWITCH_HOME` 覆盖（`config.rs:56-67`）。

---

## 0 结论速览（先看这 20 条）

1. 唯一数据根 `~/.wb-switch`（`store_dir()`），`WB_SWITCH_HOME` 可整体改根；账号库是 **JSON 数组** `accounts.json`，元素是**自由对象**（`serde_json::Value`），没有强 schema，靠 `uid` 作稳定身份、`id`（uuid v4）作本地主键。
2. 凭据字段有**两种合法形态**：非空明文字符串，或 WorkBuddy 5.6 起的**加密信封** `{"\$wbEncrypted": <any>, "envelope": <any>}`。判定函数 `is_envelope`，取值函数 `secret_value`（明文 / 信封都返回 `Some`，空串与普通对象返回 `None`）。
3. 展示字段必须走 `display_value`（标量透传、对象/数组**折叠为 null**），否则信封对象进入 React 子节点会触发 error #31 白屏（issue #100）。
4. 官方登录态文件（四段 JSON）路径：`<home>/AppData/Local/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`（国际版同目录 `workbuddy-desktop-ai.info`）；macOS 为 `Library/Application Support/...`，Linux 为 `.local/share/...`。
5. 认证文件旁边还有**退出标记** `<authfile>.logged-out`（是「完整文件名 + .logged-out」，不是替换扩展名）。WorkBuddy 优先读标记，标记存在即视为未登录——**切换成功必须删除它**，删不掉要报错、不能报告切换成功。
6. 切换顺序固定 6 步：**备份认证文件 → 关进程 → 恢复未完成会话写入 → 复制/同步会话（可选）→ 写认证文件（原子写 + 写后校验 + 清退出标记）→ 启动**。`restart=false` 时拒绝任何会话写入意图（显式报错而不是静默丢弃）。
7. 会话复制写**两件套**（正文 `projects/{workspace}/{cid}.jsonl` + `workbuddy.db` 的 `sessions` 行）；第三件套 `edge-sync-mapping-vN.db` **故意不写**——预写会让客户端认为「已迁移」而跳过上传，云端缺会话。
8. 会话写入有 7 阶段状态机 `OpPhase`：`Prepared → BodyWritten → DbWritten → MappingWritten → LinksCommitted → Completed`，另有 `Abandoned`；每次切换开头先 `recover_pending_session_operations`，不可恢复就**暂停切换与启动**。
9. IDE/VS Code 的登录态是 Chromium **Safe Storage** 密文，存在 `state.vscdb` 的 `ItemTable`，键为 `secret://{"extensionId":"tencent-cloud.coding-copilot","key":"<key>"}`；值必须是 `{"type":"Buffer","data":[..]}` 的 JSON 字符串。三平台算法不同（见 §7.4）。
10. macOS 用 Keychain「`<app> Safe Storage`」密码 → PBKDF2-SHA1(**1003** 次迭代, salt=`saltysalt`) → 16 字节 key → AES-128-CBC，IV=16 个空格，前缀 `v10`。
11. Windows 用 `Local State` 的 `os_crypt.encrypted_key`（base64，前 5 字节是字面量 `"DPAPI"`，其余 DPAPI 解密）得到 32 字节 key → **AES-256-GCM**，密文布局 `"v10" || nonce(12) || ciphertext+tag`。
12. Linux 走 Secret Service 的 `v11`（AES-128-CBC，key 由 D-Bus `GetSecret` 取），无密钥环退回固定 key 的 `v10`。
13. 签到**仅国内版**（`WbVariant::supports_checkin()`），国际版在任何入口前置短路并返回 `{"result":"skipped","reason":"unsupported_variant"}`；签到请求 body 恒为 `{}`，路径 `/v2/billing/meter/{checkin-activity-status|checkin-status|daily-checkin}`，只有 **404** 才回落路径。
14. 积分（国内）三段查询：`/billing/meter/get-user-resource-summary`、`.../paid-packages`、`.../free-packages`，旧 `/v2/billing/meter/get-user-resource` 兼容回退；企业账号（`enterpriseId` 非空或 `type∈{ultimate,exclusive}`）改走 `/v2/billing/meter/get-enterprise-user-usage`。
15. 限额监听靠 **hook 脚本**（写 `~/.wb-switch/hook-events.jsonl`）挂到三处 `settings.json`：`~/.codebuddy`、`~/.workbuddy`、`~/.workbuddy-ai`；脚本全平台统一 `.sh`，Windows 用 PortableGit 的 `bash` 执行，最后必须输出 `{}`。
16. **导出/导入只有 JSON 数组**：没有 CSV、没有剪贴板、没有「导出脱敏」开关（导出必然是含 token 的完整记录；脱敏只出现在导入**预览**里）。
17. 导入去重键是 `uid`：同 uid **整条覆盖**（保留导入记录原样），缺 uid 才追加；补齐 `id`、保留本地 `note`/`displayField`、文件内重复 uid 也会收敛成一条。
18. OAuth 是**设备码三元组**：`POST {api}{/v2/plugin}/auth/state?platform=workbuddy|workbuddy-ai` → 轮询 `GET .../auth/token?state=` → `GET .../login/account?state=`；state 有效期 **600 秒**，且响应 `domain` 必须与发起档位匹配，否则拒绝入库。
19. 更新检查**不用 GitHub API**：先取 release 资产 `latest.json`（或 `latest-<os>-<arch>.json`），失败再解析 `/releases/latest` 的 302 `Location`；成功结果进程内缓存 **6 小时**。
20. 通知存档 `notifications.json` 最多 **100** 条、2 秒内同内容去抖；错误日志 `error.log` 是 JSONL，环形保留 **200** 行，`detail` 截断 **4096 字节**。

---

## 1 路径与文件布局总表

### 1.1 工具自身存储（`config.rs:69-130`，`store_dir()` 下）

| 文件 | 生成函数 | 内容/用途 |
|---|---|---|
| `accounts.json` | `accounts_file()` | 账号库，JSON 数组 |
| `backups/` | `backup_dir()` | 认证文件备份，文件名 `workbuddy-desktop.<ts>.info` / `workbuddy-desktop-ai.<ts>.info` |
| `auto_checkin_config.json` | `checkin_config_file()` | 自动签到配置 |
| `auto_checkin_logs.json` | `checkin_logs_file()` | 签到日志数组 |
| `auto_travel_config.json` | `travel_config_file()` | 派猫猫自动旅行开关 |
| `travel_cache.json` | `travel_cache_file()` | 旅行当日缓存 |
| `credit_usage_snapshots.json` | `credit_usage_snapshots_file()` | 积分本地观察快照 |
| `rate_limit_config.json` | `rate_limit_config_file()` | 限额监听开关 |
| `rate_limit_state.json` | `rate_limit_events::state_path()` | 限额已归因事件 + hook 文件读偏移 |
| `hook-events.jsonl` | `rate_limit_hook::events_path()` | hook 原始 payload（每行一条 JSON） |
| `official_usage_cache.json` | `official_usage_cache_file()` | 官方用量投影缓存 |
| `auto_rotate_config.json` | `auto_rotate_config_file()` | 自动轮换配置 |
| `auto_rotate_logs.json` | `auto_rotate_logs_file()` | 轮换日志（保留 200 条） |
| `auto_rotate_notify.json` | `auto_rotate_notify_file()` | 推迟提示每日配额（上限 5） |
| `workbuddy_exe.json` | `workbuddy_exe_cache_file()` | 应用路径缓存，**按档位分键**（`{"cn":..,"ai":..}`，旧单键 `exe` 只归国内版） |
| `codebuddy_cn_app.json` / `codebuddy_ide_app.json` | `codebuddy_cn_app_cache_file()` / `codebuddy_ide_app_cache_file()` | IDE 应用路径缓存（旧格式单键 `{"exe": ...}`） |
| `notifications.json` | `notifications` 模块 | 通知存档（100 条） |
| `error.log` | `error_log_path()` | 错误日志（JSONL，200 行环形） |
| `github_config.json` | `update::github_config_file()` | `{"owner","repo","proxy"}`（**永不保存 token**） |
| `vscode_ext.json` / `codebuddy_cn_ide.json` / `codebuddy_ide.json` / `jetbrains.json` | 各模块 `state_path()` | 各客户端「当前账号」本地状态（`activeAccountId`） |
| `session_links.json` | `session_link` | WorkBuddy 关联组主表（`version`+`revision`+`groups`） |
| `session-links/baselines/{ref}.json` | `baselines_dir()` | 配对基线（有序行摘要 + 总摘要 + 记录数） |
| `session-links/operations/{id}.json` | `operations_dir()` | 复制/同步操作日志（阶段 + 预分配目标 UUID） |
| `session-links/previews/{id}.json` | `preview_tokens_dir()` | 预览凭据（一次性、版本绑定） |
| `locks/session-ops-{variant}.lock` | 档位操作锁 | 覆盖整个会话操作（跨进程） |
| `locks/session-links.lock` | 关联存储短锁 | 关联主表读改写 |
| `session-transactions/`、`backup-lifecycle/` | `session_backup` | 会话备份事务目录 + 生命周期记录 |

> 三个宿主（WorkBuddy 桌面 / VS Code 插件 / CodeBuddy IDE）共用同一套 `session_link` 内核，但通过 `LinkNamespace` 分开命名空间（`WorkBuddy` / `VscodeExt` / `CodeBuddyIde`），各自的文件名/目录名不同，互不可见（`session.rs:59-72`）。

### 1.2 客户端数据根（`variant.rs`）

| 项 | 国内版 `cn` | 国际版 `ai` |
|---|---|---|
| API 基址 `api_endpoint()` | `https://www.codebuddy.cn` | `https://www.workbuddy.ai` |
| 插件前缀 `api_prefix()` | `/v2/plugin` | `/v2/plugin`（相同） |
| OAuth `platform` | `workbuddy` | `workbuddy-ai` |
| 认证文件名 | `workbuddy-desktop.info` | `workbuddy-desktop-ai.info` |
| 数据根 | `~/.workbuddy` | `~/.workbuddy-ai` |
| CodeBuddy 系规范域 | `www.codebuddy.cn` | `www.codebuddy.ai` |
| 域名判据 | 非 `.workbuddy.ai` 后缀 | 后缀 `.workbuddy.ai` |
| Windows 映像名 | `WorkBuddy` | `WorkBuddyAI` |
| macOS app / bundle id | `WorkBuddy.app` / `com.tencent.workbuddy.mac` | `WorkBuddy AI.app` / `com.workbuddy.workbuddy-ai` |
| Linux 可执行 / 进程模式 | `/usr/bin/workbuddy` / `workbuddy` | `/usr/bin/workbuddy-ai` / `workbuddy-ai` |
| billing 路径候选 | 原路径 | 先 `/billing/meter...`（去掉 `/v2`），再回落 `/v2/billing/meter...` |
| 能力 | 签到 ✅、旅行 ✅、会话复制按探测 | 签到 ❌、旅行 ❌、会话复制按探测 |

- 认证目录三平台（`variant.rs: auth_file_path_at`）：macOS `Library/Application Support/CodeBuddyExtension/Data/Public/auth`、Windows `AppData/Local/CodeBuddyExtension/Data/Public/auth`、Linux `.local/share/CodeBuddyExtension/Data/Public/auth`。
- `WbVariant::parse`：`"ai"|"intl"|"global"` → Ai，其余（含空/未知）→ Cn；`from_account` 先看 `variant` 字段，再按 `domain` 后缀兜底。
- `codebuddy_domain_for(domain, variant)`：`workbuddy.cn/www.workbuddy.cn`→`www.codebuddy.cn`；`workbuddy.ai/www.workbuddy.ai`→`www.codebuddy.ai`；空域按档位补默认；其它域（企业自建/iOA）原样透传。
- `billing_paths` 回落**只在 HTTP 404** 时发生（`config::is_route_missing`），401/403/10085/`code=-1` 一律原样返回。

### 1.3 客户端其它目录（供 IDE/CLI 读取）

| 目标 | 路径/格式 |
|---|---|
| CodeBuddy CLI 设置 | `~/.codebuddy/settings.json`（`env.CODEBUDDY_AUTH_TOKEN`、`env.CODEBUDDY_INTERNET_ENVIRONMENT`、`env.CODEBUDDY_BASE_URL`、`apiKeyHelper`） |
| CLI 轮换状态 | `~/.codebuddy-rotate/state.json`（`active` 索引 + `activeAccountId` + `updatedAt`） |
| CLI helper | `~/.codebuddy-rotate/helper.cjs`（旧版 `helper.sh` / `helper.cmd`） |
| CLI 会话注册表 | `~/.codebuddy/sessions/<pid>.json`、`manual-*.json`（`lastHeartbeat` 判活） |
| CLI 日志 | `~/.codebuddy/logs/...` |
| 扩展会话树 | `<本地数据根>/CodeBuddyExtension/Data/<uid>/<VSCode|CodeBuddyIDE>/<uid>/history/<工作区目录名>/` |
| VS Code 用户数据 | `%APPDATA%\Code`、`~/Library/Application Support/Code`、`$XDG_CONFIG_HOME/Code`，内 `User/globalStorage/state.vscdb` |
| CodeBuddy CN IDE | 数据目录名 `CodeBuddy CN`，secret key `planning-genie.new.accessTokencn` |
| CodeBuddy 国际版 IDE | 数据目录名 `CodeBuddy`，secret key `planning-genie.new.accessToken` |
| JetBrains | `<JetBrains 配置根>/<IntelliJIdea|IdeaIC|PyCharm|PyCharmCE>/options/secret-storage.xml`，插件目录前缀 `coding-copilot` |

---

## 2 账号模型、登录态与切换

### 2.1 `accounts.json` 单条记录字段（`account.rs` + 各采集端）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string(uuid v4) | **本地主键**；删除、列表 key、导出选择都依赖它。缺 id 会被自动补（issue #111） |
| `uid` | string | 官方账号 id，**去重身份** |
| `email` / `nickname` | string 或信封 | 展示字段 |
| `enterpriseId` / `enterprise_id` | string | 企业 ID，决定积分/请求头分流 |
| `enterpriseName` / `enterprise_name` | string | 企业名 |
| `access_token` / `refresh_token` | string 或信封 | 凭据（信封原样保留） |
| `token_type` | string | 默认 `Bearer` |
| `domain` | string | 登录域（决定 Origin/X-Domain/API 基址） |
| `variant` | `"cn"`/`"ai"` | 档位，缺省国内版 |
| `expiresAt` / `refreshExpiresAt` | i64 ms | 绝对毫秒时间戳 |
| `refreshedAt` | i64 ms | 最近刷新时刻 |
| `createdAt` | i64 ms | 入库时刻（覆盖时保留原值） |
| `auth_raw` | object | 官方 `auth` 段原文（写回时作为基底） |
| `profile_raw` | object | 官方 `account` 段原文（写回时作为基底，另含 `phoneNumber`） |
| `needs_relogin` / `needs_relogin_reason` | bool / string | 刷新失败标记，成功后清除 |
| `note` | string ≤ 24 字符 | 本地备注（`NOTE_MAX_CHARS=24`） |
| `displayField` | `"nickname"`/`"phone"`/`"note"` | 卡片显示选择 |
| `type` | string | 历史备份里的 `ultimate`/`exclusive` 视为企业版 |

**取值语义（务必照抄）**：

- `get_str(v,key)`：仅字符串，`trim` 后为空返回 `None`。
- `display_value(acc,key)`：字符串/数字/布尔/null 原样；对象/数组（信封）→ `null`。
- `secret_value(v,key)`：非空字符串 → Some；含 `$wbEncrypted` 的对象 → Some(原样)；其余（空串、普通对象）→ None。
- `is_envelope(v,key)`：`v[key]` 是对象且含键 `$wbEncrypted`。
- `envelope_token_error(account)`：信封态 token 时返回可读文案（"该账号凭据为 WorkBuddy 加密信封态…"），**所有需要账号身份的请求（签到/积分/旅行）必须前置短路**，否则会发出空 `Bearer` 并回显 HTML 错误页（issue #94）。

### 2.2 身份合并（upsert）规则（`upsert_collected_account` / `upsert_account_in`）

1. 匹配优先级：**非空 uid** > 真实邮箱（`identity_email`：必须含 `@`、不是 `unknown`/`手动添加`、不等于 nickname/uid）> 追加。
2. 命中后保留本地 `id`（调用方持有的引用不失效）；`createdAt` 保留旧值；`variant` 缺失时继承旧值；`note`/`displayField` 缺失时继承旧值。
3. **信封保护**：新采集是信封 token 而旧记录持有**未过期明文 token**时，**不覆盖**（返回旧记录），避免每次自动 importLocal 冲掉扫码得到的明文凭据；只在明文过期后放行信封接管。
4. 展示字段兜底：新采集 `nickname/email/enterpriseName` 是信封时，保留旧记录的明文值。
5. 同一身份若命中多条旧记录（历史脏数据），删掉多余条目再插入一条。
6. 追加分支必须补 `id`（uuid v4）。

### 2.3 `account_meta`（前端契约，**不含 token**）

`{id, uid, email, nickname, phoneNumber, note, displayField, enterpriseName, expiresAt, refreshExpiresAt, refreshedAt, createdAt, needsRelogin, needsReloginReason, isEnterprise, variant}`；`phoneNumber` 实时从 `profile_raw.phoneNumber` 读取（不落库）；`isEnterprise` 由 `enterprise_id_of(acc).is_some() || type ∈ {ultimate, exclusive}` 判定。

### 2.4 请求头（`build_auth_headers`，`account.rs`）

```
Authorization: Bearer <get_str(access_token) 或空>
Accept: application/json
Content-Type: application/json
X-User-Id: <uid>                     # 有 uid 时
X-Enterprise-Id: <enterpriseId>      # 企业账号
X-Tenant-Id: <enterpriseId>          # 同值
X-Domain: <domain>                   # 有 domain 时
```

业务侧追加头：

| 场景 | 追加头 |
|---|---|
| token 刷新（`refresh.rs`） | `X-Refresh-Token: <refresh_token>`、`X-Auth-Refresh-Source: plugin`（**必需**，缺失时国际版返回 `invalid_grant` code 12153） |
| 积分/资源（`credits.rs`） | `X-Client-Platform: web`、`Accept: application/json, text/plain, */*`、`Origin: <origin>`、`Referer: <origin>/profile/plans-usage`；`origin` 从「与请求 URL 前缀匹配的已知 origin」里取（`https://www.workbuddy.ai` / `https://www.workbuddy.cn` / `https://www.codebuddy.cn`） |
| 旅行（`travel.rs`） | `x-client-platform: web`、`origin: https://www.codebuddy.cn`、`referer: https://www.codebuddy.cn/profile/growth-center` |
| OAuth 拉账号 | `Authorization: Bearer <access_token>`、`X-Domain: <domain>`（非空时） |

### 2.5 官方认证文件写回（`auth_file.rs`）

**结构（四段）**：`{"account": {...}, "auth": {...}, "accounts": [ ... ], "allAccounts": [ ... ]}`；`accounts` 与 `allAccounts` 写同一份数组。

- `build_account_obj(acc)`：以 `profile_raw` 为底，强制覆盖 `uid`、`nickname`；`setdefault` 补 `type="personal"`、`accountType=""`、`idp=""`、`oneidAccountId=""`、`areaInfoComplete=false`、`isCurrentOneIdEnterprise=false`、`isCurrentOneIdPersonal=false`、`isFirstLogin=false`、`isCreator=false`、`isAdmin=false`、`uin=""`、`phoneNumber=""`、`lastLogin=true`、`pluginEnabled=true`、`deployStatus={"statusCode":0,"statusMsg":"","detailMsg":""}`、`sso={"domain":"","domainModifiedTimes":0}`。
- `build_auth_obj(acc)`：以 `auth_raw.auth`（缺则 `auth_raw` 本身）为底；插入 `accessToken`（`secret_value` 原样，信封不降级）、`refreshToken`、`tokenType`、`domain`、`lastRefreshTime=now_ms`；`setdefault` `scope="openid profile offline_access email"`；有 `expiresAt` 时写 `expiresAt`、`expiresIn=(expiresAt-now)/1000 下限 0`、`refreshExpiresAt`（缺省沿用 auth_raw 的同名字段或 expiresAt）、`refreshExpiresIn`；无 `expiresAt` 时 `expiresIn=0`、`refreshExpiresIn=0`；再 `setdefault` `notBeforePolicy=0`、`sessionState=""`。
- **写入流程**：`create_dir_all(parent)` → 读旧文件（缺失按 `{}`）→ 取旧 `allAccounts`（回退 `accounts`，非数组按 `[]`）→ 按 `uid` 或 `id` 去重剔除目标 → 追加 `account_obj` → `serde_json::to_string_pretty` → `atomic_write`（同目录 `.tmp-<uuid>` + rename）→ **写后校验**（重读并比较 `auth.accessToken`，不等则报「认证文件写后校验失败，未写入目标账号」）→ 删除 `<path>.logged-out`（`NotFound` 视为成功；其它错误报「清理 WorkBuddy 退出标记失败（<path>）：<err>」）。
- 权限错误特殊文案：`PermissionDenied` → "无权限写入认证文件：请打开 系统设置→隐私与安全性→App 管理…"。

### 2.6 从本机导入（`import_from_auth_file` 字段提取顺序）

| 目标字段 | 提取顺序 |
|---|---|
| `uid` | `root.uid` → `account.uid` → `account.id` |
| `nickname` | `root.nickname` (secret) → `root.name` → `account.nickname` → `account.label` |
| `email` | `root.email` → `account.email` → `auth.email` |
| `access_token` | `auth.accessToken` → `auth.access_token` → `root.accessToken` → `root.access_token`（secret_value，**为空则整条放弃导入**） |
| `refresh_token` | 同上四种命名 |
| `token_type` | `auth.tokenType` → `auth.token_type` → `Bearer` |
| `domain` | `root.domain` → `auth.domain` |
| `expiresAt`/`refreshExpiresAt` | `root.*` → `auth.*`，经 `parse_ts`（数字原样；字符串 parse 成 f64 再取整，**不做秒/毫秒换算**） |
| `enterpriseName`/`enterpriseId` | `root.camel` → `root.snake` → `account.camel` → `account.snake` |
| 其余 | 新增 `id`(uuid) / `variant` / `auth_raw=root` / `profile_raw=account` / `createdAt=now_ms` |

### 2.7 刷新与保活（`refresh.rs`）

- 刷新 URL：`{variant.api_endpoint()}{variant.api_prefix()}/auth/token/refresh`；`POST`，body `{}`。
- 响应判定：`code ∈ {0,200}` 为成功；否则写 `needs_relogin=true` + 原因 "刷新失败(code=N): <message>"。
- 成功后：`access_token` 更新；`refresh_token` 有则替换；`expiresAt` 取 `expiresAt/expires_at` 经 `norm_ts`，缺则 `now_ms + expiresIn*1000`；`refreshExpiresAt` 依次取 `refreshExpiresAt\|refresh_expires_at` → `auth_raw.refreshExpiresAt` → `now_ms+refreshExpiresIn*1000`；写 `refreshedAt`；删除 `needs_relogin*`；`upsert_account`；Windows 额外调 `codebuddy_cli::sync_windows_env_for_account`。
- `ensure_fresh_token(account,cfg)`：`lazy_refresh_hours`（默认 24）内到期或已过期且存在 refresh_token → 刷新。
- `run_keepalive_cycle()`：`RunFlagGuard` 防重入；`keepalive_days<=0`（默认 0）时**无条件刷新所有带 refresh_token 的账号**；否则只刷新剩余不足 N 天的；缺 refresh_token 的标记 `needs_relogin`。返回 `{"checked": n, "refreshed": [{email,status∈{ok,failed,missing_rt},error}]}`。
- `norm_ts(v)`：字符串/数字 → i64，`< 10_000_000_000` 时视为秒并 ×1000。

### 2.8 切换（`switch.rs: switch_account`）

进度文案（通过 `ProgressFn` 回调，桌面端转成 `switch-progress` 事件）："开始切换账号…" → "正在关闭 WorkBuddy…" → "正在恢复会话写入…"（内部）→ "正在复制会话到目标账号…" / "正在同步会话到目标账号…" → "正在写入认证文件…" → "正在启动 WorkBuddy…" → "切换完成"。

分支规则：

1. 取账号（`find_account(id)`，找不到 → "账号不存在: <id>"）；档位来自账号自身，不来自参数。
2. `backup = auth_file::backup_auth_file(variant)`（返回备份路径或 None）。
3. `restart=true`：
   - `close_workbuddy(variant, 20)`（20 秒超时）失败直接返回错误；
   - `recover_pending_session_operations(variant)`：Err → "无法恢复未完成的会话写入（<err>），已暂停切换与启动 WorkBuddy；请稍后重试"；
   - `recovery_blocks_startup(report)`（存在 `retryable=false` 的 needs_recovery）→ "检测到无法安全恢复的会话写入（<detail>），已暂停切换与启动 WorkBuddy；…"；
   - 记录写入前未完成操作 id 集合 `pending_before`；
   - 复制（`copy_sessions_for_switch`）与同步（`sync_sessions_for_switch`）顺序执行；锁错误（文案前缀 `LOCK_BUSY_MESSAGE_PREFIX="会话操作正被其它进程占用"` 或 `LOCK_UNAVAILABLE_MESSAGE_PREFIX="会话操作不可用"`）→ 直接暂停切换；其它失败只写进报告；
   - 写入后差集 `newly_unfinished_writes(before, pending_operations)` 非空 → "本次会话写入未完成（<target.session_id>（操作 <id>）：<reason>；…），已暂停切换与启动 WorkBuddy；…"；
   - `share_sessions` 兼容路径返回 `{"error":"share_sessions 兼容路径暂未在 Rust 版实现"}`。
4. `restart=false`：携带复制意图 → `{"error": COPY_WITHOUT_RESTART_MESSAGE}`；携带同步意图 → `{"synced":[],"skipped":[],"errors":[{"error": SYNC_WITHOUT_RESTART_MESSAGE}]}`。
5. `write_account_to_auth_file` → （restart 时）`launch_workbuddy(variant, progress)`。
6. 返回：`{"ok":true,"account":display_name,"variant":"cn|ai","backup":<path|null>}`，并按需附 `sessionCopy` / `sessionSync` / `sessionShare` / `sessionRecovery`。

**恢复报告投影**：`{"recovered": <count>, "abandoned": <count>, "needsRecovery": [{"operationId","reason","retryable"}], "temporaryFiles": [...]}`。

---

## 3 导出 / 导入（`export_import.rs`，647 行；生产逻辑约 220 行）

### 3.1 对外函数表

| 函数 | 作用 | 失败文案（原文） |
|---|---|---|
| `parse_accounts_json(text) -> Result<Vec<Value>,String>` | 解析文件文本 | 空 → "文件内容为空"；非法 → "文件不是合法的 JSON：<err>"；非数组 → "文件内容应为 JSON 数组（账号列表）"；元素非对象 → "文件第 N 项不是合法的账号对象" |
| `preview_accounts(text)` | 生成**脱敏预览** | 同上 |
| `merge_import_records(&mut accounts, text, indexes)` | 纯函数合并 | 同上 + 索引越界计 skipped |
| `import_accounts(text, indexes)` | 读库 → 合并 → 写回 | 保存失败 → "保存账号库失败：<err>" |
| `select_export_records(accounts, ids)` | 挑出完整记录 | ids 全空 → "请先选择要导出的账号"；无命中 → "未找到要导出的账号" |
| `export_accounts(ids)` | 返回记录数组（含 token） | 同上 |
| `export_accounts_to_path(ids, path)` | 写入用户选择的绝对路径 | 见 3.5 |
| `write_records_to_file(dir, records, file_name)` | 写固定目录 + 文件名 | 见 3.5 |
| `validate_export_file_name` / `validate_export_path` | 路径白名单 | "导出文件名不合法" / "导出路径必须是绝对路径" / "导出文件名必须以 .json 结尾" |

### 3.2 文件格式（唯一格式，**没有 CSV / 剪贴板 / 分片**）

- 顶层必须是 **JSON 数组**；每项必须是 **JSON 对象**；文本 `trim` 后为空即报错。
- 导出写盘用 `serde_json::to_string_pretty`（2 空格缩进、UTF-8 中文原样），经 `config::atomic_write`（同目录 `<name>.tmp-<uuid>` + rename）。
- 导出内容是**账号库原始记录**（含 `access_token` / `refresh_token` / `auth_raw` / `profile_raw` / `note` / `displayField`），UI 明确提示「等同密码」。
- 导入按 `indexes` 选择，索引是**文件数组下标**（不是 id）；越界记 `skipped`。

### 3.3 合并策略（`MergeOutcome` 三态）

```
MergeOutcome = Appended | Overwritten | Skipped
```

单条记录判定顺序（`merge_import_record`）：

1. `account::secret_value(item,"access_token")` 为 `None`（缺失 / 空串 / 纯空白 / 普通对象）→ **Skipped**，不入库；
2. 有非空 `uid` 且本地存在同 uid 记录 → **Overwritten**：以导入记录为主体**整条替换**（不是字段级 merge）；导入记录缺 `note`/`displayField` 时用本地值补；导入记录缺 `id` 时优先用本地 `id`，本地也无 id 则生成 uuid v4；
3. 否则 → **Appended**：缺 `id` 时**必须**补 uuid v4（否则刷新回写会反复追加副本，issue #111）。

计数语义（`ImportResult`）：`imported` = 合入数（含覆盖与新增）；`skipped` = 未导入数（缺凭据 / 索引越界）；`overwritten` = 其中覆盖同 uid 的数量。
文件内重复 uid：先追加、后一条覆盖前一条 → `imported=2, overwritten=1`，最终库里只有 1 条（后者的 `id` 生效）。

### 3.4 预览（脱敏，前端导入弹窗唯一数据源）

每项字段：`{index, uid, nickname, email, hasToken, encrypted}`

- `uid/nickname/email` 一律经 `account::display_value`（对象 → null）；
- `hasToken` = `secret_value(item,"access_token").is_some()`；
- `encrypted` = `is_envelope(item,"access_token")`（加密信封可导入，但只能用于切换）；
- 顶层 `{"accounts":[...], "total": N}`；**永不返回 token 原文**。

### 3.5 导出路径校验（防目录穿越）

- `validate_export_file_name`：非空、不含正斜杠、不含反斜杠、不含 `..`、必须以 `.json` 结尾。
- `validate_export_path`：必须是**绝对路径**（`Path::is_absolute`），扩展名 `json` **不区分大小写**。
- 前端默认文件名：`wb-switch-accounts-YYYY-MM-DD.json`（月/日补零）。
- 桌面端：`@tauri-apps/plugin-dialog` 的 `save({title:"导出账号", defaultPath, filters:[{name:"JSON",extensions:["json"]}]})` → `export_accounts_to_path`；webui：`exportAccounts` 取数组后浏览器 Blob 下载。
- 导出成功后提供「在 Finder / 资源管理器 / 文件管理器中显示」（`revealItemInDir`）。

### 3.6 明确不存在的能力（Python 版不要臆造）

- 无 CSV 导入/导出；无剪贴板导入/导出；无「脱敏导出」开关或加密导出；无单条文本复制；无导入 dry-run 差异报告（只有预览 + 三个计数）。

### 3.7 导入弹窗行为（`import-accounts-dialog.tsx`）

选择 `.json` 文件 → 读文本 → `previewImportAccounts(text)` → 默认**全选** → 每行展示「昵称或邮箱或 uid 或 第 N 项」，右侧徽标 `加密凭据` 或 `缺少 token` → 「导入勾选账号」调 `importAccounts(fileText, [...selected])`，并把选中项中 `encrypted` 数量回传给宿主做能力提示。文案为「共 N 个账号，已选 M 个」「全选」「取消全选」。

---

## 4 会话复制 / 同步 / 关联（`session*.rs`）

### 4.1 WorkBuddy 会话「三件套」

| 件 | 路径 | 说明 |
|---|---|---|
| 正文 | `<data_root>/projects/{workspace}/{cid}.jsonl` | JSONL，行内含 `sessionId`；定位顺序：先 `projects/{cid}.jsonl`，再遍历子目录 `{dir}/{cid}.jsonl` |
| 元数据 | `<data_root>/workbuddy.db` 的 `sessions` 表 | 列 `id, user_id, title, custom_title, cwd, created_at, updated_at, deleted_at, is_playground`；`custom_title/is_playground/status` 可能缺列，代码按 `column_exists` 分支、缺列时 SELECT 常量占位保持列数 |
| 云端映射 | `<data_root>/edge-sync-mapping[-vN].db` 的 `edge_sync_mapping` | 列 `session_id, conversation_id, msg_channel, created_at`；**本工具不写** |

`edge_sync_db_path` 发现规则：扫数据根下所有 `edge-sync-mapping*.db`，`edge-sync-mapping.db`=版本 0、`-vN`=N（N 必须纯数字），取**最大版本**；同版本按路径定序；无候选回落 `edge-sync-mapping-v2.db`(cn) / `-v4.db`(ai)。

**能力探测** `session_copy_supported_at(root)`：`projects/` 是目录 **且** `workbuddy.db` 是文件 **且** 存在 `sessions` 表；否则统一文案 `SESSION_COPY_UNSUPPORTED = "该档位暂不支持会话复制"`（国际版实测无 `projects/`，不能按档位写死）。

### 4.2 复制（path B：生成新 id，云端可同步）

所有入口统一走 `copy_sessions_for_switch(account, ids)`（另有 `copy_sessions_cross` 用于跨客户端）：

1. 取**档位操作锁**（跨进程文件锁），先 `recover_pending_session_operations`；
2. 源 uid = 当前登录态 `current_user_uid(variant)`（读认证文件 `account.uid`）；
3. 每个源会话：读 `sessions` 行（`SELECT * FROM sessions WHERE id=? AND user_id=?`）→ 预分配目标 UUID 并**先持久化操作记录**（`Prepared`）→ 写正文（`BodyWritten`）→ 插入 `sessions` 行（`DbWritten`）→ 校验（`verify_session_row`）→ 提交关联（`LinksCommitted`）→ `Completed`；
4. 目标 uid 已存在同标题会话时由 `resolve_target` 决定跳过 / 改 id（`TargetResolution`）；
5. 返回形如 `{"copied":[...],"skipped":[...],"errors":[{"error":"..."}]}`（`switch.rs` 只读 `error` 键）。

不变量：目标 UUID 在任何副本写入前落盘；恢复复用同一 UUID，不产生第二个副本；复制失败本身不阻断切号，但「本次新产生的未完成操作」会阻断（§2.8）。

### 4.3 同步（增量，保留目标 sessionId / 标题 / 自定义标题）

- 预览 `session_links_preview(variant, target_acc)` 只读，产出**预览凭据** `PreviewToken{version=2, preview_id, created_at, binding}` 落 `previews/{id}.json`（保留 `KEEP_PREVIEW_TOKENS=200`），绑定 `group_fingerprint`。
- 执行 `sync_sessions_for_switch(account, selections)`；`SyncSelection{group_id, preview_token, mode}`，`mode` ∈ `FastForward | Overwrite | UnifyOverwrite | StatusOnly`（字符串序列化）；`parse_sync_selections` 解析前端数组。
- 执行前重新加载账号 / 成员 / 基线 / 正文并逐项核对凭据；不一致 → `REASON_PREVIEW_STALE = "previewStale"`（该项 skipped）。
- 写入阶段顺序：**备份 → 正文 → 数据库 → 组表 → 基线**；备份必须先成功且可核验（唯一目录、SQLite 一致性快照、目标正文逐文件备份 + 摘要、恢复清单），否则零写入。
- 备份清单 `SyncBackupManifest{version=SYNC_BACKUP_VERSION=1, ...}` 落 `<store>/session-transactions/<op>/manifest.json`；正文备份 `bodies/original-{sid}.jsonl`、`bodies/incoming-{sid}.jsonl`；DB 快照方法 `sqliteBackupApi`（`DB_BACKUP_PAGES_PER_STEP=1024`、`DB_BACKUP_TIMEOUT=30s`）。
- 归档同步常量：`SESSION_STATUS_ARCHIVED="archived"`、`ARCHIVE_TARGET_TERMINAL_STATUSES=["completed","error","terminated"]`、动作 `ARCHIVE_ACTION_STATUS_ONLY="statusOnly"`（只改状态，正文零写入）。

### 4.4 状态机（照抄这些字符串）

`OpPhase`（`Operation.phase`，落 `session-links/operations/{id}.json`）：

```
Prepared        // 已预分配目标 UUID，未写任何副本
BodyWritten     // 正文已写
DbWritten       // sessions 行已写
MappingWritten  // 映射阶段（兼容位；WorkBuddy 侧不预写 edge_sync_mapping）
LinksCommitted  // 关联组已提交
Completed       // 完成
Abandoned       // 未写入任何内容即放弃（不是成功）
```

| 枚举 | 取值 |
|---|---|
| `BackupState` | `Allocating`（禁止业务写入）→ `Protected`（备份完备、禁止回收）→ `CleanupPending`（可幂等回收）；字符串 `"allocating" / "protected" / "cleanupPending"` |
| `CleanupOutcome` | `Cleaned` / `Pending{reason}` / `Protected{reason}` |
| `MemberState` | `Active` / `Stale` / `Superseded`（后两者不自动复活） |
| `ContentState` | `Missing` / `Ready(ContentSnapshot)` / `Unavailable(String)` |
| `BaselineState` | `Ready(BaselineRecord)` / `Missing` / `Unverifiable(String)` |
| `SyncVerdict` | `Identical`（不写）/ `FastForward`（目标为来源严格有序前缀，默认勾选）/ `Ahead`（仅目标变化或来源是目标前缀）/ `Diverge`（双方都变或重排，默认不勾）/ `Unknown`（禁止同步） |
| `SyncMode` | `FastForward` / `Overwrite` / `UnifyOverwrite` / `StatusOnly` |
| `StoreState` | `Missing` / `Ready(LinkStore)` / `Unavailable(String)` |
| `LockError` | `Busy` / `Unavailable(String)`；文案前缀 `"会话操作正被其它进程占用"` / `"会话操作不可用"` |

### 4.5 关联数据结构（`session_link.rs`）

```
LinkStore      { version=1, revision: u64, groups: [LinkGroup] }
LinkGroup      { id, variant, created_at, members: [LinkMember], pair_bases: [PairBase] }
LinkMember     { member_id, account_id: Option, uid, session_id, variant: Option, state: MemberState, linked_at, last_synced_at: Option }
PairBase       { member_ids: [String;2], baseline_ref, normalization_version }
BaselineRecord { version=1, baseline_ref, normalization_version=1, created_at, record_count, total_digest, line_digests: [String] }
PreviewBinding { variant, group_id, group_fingerprint, source/target: PreviewMemberBinding, baseline_ref?, baseline_total_digest?, baseline_record_count?, verdict, source_status?, target_status?, archive_action? }
PreviewMemberBinding { member_id, account_id?, uid, session_id, raw_digest, normalized_digest, record_count }
Operation      { version=1, operation_id, kind:"copy"|"sync", variant, source_variant?, group_id, source/target: OperationMember, expected_content_digest, expected_record_count, phase, backup?, lifecycle_version?, cleanup_state?, last_error?, created_at, updated_at }
```

- 内容归一化：`normalize_jsonl(text, own_session_id)` → `NormalizedContent{record_count, line_digests, total_digest}`；摘要 **SHA-256 → 小写 hex**。
- 前缀判定：`is_ordered_prefix`、`is_strict_ordered_extension` 决定 FastForward / Diverge。
- 常量：`LINK_STORE_VERSION=1`、`NORMALIZATION_VERSION=1`、`BASELINE_VERSION=1`、`OPERATION_VERSION=1`、`SESSION_ID_MARKER="__wb_switch_session_id__"`、`KEEP_COMPLETED_OPERATIONS=20`、`PREVIEW_TOKEN_VERSION=2`、`KEEP_PREVIEW_TOKENS=200`、`OP_SCAN_PROBLEM_PREFIX="操作记录扫描不完整："`；短锁重试 `STORE_LOCK_RETRY=25` × `20ms`。
- `OperationScan{operations, problems, complete}`：`complete=false` 时不得把结果当作「不存在对应操作」来授权删除。

### 4.6 关联组列表 / 详情 payload（`session_groups.rs::group_payload`）

```
{ key: "<client>:<variantScope|all>:<groupId>",
  client, variantScope, groupId, groupVariant,
  title, projectLabel, latestActivityAt,
  memberCount, activeMemberCount, accountNames: [String],
  summaryStatus: "ok|ahead|diverge|unknown|empty", summaryText,
  safeSourceMemberId, hasSafeSource }
```

`include_detail=true` 时追加 `divergence{commonMemberIds, branches[[memberId]]}`、`members[{memberId, accountId, uid, sessionId, accountName, variant, linkState, versionStatus, title, projectLabel, updatedAt, recordCount, contentPreview, contentState, reason, canBeSource}]`、`addTargets`。
`SessionClient` 枚举：`WorkBuddy / VscodeExt / CodeBuddyIde`（决定数据根与 `SessionStoreSpec`）。

---

## 5 签到 / 积分 / 用量 / 限额

### 5.1 签到（`checkin.rs`，仅国内版）

| 步骤 | 端点 / 内容 |
|---|---|
| 状态（新） | `POST {api}/v2/billing/meter/checkin-activity-status`，body `{}` |
| 状态（旧，新接口失败才试） | `POST .../checkin-status` |
| 提交签到 | `POST .../daily-checkin`，body `{}` |
| 成功判定 | `code ∈ {0,200}`；读取 `today_checked_in` 或 `todayCheckedIn` |
| 幂等「已签到」 | 文案含 `已签到` 或小写含 `repeat` → `{"ok":true,"already":true}` |
| 国际版 inactive | 文案含 `未开启|未开放|已过期|inactive|not enabled|not available` → `{"ok":false,"inactive":true}`，**不写日志** |

- 未授权判定 `is_unauthorized`：`code ∈ {401,403}`，或 message 小写包含 `unauthorized|401|登录|失效|过期|token`；命中且存在 refresh_token → 刷新一次后重试一次；国际版先做 `skips_refresh_before_retry` 短路（避免把业务「已过期」当鉴权失败而白刷 token 并写 `needs_relogin`）。
- 路径回落：`variant.billing_paths(...)`，**只有 404** 才换下一个候选；401/403/10085/`code=-1` 原样返回。
- 单账号流程 `checkin_account`：单账号重入锁（`CHECKIN_ACCOUNTS_RUNNING`，重复 → "该账号正在签到，请稍后再试"）→ 国际版短路 `{"result":"skipped","reason":"unsupported_variant"}` → `ensure_fresh_token` → 查状态 → 已签 `{"result":"already"}` → 否则提交并写日志。
- 签到日志条目（`auto_checkin_logs.json`）：`{ts, accountId, email(展示名), variant, result: "success"|"already"|"error", error?}`；保留 `CHECKIN_LOG_KEEP_DAYS=30` 天、`CHECKIN_LOG_MAX_RECORDS=500` 条，写入前统一 `normalize_checkin_logs` 归一化（补齐 variant、丢弃非法项）。
- 调度常量：`CHECKIN_RECOVERY_INTERVAL=30min`、`FAST_RETRY_INTERVAL=5min`、`FAST_RETRY_MAX=12`、`MIN_CYCLE_DELAY=60s`；`CheckinCycleMode` 区分周期 / 手动。
- 时间窗（`checkin_start` / `checkin_end`，本地时区）：窗口生效时每个账号每天按「本地日期 + 账号 id」**确定性抽一个目标分钟**（`target_minute`），到点未办才请求；窗口未设置时不受限。`run_checkin_all(variant, respect_window)`：账号页「刷新并签到」传 `true`，窗口外整轮返回 `skipped / outside_checkin_window`，不查状态、不提交、不写日志；设置页 / 托盘 / 单账号传 `false`。
- 批量行结果：`{accountId, name, result: success|already|error|skipped|inactive, error?, reason?}`；托盘文案：`签到完成：成功 2，已签 1，失败 3`、`签到完成：成功 1，已签 0，失败 0，未开放 1`、`没有可签到的账号`、`签到任务正在进行，请稍后再试`。
- 签到配置（`auto_checkin_config.json`）默认值：`{"enabled":false,"excluded_account_ids":[],"checkin_start":"","checkin_end":"","start_hour":6,"end_hour":12,"keepalive_days":0,"lazy_refresh_hours":24}`；已有安装（配置文件或日志存在）但缺 `enabled` 时**沿用历史默认 true**，全新安装默认 false。`parse_clock` 接受 1–2 位时/分（`"9:5"`），越界（24:00 / 23:60）返回 None；保存时合法值补零成 `HH:MM`、非法归一为空串。

### 5.2 积分（`credits.rs`）

常量：

```
USER_RESOURCE_SUFFIX        = "/get-user-resource"                 // 旧接口（兼容回退）
WORKBUDDY_WEB_ENDPOINT      = "https://www.workbuddy.cn"
RESOURCE_SUMMARY_PATH       = "/billing/meter/get-user-resource-summary"
RESOURCE_PAID_PACKAGES_PATH = "/billing/meter/get-user-resource-paid-packages"
RESOURCE_FREE_PACKAGES_PATH = "/billing/meter/get-user-resource-free-packages"
ENTERPRISE_USAGE_PATH       = "/v2/billing/meter/get-enterprise-user-usage"
ENTERPRISE_ACCOUNTS_PATH    = "/console/accounts"
PRODUCT_CODE                = "p_tcaca"
EXPIRING_SOON_DAYS = 7 ; EXPIRY_CYCLE_OVERRIDE_DAYS = 365 ; FAR_FUTURE_EXPIRY_DAYS = 730
```

请求体（原样）：

```json
// paid
{"PageNumber":1,"PageSize":200,"Status":[0,3],"PackageCodes":["TCACA_code_002_AkiJS3ZHF5","TCACA_code_023_4xbGhMrE6q","TCACA_code_026_BaESVICNoi","TCACA_code_027_0FCGVA6vSa","TCACA_code_009_0XmEQc2xOf","TCACA_code_038_OhvqZtiPKr","TCACA_code_003_FAnt7lcmRT","TCACA_code_036_lupO5WgNdG"],"NeedRenewInfo":true}
// free（时间窗 = 本地当天 00:00:00 ~ 23:59:59，格式 "%Y-%m-%d %H:%M:%S"）
{"PageNumber":1,"PageSize":200,"Status":[0,3],"SlicePeriodStartTime":"...","SlicePeriodEndTime":"...","PackageCodes":["TCACA_code_008_cfWoLwvjU4","TCACA_code_007_nzdH5h4Nl0","TCACA_code_028_NtpWi0jzXs","TCACA_code_029_6wCGEWquYy","TCACA_code_030_BjSt89qTvr","TCACA_code_001_PqouKr6QWV","TCACA_code_006_DbXS0lrypC","TCACA_code_035_ArVxJcGDsm","TCACA_code_037_WxOD3MpI2o","TCACA_code_039_KRcQj7wUat","TCACA_code_040_mi9rCYg46x"]}
```

- 基址 `api_base_for(account)`：国际版固定 `https://www.workbuddy.ai`；国内版 `domain ∈ {workbuddy.cn, www.workbuddy.cn}` → `https://www.workbuddy.cn`，否则 `https://www.codebuddy.cn`（不允许账号数据拼任意主机）。
- 只有 404 才换候选（`post_with_fallback`）；401 时可能刷新 token 后重试一轮（`retry_new_response_if_unauthorized`）。
- 单资源摘要 `resource_summary(raw, now)` → `{packageCode, packageName, total, remaining, used, status, expireAt, expired, expiringSoon}`，取数键（按顺序，先本体再 `SlicePeriodUsageDetails[0]`）：
  - total：`CycleCapacitySizePrecise, CycleCapacitySize, CycleTotalCapacity, CapacitySizePrecise, CapacitySize, SlicePeriodCapacitySizePrecise, SlicePeriodCapacitySize`
  - remaining：`CycleCapacityRemainPrecise, CycleCapacityRemain, CycleRemainCapacity, CapacityRemainPrecise, CapacityRemain, SlicePeriodCapacityRemainPrecise, SlicePeriodCapacityRemain`
  - used：`CycleCapacityUsedPrecise, CycleCapacityUsed, CycleUsedCapacity, CapacityUsedPrecise, CapacityUsed, SlicePeriodCapacityUsedPrecise, SlicePeriodCapacityUsed`
  - 缺项互补：`total = remaining+used`、`remaining = total-used`、`used = total-remaining`，全部 `max(0)`
- 到期时间 `resolve_expire_at`：优先 `DeductionEndTime|deductionEndTime|ExpiredTime|expiredTime`；若比 `CycleEndTime|cycleEndTime` 晚超过 **365 天**（长期占位，如 2049）则改用 CycleEndTime；最终值距 now 超过 **730 天** 视为长期有效（`expireAt=null`）。`expiringSoon` = `0 < expireAt-now <= 7 天`。
- 企业账号（`is_enterprise_account`）走 `/v2/billing/meter/get-enterprise-user-usage`；缺 `enterpriseId` 时单次 `GET /console/accounts` 补拉。
- 错误结果 `credit_error_result{ok:false, accountId, error, ...}`。

### 5.3 本地积分统计（`credit_usage.rs`）

- 快照 `Snapshot{ts, account_id, account_name, total, remaining, variant}`，落 `credit_usage_snapshots.json`；保留 `CREDIT_SNAPSHOT_RETENTION_DAYS=90` 天、`CREDIT_SNAPSHOT_MAX_RECORDS=5000`、同账号 `CREDIT_SNAPSHOT_DEDUPE_WINDOW_MS=5min` 内去重。
- 消耗推导：仅当 `prev.remaining - cur.remaining > 0` 时记一条「观察到的消耗」；首次快照 / 余额上升只建新基线（不产生负数）。
- 事件：`UsageEvent{ts,date,account_id,variant,amount}`、`CheckinEvent{ts,date,account_id?,account_name,result,error?}`；`CREDIT_STATS_MAX_EVENTS=200`。
- `build_statistics(snapshots, checkins, accounts, at_ms)` 输出：`{generatedAt, coverageStartAt, rangeDays, accounts:[{accountId,accountName,variant,remaining,total,today,week,month,lastSnapshotAt,checkinToday}], daily, usageEvents, checkinSummary:{todaySuccess,todayAlready,todayFailed}}`。
- 统计窗口一律按**本地日期**换算（`local_date` / `local_date_string`），跨天以本地 00:00 为界；签到事件只取 `CHECKIN_LOG_KEEP_DAYS` 内、且不晚于 `at_ms`。

### 5.4 官方请求用量（`official_usage.rs`）

- URL：`https://www.workbuddy.cn/billing/meter/get-user-request-usage` / `https://www.workbuddy.ai/billing/meter/get-user-request-usage`（按账号档位）。
- 单次最多 `OFFICIAL_USAGE_PAGE_SIZE=3000` 条且超限时保留**区间内最早**一批（`total` 也被截断），因此**不能用 total 判断取完**：取满就把起点推进到本轮最新时间戳继续扫，直到某轮不满；`OFFICIAL_USAGE_MAX_ROUNDS=100`。
- 明细上限 `OFFICIAL_USAGE_DETAIL_LIMIT=1000`；窗口为最近 31 个自然日。
- 字段白名单（`take_object_fields` + `sanitize_models/daily/requests/accounts`）：只保留统计字段与有限请求摘要（时间、模型、账号名、请求 ID、积分等），**prompt / input 等正文永不复制**。
- 缓存：`official_usage_cache.json` + 进程内 `Mutex<Option<Value>>`；入口 `official_usage_for_statistics(accounts, at_ms, refresh)`，取数有 tokio 互斥锁防并发。

### 5.5 限额（`limits.rs` + `rate_limit_events.rs` + `rate_limit_hook.rs`）

- 唯一判据 `QUOTA_MARKERS = ["超出频率限制", "usage exceeds frequency limit"]`（`pub(crate)`，hook 侧复用）。
- 恢复时刻 `parse_reset_at(line)`：文案含 `"将在 "` 或 `"will reset at "`，直接解析原文时间（含原文声明时区），不自建窗口模型。
- 数据源：WorkBuddy / CLI 日志（`WINDOW_DAYS=2` 个最近日期目录）、两个 CodeBuddy IDE 日志（`Tencent-Cloud.coding-copilot` 目录、按 mtime 收窗）、hook payload；已接入 hook 的来源不再扫日志。
- 解析常量：`CLI_AUTH_MARKER="[AuthDoInitProbe]"`、`IDE_AUTH_MARKER="[PulseServiceLifecycle] Auth session changed:"`、`IDE_MODEL_FIELD="modelId="`、`IDE_AUTH_ERROR_MARKER="[handleAuthError]"`、`IDE_ACP_AGENT_TAG="AcpAgent:"`、`IDE_ACP_CONNECTION_TAG="AcpConnection:"`、`IDE_TS_FORMAT="%Y-%m-%d %H:%M:%S%.3f"`、`MERGE_WINDOW_MS=15000`、`MODEL_UNKNOWN=["auto","undefined","null"]`、`SCAN_MIN_INTERVAL_MS=5min`、`LONG_LINE_SNIFF_BYTES=4096`。
- 台账 payload（`build_payload`）：丢弃 `reset_at <= scanned_at`；按 `(accountId, model)` 取**最晚** resetAt；账号内按 resetAt 升序：

```json
{"scannedAt":<ms>,"windowDays":2,"accounts":[{"accountId":"...","limited":[{"model":"...","resetAt":<ms>,"firstSeenAt":<ms>,"hitCount":<n>}]}]}
```

- 状态文件 `rate_limit_state.json`：`State{offset: u64, lastEventAt?: i64, events:[StoredEntry]}`（camelCase），`StoredEntry{accountId, model?, resetAt, firstSeenAt, hitCount}`；事件文件超 `COMPACT_AFTER_BYTES=1MB` 压缩；`POLL_INTERVAL=1s`；`REQUEST_TIME_FLOOR_MS=1704067200000`（2024-01-01）以下的请求时间视为无效。
- hook 脚本（`script_body(events)`，全平台统一 `.sh`，事件路径安装时写死绝对路径；Windows 统一正斜杠）：

```sh
#!/bin/sh
payload=$(cat)
printf '%s\n' "$payload" >> '<events 绝对路径>'
printf '{}'
```

  注册命令 `hook_command = "bash '<MSYS 路径>'"`（Windows）/ `"sh '<路径>'"`（其它）；单引号按 POSIX 转义为 `'\''`。**最后必须输出 `{}`**，空 stdout 会被客户端当 hook 失败。
- 三处客户端配置：`~/.codebuddy/settings.json`、`~/.workbuddy/settings.json`、`~/.workbuddy-ai/settings.json`；安装幂等、写前备份、卸载尽量逐字节还原；`hookOptOut` 标记用户主动卸载（安装时清除）。
- 归因：`transcript_path` 前缀 → CLI（`~/.codebuddy/projects`）/ WorkBuddy（`~/.workbuddy` 或 `~/.workbuddy-ai`）；模型取**本次 payload** 的 `model`（不读 transcript 猜，避免延迟消费时错配）。
- 配置 `rate_limit_config.json` 默认 `{"enabled":true,"hookOptOut":false,"scanIdeLogs":true}`；`scanIdeLogs` 只影响两个 IDE 日志源。

### 5.6 Token 统计（`token_stats.rs`）

- 四路数据源（`get_statistics(days)`）：`workbuddy`（cn 数据根 `projects/`）、`workbuddy-ai`（ai 数据根 `projects/` + `sessions/`）、`codebuddy-cli`（`~/.codebuddy/projects`，**唯一开放请求明细**）、`codebuddy-ide`（`<local data>/CodeBuddyExtension/Data` 下 `index.json` 会话索引；遍历时跳过 `messages|check-point|backups|Public`）。
- 范围：`days ∈ {7,30,90}`（其它值 → 不限范围，`rangeDays=null`）；输出 `{generatedAt, rangeDays, sources:[...]}`，每 source 内含总量 / 模型 / 会话 / 项目 / 请求明细聚合。
- 用量字段别名（`usage_fields` 优先级）：`inputTokens|input_tokens`、`outputTokens|output_tokens`、`cacheReadInputTokens|cache_read_input_tokens`、`prompt_cache_write_tokens`，以及 `providerData.rawUsage.*` 覆盖层。

---

## 6 OAuth / 更新 / 通知 / 错误日志 / 旅行 / 进程 / 配置

### 6.1 OAuth 设备码登录（`oauth.rs`，复刻 cockpit 流程）

三步（全程无 PKCE，无 client_secret）：

| 步骤 | 请求 | 说明 |
|---|---|---|
| 1 申请 state | `POST {api_endpoint}{/v2/plugin}/auth/state?platform={workbuddy|workbuddy-ai}`，body `{}` | 响应取 `data.state`（空 → 报 "auth/state 响应缺少 state: <前 300 字符响应>"）；取授权页 `data.authUrl` / `auth_url` / `url`，都没有则拼 `{endpoint}/login?state={state}` |
| 2 轮询换 token | `GET {api_prefix}/auth/token?state={state}` | `code != 0/200` 或 `accessToken` 为空 → `{"done":false}` 继续轮询 |
| 3 拉账号 | `GET {api_prefix}/login/account?state={state}`，头 `Authorization: Bearer <access_token>` + `X-Domain` | 取 `uid / nickname / email / enterpriseName / enterpriseId` |

- 进程内状态表：`OAUTH_STATES: Mutex<HashMap<login_id, OAuthInfo>>`，`login_id = "wb_" + uuid(hex 无连字符)`；`OAuthInfo{variant, state, expires_at=now_secs()+600, done, result, error}`；`OAUTH_TIMEOUT_SECONDS=600`。
- 轮询必须用**发起时档位**（记录在 OAuthInfo），不能用当前列表档位。
- 档位一致性：响应 `domain` 与发起档位不符（`matches_domain`）→ 直接失败并写回：`"登录响应的 domain（<domain>）与<国内版|国际版>档位不符，已拒绝入库"`；account 响应里的 `domain` 同样校验。
- 成功后构造账号：`{id(uuid), uid, nickname, email, enterpriseName, enterpriseId, access_token, refresh_token, token_type(默认 Bearer), domain, variant, expiresAt, refreshExpiresAt, auth_raw=data, profile_raw=acc_data, createdAt}`；`expiresAt` 缺则 `now_ms + expiresIn*1000`，`refreshExpiresAt` 同理；落库走 `save_collected_account`（按 uid 合并），返回 `account_meta`。
- 错误：`loginId` 不存在 → `{"done":true,"error":"登录请求不存在"}`；超时 → `"登录超时"`；保存失败 → `"保存账号失败: <err>"`。
- 前端：`oauth-login-dialog.tsx` 显示授权链接/二维码 + 轮询 `oauth_status`。

### 6.2 更新检查（`update.rs` + `src-tauri/src/update_service.rs`）

- 常量：`GITHUB_OWNER="changexbc"`、`GITHUB_REPO="workbuddy-switch"`、`CACHE_TTL_SECS=6*3600`、`APP_VERSION=env!("CARGO_PKG_VERSION")`。
- 配置 `github_config.json`：`{owner, repo, proxy}`；保存时过滤空值、**丢弃 token 字段**（旧配置含 token 会被重写成不含 token 的形态）；旧仓库对 `changexbc/wb-switch` 自动迁移为 `workbuddy-switch`。
- 主端点（不计 GitHub API 配额）：依次尝试
  1. `https://github.com/{owner}/{repo}/releases/latest/download/latest.json`
  2. `.../latest-{os_slug}-{arch}.json`（`darwin→macos`）
  3. `.../latest-macos-{arch}.json`（非 macos 时追加，兼容旧 Windows 包）
  请求头 `Accept: application/json`、`User-Agent: wb-switch`。
- 兜底端点：`GET https://github.com/{owner}/{repo}/releases/latest`（`Accept: text/html`，**不跟随重定向**），读 302 的 `Location`，取 `/releases/tag/<tag>` 的最后一段；404 → `"未找到可用的发布版本"`；无 Location → `"无法获取发布页跳转地址（HTTP <status>）"`。
- 版本比较 `compare_versions`：去前缀 `v`、按 `.` 拆数字、缺失位补 0、逐段比较，返回 1/0/-1。
- 返回（成功）：`{ok:true, current, latest, latestTag, hasUpdate, releaseName, releaseUrl, publishedAt?, checkedAt}`；（失败）`{ok:false, error, message:"<msg>（code=<code>）", releaseUrl}`。`force=true` 绕过缓存（设置页手动检查）。
- 下载/安装走 tauri-plugin-updater（整包 + 签名校验），命令 `update_state / update_download / update_restart / relaunch_app`；托盘菜单文案：`检查更新 / 正在检查… / 升级到 v<版本> / 正在下载更新 <n>% / 重启以完成升级 / 更新失败，点击重试`。

### 6.3 通知存档（`notifications.rs`，`notifications.json`）

- 条目 `NotificationEntry{level, title, description?, at}`（camelCase）；`level ∈ success|error|warning|info`。
- 上限 `NOTIFICATION_LIMIT=100`（超出丢最旧）、`NOTIFICATION_VERSION=1`（版本不符报错并**保留原文件**）、标题截断 200 字符、描述截断 2000 字符（超出补 `…`）、`DEDUPE_WINDOW_MS=2000`（与上一条 level+title+description 全等且间隔 <2s 则丢弃）。
- 文件结构 `{version, items:[...]}`；读取时反转（新→旧）；写入失败不影响 toast。
- 命令：`record_notification / list_notifications / clear_notifications`；设置页「通知历史」展示并可清空。

### 6.4 错误日志（`error_log.rs`，`error.log`）

- 每行一个 JSON：`{ts, kind, message, detail, appVersion, os}`；`kind` 白名单 `frontend_crash | frontend_unhandled | backend`（其它值归 backend）。
- `ERROR_LOG_MAX_LINES=200`（超出则原子重写只留最后 200 行）、`ERROR_LOG_DETAIL_MAX_BYTES=4096`（按 UTF-8 字符边界截断）。
- 追加是「读-追加-必要时重写」，用进程内 `Mutex` 串行；锁中毒时取回内部守卫继续写（记日志不得 panic）。读失败/文件不存在 → 当空，不报错。
- 命令：`log_error(kind,message,detail) / get_error_log_path / reveal_error_log`。

### 6.5 派猫猫旅行（`travel.rs`，仅国内版）

- 前缀 `TRAVEL_API_PREFIX = "/activity/growth/buddy/travel"`，挂在 API 域名下（非 `/v2` 体系）：`GET {TRAVEL_API_PREFIX}/config`、`GET .../status`、`POST .../depart`（body `{"location_id": <value>}`）、`POST .../claim`（`record_id>0` 时 `{"record_id":N}`，否则 `{}`）。
- 头：见 §2.4（`x-client-platform: web`、`origin`、`referer` 均指向 `https://www.codebuddy.cn`）。
- 状态机值 `KNOWN_STATES = ["idle","traveling","arrived"]`（未知值返回 `"unknown travel state: <v>"`）。
- 缓存 `travel_cache.json` 结构：`{date: "YYYY-MM-DD", completed: bool, results: { <accountId>: {state, locationName, arriveAt, rewardCredit, claimed, claimedAt, ...} }}`；写入在 `with_travel_cache_lock` 内做「读盘 → 合并 → 落盘」。
- 合并规则 `merge_claim_state(prior, new)`：行进中（in_flight）保留上趟的 `locationName`、非 0 `rewardCredit`、`arriveAt`；已领取则强制 `claimed=true`、无新奖励时沿用旧 `rewardCredit/claimedAt`、补齐 `locationName`、并把 `state` 置回 `"idle"`。跨天时 `roll_cache_to_today` 重置。
- 周期：`TRAVEL_RETRY_INTERVAL=30min`（派发失败重试）、`TRAVEL_CLAIM_INTERVAL=15min`（领取）；`run_travel_cycle` / `run_travel_claim_cycle`；失败分类 `classify_depart_error` 决定是否可重试。
- 结果对象（派发）：`{accountId, uid, ok, already, skip?, state?, message, rewardCredit?}`；展示层 `display_record` / `display_label` 生成账号页标签。
- 配置 `auto_travel_config.json` 默认 `{"enabled":false}`（已有安装沿用历史默认 true）；命令 `get_travel_status / get_auto_travel_config / save_auto_travel_config`（开启时立刻跑一轮）。

### 6.6 进程管理（`process.rs`）

- 通用：`run_cmd_timeout(program, args, timeout_secs)`（并发读 stdout/stderr，超时杀进程），避免 Keychain/secret-tool 卡死调用线程。
- Windows 探测顺序（`windows_workbuddy_exe_path`）：运行进程 Path → 本地缓存（`workbuddy_exe.json`）→ 注册表（PowerShell 探测脚本）→ 环境变量 / 盘符扫描；全失败回落 `LOCALAPPDATA` 默认路径。进程枚举用 `tasklist /FO CSV` + CIM 脚本；映像名精确等价匹配（忽略 `.exe` 与大小写），排除 Crashpad helper 与自身映像。
- macOS：`ps` 行匹配（主名 + bundle 模式）→ `mdfind` → 候选目录；优雅退出用 bundle id（`com.tencent.workbuddy.mac` / `com.workbuddy.workbuddy-ai`），等待进程消失；启动用 `open -a` 并校验启动。
- Linux：按进程模式（`workbuddy` / `workbuddy-ai`）匹配；守护进程用 PID 等待。
- 关闭/启动超时：切换默认 20 秒；编辑器类（VS Code / JetBrains / CodeBuddy IDE）`VSCODE_CLOSE_TIMEOUT_SECS=60` / `CLOSE_TIMEOUT_SECS=60`；**超时只报错、绝不强杀**（VS Code 主进程把 ItemTable 缓存在内存，强杀会丢写入）。
- 文案常量：`RUNNING_MANUAL_HINT`（VS Code 运行中需手动退出）、`"检测到 VS Code 正在运行，请先完全退出后再同步会话。"`。

### 6.7 其它配置文件与默认值

| 文件 | 键与默认值 |
|---|---|
| `auto_checkin_config.json` | `enabled:false, excluded_account_ids:[], checkin_start:"", checkin_end:"", start_hour:6, end_hour:12, keepalive_days:0, lazy_refresh_hours:24` |
| `auto_travel_config.json` | `enabled:false` |
| `rate_limit_config.json` | `enabled:true, hookOptOut:false, scanIdeLogs:true` |
| `auto_rotate_config.json` | `enabled:false, check_interval_minutes:5, cooldown_minutes:120, min_gap_hours:24, min_urgency_hours:72, active_guard_minutes:30, min_remaining_credits:0` |
| `auto_rotate_notify.json` | 每日提示配额，`ROTATE_NOTIFY_DAILY_LIMIT=5` |
| `github_config.json` | `owner:"changexbc", repo:"workbuddy-switch", proxy:""` |
| `workbuddy_exe.json` | `{"cn":"<exe>","ai":"<exe>"}`（旧单键 `exe` 归 cn） |
| `codebuddy_cn_app.json` / `codebuddy_ide_app.json` | `{"exe":"<path>"}` |

- 自动轮换决策（`rotate::decide_target`，8 步顺序判定，任一步 Skip 即不切）：
  1. 过滤有效候选（查询成功、未过期、剩余 > 0），空 → Skip "没有可用账号（查询失败/已过期/无剩余积分）"；
  2. 按紧迫度排序（到期早优先，无到期时间排最后）取第一个为目标；
  3. 目标剩余时间 > `min_urgency_hours`（默认 72h）→ Skip "所有账号到期都还早（最紧迫的还剩 N 天），无需切换"；
  4. 当前账号已是目标 → Skip "当前账号已是最紧迫账号"；
  5. 冷却期（`cooldown_minutes`，默认 120）内 → Skip "处于切换冷却期"；
  6. **存活门控**：存在心跳新鲜的 CLI 会话 → Skip `SKIP_LIVE_SESSION_REASON="有 CodeBuddy CLI 会话在运行，暂不切换"`；
  7. 目标剩余积分 < `min_remaining_credits` → Skip "目标账号剩余积分不足（…），不值得切换"；
  8. 与当前账号到期差 < `min_gap_hours`（默认 24h）→ Skip "目标到期仅早 N 小时，未达切换阈值（M 小时）"；
  9. 否则 Switch(target)。
- 被门控拦下时，若「强制放开门控本会切」则发一次通知（每日配额 5 条）：`{"title":"workbuddy-switch","body":"「<账号名>」的积分 N 天后到期，但检测到有 CodeBuddy CLI 会话在运行；重启 CLI 后新账号才会生效。"}`。
- 轮换日志条目：`{ts, action: switched|skipped|error|noop, reason, from:{id,name}, to:{id,name,variant}, detail:[{name,remaining,soonestExpireAt,valid}]}`；保留 `ROTATE_LOG_MAX_RECORDS=200`。

---

## 7 各 IDE / CLI 登录态的读取与写入

### 7.1 CodeBuddy CLI（`codebuddy_cli.rs`）

**两种认证模式**

| 平台 | 模式 | 落地位置 |
|---|---|---|
| Windows | `settings-env` | `~/.codebuddy/settings.json` 的 `env.CODEBUDDY_AUTH_TOKEN`（+ 区域 env） |
| macOS / Linux | `api-key-helper` | `~/.codebuddy/settings.json` 的 `apiKeyHelper` 指向 `~/.codebuddy-rotate/helper.cjs` |

**常量**

```
ROTATE_DIR=".codebuddy-rotate"   STATE_FILE="state.json"
HELPER_FILE="helper.cjs"          LOGIC_FILE="helper.cjs"
LEGACY_HELPER_FILE="helper.sh"    LEGACY_WINDOWS_HELPER_FILE="helper.cmd"
SETTINGS_DIR=".codebuddy"         SETTINGS_FILE="settings.json"
SESSIONS_DIR_NAME="sessions"      LIVE_SESSION_STALE_MS=120_000
CODEBUDDY_AUTH_TOKEN="CODEBUDDY_AUTH_TOKEN"
CODEBUDDY_INTERNET_ENVIRONMENT="CODEBUDDY_INTERNET_ENVIRONMENT"
CODEBUDDY_BASE_URL="CODEBUDDY_BASE_URL"
CN_INTERNET_ENVIRONMENT="internal"    AI_INTERNET_ENVIRONMENT="public"
CN_CLI_ENDPOINT="https://copilot.tencent.com"
AI_CLI_ENDPOINT="https://www.codebuddy.ai"
AI_CLI_OPENAI_BASE_URL="https://www.codebuddy.ai/v2"
CLI_ENV_CACHE_KEY="CodeBuddy-Environment-Cache"
CLI_ENDPOINT_CACHE_KEY="CodeBuddy-Endpoint-Cache"
CLI_PRODUCT_CACHE_KEY="CodeBuddy-Product-Cache"
ACCOUNTS_SYNC_GRACE_MS=30_000
```

**state.json**（`~/.codebuddy-rotate/state.json`）：`{active: <index>, activeAccountId: "<uuid>", updatedAt: <ms>}`。

**helper.cjs 行为**（`scripts/codebuddy-cli-helper.cjs`，Node，被 CLI 当作 apiKeyHelper 调用）：

1. 读 `CODEBUDDY_ROTATE_DIR`（默认 `~/.codebuddy-rotate`）与 `WB_SWITCH_ACCOUNTS_FILE`（默认 `~/.wb-switch/accounts.json`）；
2. 读 accounts 数组，空 → stderr `wb-switch: no accounts available` + exit 1；
3. 读 state：优先 `activeAccountId` 按 `account.id` 查找；否则用旧字段 `active`（parseInt）取模；默认 0；
4. 取 `account.access_token`（必须是字符串），为空 → `wb-switch: selected account has no access token` + exit 1；
5. stdout 输出 `Bearer <token>` + 换行（**只这一行**，CLI 会把它当 Authorization 头）。

**切换顺序**（`switch_active_account(account_id)`）：

1. Windows：`ensure_no_process_env_override`（进程环境已有 `CODEBUDDY_AUTH_TOKEN` 时拒绝，避免"改了也不生效"）；
2. 非 Windows：`helper_migration_required` → 错误 "检测到旧版 CodeBuddy CLI helper；请先在账号页升级 CLI helper"；`!helper_is_configured` → 错误 "未检测到 CodeBuddy CLI apiKeyHelper；请先在 ~/.codebuddy/settings.json 配置轮换 helper"；
3. 校验账号存在（`account_index`），取档位，`ensure_region_env_compatible`；
4. Windows：**先在内存**算出完整 settings 值与 token（不落盘），避免"CLI 已关、账号没切"；
5. `create_dir_all(rotate_dir)` → 备份旧 state 文本；
6. **先关 CLI 进程，再写 state**（`close_then_write_state`，顺序被单测固定）；关闭消息区分是否切区域；
7. Windows：`commit_settings_env_update`（写 env + 校验回读），失败回滚 state 并报错；非 Windows：`helper_command` → `validate_helper_for_account`（实际执行 helper，比对输出是否等于 `Bearer <token>`）→ `persist_cli_region_env`；
8. 返回 `{ok, activeAccountId, activeAccountName, variant, regionChanged, closedCount, message}`；切换会中断当前 CLI 会话且不自动重开。

**status() 字段**：`configured, authMode("settings-env"|"api-key-helper"), environmentOverride, helperCurrent, migrationRequired, syncPending, syncInProgress, settingsPresent, helperPresent, helperSupportsAccountIds, activeIndex, activeAccountId, activeAccountName, activeAccountVariant, accountCount, statePath`。
`syncPending` 与 `syncInProgress` 互斥：账号库在 30 秒内被写过且 active 暂时解析不到 → 视为「保活刷新中」，不报脱节（否则每次启动都提示认证脱节）。

**存活门控** `has_live_session(now_ms)`：目录 `~/.codebuddy/sessions`，文件名必须匹配「纯数字 或 manual-*」+ `.json`，`lastHeartbeat` 满足 `now - hb <= 120_000`（未来时间也算活）；坏 JSON / 缺字段跳过；目录不存在或读不了 → false（并 eprintln 告警）。

### 7.2 VS Code CodeBuddy 扩展（`vscode_ext.rs`）

- 常量：`STATE_FILE="vscode_ext.json"`、`EXTENSION_ID="tencent-cloud.coding-copilot"`、`PAYLOAD_ID="Tencent-Cloud.coding-copilot"`、`VSCODE_CLOSE_TIMEOUT_SECS=60`。
- 数据目录：`%APPDATA%\Code`（Win）/ `~/Library/Application Support/Code`（mac）/ `$XDG_CONFIG_HOME/Code`（Linux）；库文件 `<data>/User/globalStorage/state.vscdb`。
- secret 行键：`secret://{"extensionId":"tencent-cloud.coding-copilot","key":"Tencent-Cloud.coding-copilot.new.accessToken"}`。
- 载荷（`build_ext_session_json(acc, existing)`，与既有值**合并**而非替换）：

```json
{"id":"Tencent-Cloud.coding-copilot","token":"<at>","refreshToken":"<rt>","expiresAt":<ms>,
 "domain":"www.codebuddy.cn","accessToken":"<uid>+<at>","converted":true,
 "account":{ ...官方 account 字段..., "lastLogin":true },
 "auth":{"accessToken":"<at>","refreshToken":"<rt>","tokenType":"Bearer","domain":"...","expiresAt":<ms>,
         "expiresIn":<ms>,"refreshExpiresIn":0,"refreshExpiresAt":<ms|0>,"lastRefreshTime":<ms>},
 "accounts":[ {...其他账号..., "lastLogin":false}, {...当前账号...} ]}
```

  - `domain` 经 `codebuddy_domain_for` 规范化（见 §1.2）；
  - `expiresIn` 刻意写**绝对时间戳**、`refreshExpiresIn=0`（与已验证的 CN IDE 实现保持一致，不要"修正"）；
  - `accounts[]` 按 `uid`（缺则 `id`）原位替换，未命中则追加；其余条目 `lastLogin=false`。

- 时序：`close_vscode_for_switch(restart)` → 等进程退出（优雅退出，超时只报错）→ 注入 → `relaunch_closed_editor`；`restart=false` 且 VS Code 运行中 → 直接拒绝。
- 检测 `detect_current_account`：读 secret → `parse_token_from_secret`（取「uid+token」形态的 uid）→ 在账号库按 uid / token 匹配 → 返回 `{ok, matched, account?: account_meta, rawUid}`。

### 7.3 CodeBuddy CN IDE / 国际版 IDE（`codebuddy_cn_ide.rs` / `codebuddy_ide.rs`）

| 项 | CN IDE | 国际版 IDE |
|---|---|---|
| 状态文件 | `codebuddy_cn_ide.json` | `codebuddy_ide.json` |
| macOS bundle | `com.tencent.codebuddycn` / `CodeBuddy CN.app` | `com.tencent.codebuddy` / `CodeBuddy.app` |
| secret key | `planning-genie.new.accessTokencn` | `planning-genie.new.accessToken` |
| Keychain 服务名 | `CodeBuddy CN Safe Storage` | `CodeBuddy Safe Storage` |
| Linux 密钥环应用名 | `CodeBuddy CN, codebuddy cn, codebuddy-cn, codebuddycn` | `CodeBuddy, codebuddy` |
| Linux 可执行名候选 | `buddycn, codebuddy-cn, codebuddycn`（另加 .desktop 解析 + which） | 按 cmdline / exe 名匹配 |

- `build_session_json(acc)` 载荷（CN IDE，国际版同构）：

```json
{"id":"Tencent-Cloud.genie-ide","token":"<at>","refreshToken":"<rt>","expiresAt":<ms>,
 "domain":"www.codebuddy.cn","accessToken":"<uid>+<at>","converted":true,
 "account":{"id":"<uid>","uid":"<uid>","label":"<nickname>","nickname":"<nickname>",
            "enterpriseId":"...","enterpriseName":"...","pluginEnabled":true,"lastLogin":true},
 "auth":{"accessToken":"<at>","refreshToken":"<rt>","tokenType":"Bearer","domain":"...",
         "expiresAt":<ms>,"expiresIn":<ms>,"refreshExpiresIn":0,"refreshExpiresAt":0,"lastRefreshTime":<ms>}}
```

- 本地状态：`{activeAccountId}`；接口 `status() / switch_account(id, restart) / validate_switch_target / inject_session_and_finish / detect_current_account`。
- 「首次使用前需先手动打开并登录一次」是产品要求（数据目录 / secret 行需要先由客户端创建）；VS Code 扩展侧已支持无 secret 行时按新会话载荷写入，IDE 侧仍要求 `state.vscdb` 已存在。

### 7.4 Safe Storage 加解密步骤（`vscode_cn_inject.rs`，三目标共用）

常量：`V10_PREFIX=b"v10"`、`V11_PREFIX=b"v11"`、`CBC_IV=[b' ';16]`、`SALT=b"saltysalt"`、`SECRET_EXTENSION_ID="tencent-cloud.coding-copilot"`；DB 值必须是 `{"type":"Buffer","data":[0..255,...]}` 的 JSON 字符串。

```text
VscodeSafeStorageTarget { data_dir_resolver, display_name,
  secret_item_prefix_extension_id, secret_key, macos_keychain_service, linux_secret_tool_app_names }
CODEBUDDY_CN_TARGET   = { data: codebuddy_cn_data_dir,   display: "CodeBuddy CN",
                          ext: "tencent-cloud.coding-copilot", key: "planning-genie.new.accessTokencn",
                          keychain: "CodeBuddy CN Safe Storage",
                          linux: ["CodeBuddy CN","codebuddy cn","codebuddy-cn","codebuddycn"] }
CODEBUDDY_INTL_TARGET = { key: "planning-genie.new.accessToken", keychain: "CodeBuddy Safe Storage",
                          linux: ["CodeBuddy","codebuddy"], ... }
```

**macOS**

1. `security find-generic-password -s "<macos_keychain_service>" -w`（带超时）取 Safe Storage 密码；
2. 派生 key：`pbkdf2_hmac::<Sha1>(password=上述密码, salt=b"saltysalt", iterations=1003)`，输出 16 字节；
3. 密文 = `"v10" || AES-128-CBC(PKCS7, key, IV=16 个空格)`；解密前校验前缀。

**Windows**

1. 读 `<data_root>/Local State` → `os_crypt.encrypted_key`（base64）；
2. base64 解码后前 5 字节必须是字面量 `DPAPI`，其余字节 `CryptUnprotectData`（DPAPI）解密 → 32 字节 key；
3. 密文 = `"v10" || nonce(12 随机) || AES-256-GCM(key, nonce, plaintext)`；解密要求长度 ≥ 31。

**Linux**

1. Secret Service：`OpenSession("plain","")` → `SearchItems({"application": <候选名>})` → 对**已解锁**条目 `Item.GetSecret(session)` 取明文 → 密码；
2. `v11`：用该密码派生 16 字节 key 后 AES-128-CBC（解密失败依次退回 `LINUX_EMPTY_KEY`）；
3. `v10`：使用固定 `LINUX_V10_KEY`（peanuts）→ 失败退回 `LINUX_EMPTY_KEY`；
4. 加密时优先沿用已有条目的前缀；无既有前缀且密钥环可用 → `v11`，否则 `v10`。

**注入流程**（`inject_secret_for(target, plaintext, user_data_dir)`）：

1. `resolve_state_db_path_for`：按 `<root>/User/globalStorage/state.vscdb` → `<root>/globalStorage/state.vscdb` → `<root>/state.vscdb` 顺序找已存在者，否则用第一个；
2. `data_root_from_db` = db 路径上溯 3 级；
3. 打开库 → `CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value TEXT)`；
4. 读旧行探测前缀（`detect_prefix`）→ 加密 → `{"type":"Buffer","data":[...]}` → `INSERT OR REPLACE INTO ItemTable(key,value)`；
5. **写后校验**：重新 SELECT，解析 JSON 且 `type=="Buffer"`，否则报 "写后校验失败：value 不是 Buffer"。
- 读取侧 `read_secret_for`：只读打开（`SQLITE_OPEN_READ_ONLY`，避免空库被创建）；`no such table` 视为无行；`decode_secret_storage_value` 对非 JSON 值原样返回（兼容历史明文）。

### 7.5 JetBrains IDE（`jetbrains.rs`）

- 常量：`STATE_FILE="jetbrains.json"`、`SECRET_KEY="Tencent-Cloud.coding-copilot.new.accessToken"`、`PLUGIN_DIR_PREFIX="coding-copilot"`、`SECRET_RELATIVE="options/secret-storage.xml"`、`PRODUCT_PREFIXES=["IntelliJIdea","IdeaIC","PyCharm","PyCharmCE"]`、`WINDOWS_IMAGE_NAMES=["idea64.exe","pycharm64.exe","idea.exe","pycharm.exe"]`、`CLOSE_TIMEOUT_SECS=60`。
- 插件把 secret 存在**应用级配置目录** `<config_root>/<产品>/options/secret-storage.xml`，格式：

```xml
<MapStorage><Scores>
  <Entry key="Tencent-Cloud.coding-copilot.new.accessToken" value="{...会话 JSON（XML 属性转义）...}" />
</Scores></MapStorage>
```

- 值就是 `vscode_ext::build_ext_session_json` 的产物（**无 secret:// 前缀**，键名与 VS Code 扩展完全同名）；写 XML 时对 `& < > " '` 做属性转义，读时反转义；解析以 `<Entry key=.. value=.. />` 为单位，未识别内容原样保留。
- 进程 ↔ 配置目录：由安装目录 `product-info.json` 的 `dataDirectoryName` 精确解析；映射不上的进程一律不动（不关闭、不重开）。
- 一次切换写入**所有**装了插件的受支持 IDE；时序同 VS Code（优雅退出 → 写 XML → 重开），超时只报错不强杀。
- 载荷与「检测当前账号」复用 VS Code 扩展的解析逻辑（`parse_token_from_secret`）。

### 7.6 Linux 密钥环直连（`linux_keyring.rs`）

- 目的：不依赖 `secret-tool`（libsecret-tools 默认未装，issue #80）。
- D-Bus：服务 `org.freedesktop.secrets`、路径 `/org/freedesktop/secrets`、接口 `org.freedesktop.Secret.Service` / `org.freedesktop.Secret.Item`：
  1. `OpenSession("plain","")` → 会话路径；
  2. `SearchItems({"application": <name>})` → `(unlocked[], locked[])`；
  3. 对每个 unlocked：`Item.GetSecret(session)` → `(session, parameters, value, content_type)`，`value` 按 UTF-8 转字符串。
- 属性名 `APPLICATION_ATTRIBUTE="application"`；只取**已解锁**条目（对锁定条目取值会拉起解锁弹窗并可能长时间阻塞）；整次查询 10 秒超时（后台线程 + channel）。

### 7.7 IDE / 扩展的会话复制

- 会话树布局：`<平台本地数据根>/CodeBuddyExtension/Data/<uid>/<client_dir>/<uid>/history/<工作区目录名>/`；`HISTORY_DIR="history"`；工作区目录名是**不透明字符串**（只枚举与按名复制，不重算 md5）。
- `SessionStoreSpec{app_dir, client_dir, backup_kind}`：VS Code 插件 = (`CodeBuddyExtension`, `VSCode`, `vscode-sessions`)；CodeBuddy IDE = (`CodeBuddyExtension`, `CodeBuddyIDE`, `codebuddy-ide-sessions`)。
- 复制项 `CopyItem{workspaceHash, conversationId}`（均 32 位小写 hex）。
- ID 策略 `CopyIdPolicy`：`AlwaysNew`（VS Code 插件：副本必须与源区分）/ `KeepUnlessConflict`（CodeBuddy IDE：默认沿用源 id，仅当目标工作区已存在同 id 时重随机并重写引用）。
- 合并：`index.json` 的会话索引合并、`messages/*` 重映射、跨文件 id 引用替换（`rewrite_conversation_id_references` / `remap_json_references`）、工作区索引 `merge_workspace_index`；写入用原子写。
- 运行态前提：IDE / 编辑器必须已关闭（运行中写入会被内存态覆盖）；`restart=false` 且正在运行 → 拒绝。

---

## 8 Tauri 命令清单（`lib.rs::generate_handler` 全量，共 96 条：`commands.rs` 93 + `companion.rs` 3）

| # | 命令 | 用途 |
|---|---|---|
| 1 | `get_status` | WorkBuddy 运行状态 + 当前登录账号 |
| 2 | `get_accounts` | 账号列表（`account_meta`，不含 token） |
| 3 | `get_codebuddy_cli_status` | CLI helper 轮换状态（不含 token） |
| 4 | `install_codebuddy_cli_helper` | 显式安装 / 升级 CLI helper（Windows 写 settings env） |
| 5 | `switch_codebuddy_cli_account` | 切换 CLI 默认账号（关 CLI → 写 state/settings → 校验） |
| 6 | `get_codebuddy_cn_ide_status` | CN IDE 安装 / 运行 / 当前账号 |
| 7 | `switch_codebuddy_cn_ide_account` | 切换 CN IDE 账号（可选复制会话） |
| 8 | `detect_codebuddy_cn_ide_account` | 读取本机 CN IDE 当前登录并匹配账号库 |
| 9 | `list_codebuddy_ide_sessions` | 列出当前 IDE 账号可复制的会话 |
| 10 | `codebuddy_ide_session_links_preview` | CN IDE 会话关联预览 |
| 11 | `get_vscode_ext_status` | VS Code 扩展安装 / 运行 / 当前账号 |
| 12 | `switch_vscode_ext_account` | 切换 VS Code 扩展账号（可自动关/重开 VS Code） |
| 13 | `detect_vscode_ext_account` | 读取本机扩展当前登录并匹配账号库 |
| 14 | `list_vscode_sessions` | 列出当前扩展账号可复制的会话 |
| 15 | `vscode_session_links_preview` | VS Code 会话关联预览 |
| 16 | `get_codebuddy_ide_status` | 国际版 IDE 安装 / 运行 / 当前账号 |
| 17 | `switch_codebuddy_ide_account` | 切换国际版 IDE 账号（可复制会话） |
| 18 | `list_codebuddy_intl_ide_sessions` | 列出国际版 IDE 可复制会话 |
| 19 | `codebuddy_intl_ide_session_links_preview` | 国际版 IDE 关联预览 |
| 20 | `detect_codebuddy_ide_account` | 读取国际版 IDE 当前登录并匹配 |
| 21 | `get_jetbrains_status` | JetBrains（IDEA/PyCharm）插件状态 |
| 22 | `switch_jetbrains_account` | 切换 JetBrains 插件账号（写所有受支持 IDE） |
| 23 | `detect_jetbrains_account` | 读取 JetBrains 当前登录并匹配 |
| 24 | `delete_account` | 删除账号（按 id） |
| 25 | `update_account_display` | 更新本地展示字段（备注 / 显示选择） |
| 26 | `oauth_start` | 发起 OAuth 扫码登录（`variant` 缺省 cn） |
| 27 | `oauth_status` | 轮询采集结果（档位取发起时记录） |
| 28 | `import_local` | 导入本机当前账号（从认证文件） |
| 29 | `export_accounts` | 按 id 列表导出完整记录（含 token） |
| 30 | `export_accounts_to_path` | 写入用户选择的绝对路径 |
| 31 | `preview_import_accounts` | 解析导入文件并返回脱敏预览 |
| 32 | `import_accounts` | 按索引导入，返回导入/跳过/覆盖计数 |
| 33 | `switch_account` | WorkBuddy 切换主命令（restart / shareSessions / copySessionIds / syncSelections） |
| 34 | `list_sessions` | 当前账号会话列表（`variant` 缺省 cn） |
| 35 | `list_account_sessions` | 指定账号的会话列表 |
| 36 | `copy_sessions` | 复制会话到目标账号 |
| 37 | `copy_sessions_cross` | 跨客户端复制会话 |
| 38 | `session_links_preview` | WorkBuddy 关联同步预览（发预览凭据） |
| 39 | `session_links_preview_cross` | 跨客户端关联预览 |
| 40 | `session_sync_cross` | 跨客户端会话同步 |
| 41 | `list_session_groups` | 关联组列表（按 client + 档位范围） |
| 42 | `get_session_group` | 关联组详情（成员 / 内容状态 / 可新增目标） |
| 43 | `preview_session_group_pair` | 组内某对成员的同步预览 |
| 44 | `sync_session_group_pair` | 组内一对成员同步 |
| 45 | `sync_session_group_unify` | 组级「统一到某成员」 |
| 46 | `sync_session_group_safe_batch` | 组级安全批量同步（只做 FastForward 类） |
| 47 | `add_session_group_member` | 新增关联成员（复制并登记） |
| 48 | `copy_linked_sessions` | 复制已关联会话（到新账号） |
| 49 | `vscode_restart_precheck` | VS Code 重启前置检查（运行中提示） |
| 50 | `unlink_session_group_member` | 解除关联成员 |
| 51 | `delete_session_group` | 删除关联组 |
| 52 | `open_permission_settings` | 打开系统授权面板（App 管理 / 完全磁盘访问） |
| 53 | `check_auth_permission` | 权限自检（写删探针文件） |
| 54 | `reveal_app_in_finder` | 在文件管理器中显示 App（便于拖拽授权） |
| 55 | `get_checkin_status` | 单账号签到状态（档位取账号自身） |
| 56 | `get_credit_expiry` | 单账号积分资源与到期时间 |
| 57 | `get_credit_statistics` | 本地快照 + 官方用量统计 |
| 58 | `get_token_statistics` | Token 统计（四源聚合） |
| 59 | `get_rate_limits` | 模型限额台账 |
| 60 | `get_rate_limit_hook_status` | hook 安装状态（脚本 + 三处配置逐项） |
| 61 | `install_rate_limit_hook` | 安装 hook（幂等、写前备份、清 optOut） |
| 62 | `uninstall_rate_limit_hook` | 卸载 hook（尽量逐字节还原） |
| 63 | `get_rate_limit_config` | 限额监听开关 |
| 64 | `save_rate_limit_config` | 保存限额监听开关 |
| 65 | `checkin` | 单账号立即签到 |
| 66 | `checkin_all` | 全部账号批量签到 |
| 67 | `get_auto_checkin_config` | 自动签到配置 |
| 68 | `save_auto_checkin_config` | 保存自动签到配置 |
| 69 | `get_checkin_logs` | 签到日志（带 variant） |
| 70 | `get_travel_status` | 单账号今日旅行状态 |
| 71 | `get_auto_travel_config` | 自动旅行配置 |
| 72 | `save_auto_travel_config` | 保存并立刻跑一轮派发/领取 |
| 73 | `refresh_account_token` | 单账号刷新 token |
| 74 | `get_auto_rotate_config` | 自动轮换配置 |
| 75 | `save_auto_rotate_config` | 保存自动轮换配置 |
| 76 | `rotate_status` | 轮换状态（配置 + 上次检查/切换 + CLI 当前账号） |
| 77 | `run_rotate` | 手动触发一次轮换检查 |
| 78 | `get_rotate_logs` | 最近轮换日志（新→旧） |
| 79 | `get_github_config` | 更新源配置 |
| 80 | `save_github_config` | 保存更新源配置 |
| 81 | `check_update` | 检查 GitHub Releases 新版本 |
| 82 | `update_state` | 更新状态快照（首屏 + 事件丢失兜底） |
| 83 | `update_download` | 启动更新包下载 |
| 84 | `update_restart` | 安装并重启 |
| 85 | `relaunch_app` | 启新进程并退出旧进程 |
| 86 | `get_launch_at_login_enabled` | 查询开机自启注册状态 |
| 87 | `set_launch_at_login_enabled` | 注册/移除开机自启并回读 |
| 88 | `record_notification` | 记录一条提示 |
| 89 | `list_notifications` | 读取最近通知（新在前，≤100） |
| 90 | `clear_notifications` | 清空通知存档 |
| 91 | `log_error` | 记录错误（kind 白名单） |
| 92 | `get_error_log_path` | 错误日志路径 |
| 93 | `reveal_error_log` | 定位日志（未生成时定位目录） |
| 94 | `companion::get_companion_enabled` | 悬浮窗开关状态 |
| 95 | `companion::set_companion_enabled` | 设置悬浮窗开关 |
| 96 | `companion::open_companion_settings` | 打开悬浮窗设置页 |

> 注：上表按 `generate_handler!` 的注册顺序编号（含 3 条 `companion::*`）；`commands.rs` 内共 93 条 `#[tauri::command]`。

---

## 9 前端页面与动作

### 9.1 账号页 `AccountsPage.tsx`（57 KB）

- 顶部档位 Tab：`国内版`(`cn`) / `国际版`(`ai`)。
- 「添加与迁移账号」区：**OAuth 扫码登录**、**从备份文件导入账号**、**导出账号备份**、**从本机登录态导入**。
- 账号卡片（`account-card.tsx`，52 KB）：展示名（按 `displayField`）、企业徽标、积分余额 + 到期高亮（7 天内）、签到状态、限额 chip（模型 + 恢复时刻）、需要重登提示。
- 卡片动作：**切换**（弹 `switch-account-dialog`：可选复制会话 / 归档同步）、**账号信息**（`account-info-dialog`：备注 ≤24 字、显示字段 昵称/手机号/备注）、**刷新积分**、**刷新 token**、**立即签到**、**检查签到状态**、**删除账号**。
- 各端接入区：**CodeBuddy CLI 接入**（显示 `~/.codebuddy/settings.json` 的 `env.CODEBUDDY_AUTH_TOKEN` 或 `~/.codebuddy-rotate/helper.cjs`；按钮「继续」「切换 CodeBuddy CLI」）、**CodeBuddy IDE**（国内/国际，切换 + 复制会话）、**VS Code CodeBuddy 插件**、**JetBrains**。
- 页内调用：`getAccounts / getRateLimits / getCheckinStatus / checkin / checkinAll / refreshAccountToken / getTravelStatus / getCodebuddyCliStatus / installCodebuddyCliHelper / switchCodebuddyCliAccount / getCodebuddyCnIdeStatus / getCodebuddyIdeStatus / getVscodeExtStatus / getJetbrainsStatus / detectCodebuddyCnIdeAccount / detectCodebuddyIdeAccount / detectVscodeExtAccount / detectJetbrainsAccount`。

### 9.2 积分统计页 `CreditStatsPage.tsx`（79 KB）

区块：**积分总览**（当前剩余 / 今日消耗 / 近 7 天 / 本月）、**趋势**（`趋势范围`）、**账号积分明细**（账号 / 当前剩余 / 今日消耗 / 签到）、**按模型分类**（`模型分类时间范围`）、**账号消耗构成**（`账号消耗构成范围`）、**官方请求用量**（模型 / 客户端 / 请求 ID / 观察到积分消耗）、**最近事件**、**档位筛选**。
口径文案：`签到与积分观察分开记录，签到不会计入消耗。`、`国内版与国际版积分体系不同，不会合并计算。`、`目前只有签到记录`、`首次成功采集积分资源后，趋势统计才会开始累计。`、`官方请求用量暂不可用；总览已回退到本地观察数据，请稍后刷新重试。`
调用：`getCreditStatistics`（+ 自动刷新 hook `use-credit-auto-refresh`）。

### 9.3 Token 统计页 `TokenStatsPage.tsx`（64 KB）

区块：**Token 总览**（`总览范围`、`按模型筛选`）、**趋势**（`左轴：Token · 右轴：调用次数`）、**Token 活动**（最近一年按天热力图、每日视图）、**用量分布**（维度选择；输入 / 输出 / 缓存命中率 = 缓存命中 ÷ 输入）、**消耗最高的会话**（按本地聚合 Token 排序）、**Token 消耗明细**（时间 / 模型 / 会话 / 项目 / 用量）、**请求明细**、**数据来源**说明、`正在扫描本地会话日志…` 加载态。

### 9.4 会话页 `SessionsPage.tsx`（58 KB）+ `session-group-detail.tsx`（33 KB）

- 顶部：`会话客户端`（WorkBuddy / CodeBuddy IDE 国内 / 国际 / VS Code）、`档位筛选`、`按状态筛选`、`排序方式`（最近更新 / 最早更新）、`刷新`、分页（`上一页` / `下一页`）。
- 组卡片：标题、项目、成员数 / 活跃成员数、`summaryText` 状态、账号名列表。
- 组详情：**会话关联图**（共同旧版 + 各自分支）、成员列表（标题 / 文章数 / 内容状态 / 版本关系 / 是否为可用来源）、**预览**、**同步**（选 mode）、**统一到某成员**、**安全批量同步**、**新增关联会话**（选择来源账号 → 来源会话树 → 目标账号 → 复制并关联）、**解除关联**、**删除关联组**、`更多操作`。
- 运行中拦截：`同步前需要重启 VS Code` / `正在关闭 VS Code 并处理，请勿关闭本窗口…` / `正在复制并关联…`。
- 调用：`getStatus / listSessionGroups / getSessionGroup / listAccountSessions / previewSessionGroupPair / syncSessionGroupPair / syncSessionGroupUnify / syncSessionGroupSafeBatch / addSessionGroupMember / copyLinkedSessions / unlinkSessionGroupMember / deleteSessionGroup / copySessionsCross / vscodeRestartPrecheck / getCodebuddyCnIdeStatus / getCodebuddyIdeStatus / getVscodeExtStatus`。

### 9.5 设置页 `SettingsPage.tsx`（82 KB）

| 分区 | 字段 / 开关 / 按钮 |
|---|---|
| 自动签到 | `启用自动签到`、时间窗（`checkin_start`/`checkin_end`）、`excluded_account_ids` 多选（`暂无可签到的账号`）、`lazy_refresh_hours`、`keepalive_days`、签到记录列表、`一键签到` |
| 自动旅行 | `启用自动旅行`（仅国内版可见） |
| CodeBuddy CLI 自动轮换 | `启用自动轮换`、`check_interval_minutes`、`cooldown_minutes`、`min_gap_hours`、`min_urgency_hours`、`min_remaining_credits`、轮换记录、`未接入 CodeBuddy CLI（请先到账号页安装 helper）` |
| 限额监听 | `启用限额监听`、`扫描 CodeBuddy IDE 日志`、`安装 hook` / `卸载 hook`、三处配置逐项状态 |
| 权限检测 | `打开完全磁盘访问`、`在 Finder 中显示`、`检测权限`；三步引导文案 |
| 自动更新 | 当前版本、`公开更新源`（owner/repo/proxy）、`检查更新`、`打开 GitHub Release`、下载进度、`重启以完成升级` |
| 启动设置 | `开机时静默启动到托盘` |
| Agent Companion | `启用会话悬浮窗`、`悬浮窗设置` |
| 通知历史 | 列表 + `清空通知历史`（确认框） |
| 错误日志 | `打开日志` / `定位日志` |
| 外观 | `主题`（系统 / 浅色 / 深色）、`显示关联会话菜单`、`切换账号时默认勾选归档同步` |
| 支持工具 | 5 个开关：WorkBuddy / CodeBuddy IDE / CodeBuddy CLI / VS Code CodeBuddy 插件 / JetBrains（默认关） |

调用：`getAutoCheckinConfig / saveAutoCheckinConfig / getCheckinLogs / checkinAll / getAutoTravelConfig / saveAutoTravelConfig / getAutoRotateConfig / saveAutoRotateConfig / getRotateStatus / runRotate / getRotateLogs / getRateLimitConfig / saveRateLimitConfig / getRateLimitHookStatus / installRateLimitHook / uninstallRateLimitHook / checkAuthPermission / openPermissionSettings / revealAppInFinder / checkUpdate / saveGithubConfig / setLaunchAtLoginEnabled / clearNotifications / listNotifications / revealErrorLog / openCompanionSettings / updateRestart`。

### 9.6 全局与托盘

- 侧栏路由：账号 / 关联会话 / 积分统计 / Token 统计 / 设置；左下角悬浮窗图标；更新入口。
- 托盘菜单项（`tray.rs`）：`打开主界面`、`一键签到` / `已签到`、`检查更新` / `正在检查…` / `升级到 v<ver>` / `正在下载更新 <n>%` / `重启以完成升级`、`轻量模式`（checkbox）、`显示 / 隐藏悬浮窗`、`悬浮窗设置`、`打开 GitHub`、`退出应用`；tooltip 会显示「正在签到…」「签到完成：成功 N，已签 M，失败 K，未开放 U」等。
- localStorage 键：`wb-switch.theme`、`wb-switch.compact`、`wb-switch.tools.<id>`（`"1"`/`"0"`）、`wb-switch.sync.default-archive`（`"1"` 为开）、导航偏好（`nav-prefs.ts`）。
- 对话框组件：`switch-account-dialog`、`codebuddy-ide-switch-account-dialog`、`vscode-switch-account-dialog`、`jetbrains-switch-dialog`、`oauth-login-dialog`、`export-accounts-dialog`、`import-accounts-dialog`、`account-info-dialog`、`update-install-dialog`、`session-group-detail`、`session-tree`、`session-copy-tab`、`session-sync-section`、`vscode-session-sync-section`、`companion-demo-dialog`、`app-error-boundary`。

---

## 10 README / docs 功能清单

README 功能表（原文要点）：

| 模块 | 说明 |
|---|---|
| 账号管理 | OAuth 扫码登录、导入导出账号、删除账号 |
| 账号切换 | 一键切换 WorkBuddy 登录账号，切换过程实时显示进度 |
| 会话复制 | 把当前账号勾选的会话复制给目标账号，源账号数据不受影响 |
| 关联会话 | 自动建立跨账号关联；按客户端查看副本状态，增量同步、分歧处理、新增/解除关联 |
| 积分到期查询 | 自动查询剩余量与到期时间；7 天内高亮、按紧迫排序、标注「建议优先使用」 |
| 积分统计 | 官方请求用量：总览、近 30 天趋势、模型分类、账号消耗、请求明细 |
| Token 统计 | 按来源查看总览与趋势、构成占比、活跃热力图、项目/模型 Top 10、会话排行 |
| CodeBuddy CLI | 复用账号库、默认账号独立、切换后立即生效 |
| CodeBuddy IDE | 桌面客户端切换 + 弹窗内勾选复制会话（首次需手动登录一次） |
| VS Code 插件 | 切换插件账号；VS Code 运行时可自动关闭并在写入后重开 |
| JetBrains 插件 | 一次切换写入所有装了插件的 IDE |
| 插件会话复制 | 切换插件账号时可复制会话（加法，源不变） |
| 自动轮换 | 后台把积分最紧迫账号设为 CLI 后续启动账号；CLI 会话运行时跳过 |
| 自动更新 | GitHub Releases + 签名校验整包更新 |
| 会话悬浮窗 | Agent Companion 悬浮栏（Codex / WorkBuddy / CodeBuddy / Codeg 状态） |
| 权限检测 | macOS 授权引导 + 自动检测 |

支持矩阵（README 表）：WorkBuddy 切号/复制/自动关闭重开/悬浮窗 ✅、自动轮换 —；CodeBuddy IDE 切号/复制/关闭重开/悬浮窗 ✅；CodeBuddy CLI 切号 ✅ 轮换 ✅；VS Code 插件 切号/复制/关闭重开/悬浮窗 ✅；JetBrains 切号/关闭重开 ✅（默认关闭入口）。

docs：`DEVELOPMENT.md`（环境/构建/发布流程、签名密钥 `~/.wb-switch/wb-switch-updater.key`、`gen-update-json.sh` / `merge-update-manifests.py`、目录结构）、`agent-companion-integration.md`（vendor 固定快照、`interfaceVersion` 兼容边界、demo overlay 构建模式 dev/build/copy/clean）。

---

## 11 Python 版落地清单（按依赖顺序）

**P0 基础设施**

1. `store_dir()`（`WB_SWITCH_HOME` 覆盖）、`atomic_write`（同目录 tmp + rename）、`now_ms/now_secs/utc_iso`（`%Y-%m-%dT%H-%M-%SZ`）、`norm_ts`（秒/毫秒/字符串）。
2. `http`：默认 UA = 桌面 Chrome UA、超时 30s、错误归一化（JSON 解析失败 → `{code: status, message: 前 500 字符}`，HTML → 取 `<title>`）、网络错误 → `{code:-1}`。
3. `is_route_missing`（仅 404）、`variant.billing_paths`（ai 先 `/billing/...` 再 `/v2/billing/...`）。

**P1 账号与登录态**

4. 账号库读写、`get_str/display_value/secret_value/is_envelope`、`account_meta`、`upsert_collected_account`（含信封保护）、`upsert_account`。
5. 认证文件读/写/备份 + 退出标记删除 + 写后校验；`import_from_auth_file` 的字段提取顺序。
6. `build_auth_headers` 与各业务附加头；`refresh_account_token`（含 `X-Auth-Refresh-Source: plugin`）。
7. 切换编排（§2.8 的顺序与拒绝文案），restart=false 的显式拒绝。

**P2 登录/迁移**

8. OAuth 三步 + 600s state + 域校验；CLI helper 的安装与切换（Node 脚本内容见 §7.1）；VS Code / IDE / JetBrains 的 Safe Storage 注入（§7.4）。

**P3 会话**

9. 会话三件套读写、`session_copy_supported_at` 探测、复制阶段状态机 + 操作日志 + 恢复；关联组表 / 基线 / 预览凭据；同步快照备份与阶段写入。

**P4 运营类**

10. 签到（仅 cn）+ 日志 + 调度窗口；积分三端点 + 企业分流 + 资源摘要；本地快照统计；官方用量分页扫描；限额 hook（脚本 + 三处 settings.json + state）；Token 统计四源；旅行；自动轮换；更新检查；通知；错误日志。

**容易踩的坑（务必照做）**

- 信封 token **不要**当明文字符串处理：写回必须原样，展示必须折叠 null，请求前必须 `envelope_token_error` 短路。
- 认证文件写完必须删 `<file>.logged-out`，删除失败要报错（否则"切了但没登录"）。
- 路径回落只在 404；401/403/10085 不要当作「路径不对」去重试。
- 刷新必须带 `X-Auth-Refresh-Source: plugin`（国际版否则 12153）。
- 会话副本的目标 UUID 必须先落盘再写正文；恢复复用 UUID。
- **不要**写 `edge_sync_mapping`。
- VS Code / IDE 运行中不要写入（关不干净就报错，别强杀）。
- Windows DPAPI 5 字节前缀校验；macOS 迭代次数 1003；Linux 只取已解锁条目。
- hook 脚本 stdout 必须回 `{}`；路径用单引号包裹并按 `'\''` 转义。
- 通知 2s 去抖、错误日志按 UTF-8 边界截断 4096 字节。
- 所有写盘都走原子写；所有「当前账号」状态文件（CLI 为 `~/.codebuddy-rotate/state.json`；其余为 `~/.wb-switch/codebuddy_cn_ide.json`、`codebuddy_ide.json`、`vscode_ext.json`、`jetbrains.json`）与账号库解耦，删除账号不影响它们，反之亦然。





