"""注册与登录：手机号 + 短信验证码，纯 HTTP 走完 OneID → Keycloak → 客户端凭证。

为什么要自己走这条链（而不是拉起客户端让用户手动登录）：登录之后拿到的凭证要直接
落进账号库，才能像别的账号一样切换、续期、签到。整条链路只用标准库，全程在本机：

    1  POST www.workbuddy.cn/v2/plugin/auth/state?platform=workbuddy   设备授权 state
    2  GET  /console/auth/login?state=...&domain=...                    → Keycloak 地址
    3  GET  <Keycloak 登录页>                                            HTML 里找 OneID 入口
    4  GET  <broker/oneid/login>                                          → OneID 授权地址(S1)
    5  GET  <OneID /v1/authorize>                                        建立 OneID 会话
    6  POST oauth2.account.tencent.com/v1/auth/sms/code/send              发短信 → state_token
    7  POST .../v1/auth/sms/code/verify                                   校验验证码
    8  GET  .../v1/auth/accounts?state_token=                             账号 + OneID code
    9  GET  /auth/realms/copilot/broker/oneid/endpoint?code=&state=S1     落 console 会话
   10  GET  /console/auth/login?...&state=...                            绑定设备 state
   11  GET  /v2/plugin/auth/token?state=...                              客户端凭证

坑位（照抄实测结论，改之前先读）：
* 第 8 步只能做一次——它消耗 state_token；
* 全程共用同一个 cookie jar（workbuddy.cn 与 account.tencent.com 的会话必须在一起）；
* 第 9 步的 state 是第 4 步的 OneID state（S1），不是 Keycloak 的；
* 短信验证码始终由腾讯下发给账号主人本人；本模块不破解验证码，只是在用户已经
  拿到验证码之后替他把协议跑完。接码平台属于用户自备的可选项。

接码平台（自备）：Settings 里的 sms 段，接口按「疾驰短信」那样一套常见实现
（token 放 fcToken 头，getPhone / getVerifyCode / releasePhone），换平台改配置即可。
"""
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from util import Prefs
from util import REALMS, CoreError, as_dict, jwt_claims, jwt_exp, jwt_uid, say

HOST = "https://www.workbuddy.cn"
OAUTH2 = "https://oauth2.account.tencent.com"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0")
ALLOWED_HOSTS = ("workbuddy.cn", "codebuddy.cn", "tencent.com")
MAX_HOPS = 12
TIMEOUT = 12
MOBILES = {"86": r"1[3-9][0-9]{9}", "852": r"[4-9][0-9]{7}", "853": r"6[0-9]{7}"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不给自动跳转，把 3xx 与 Location 原样交回调用方。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """返回 None 就是不生成新请求。"""
        return None


class Flow(object):
    """一次登录会话：一个 cookie jar 走完全程，中间态都记在这里。"""

    def __init__(self, proxy="", timeout=TIMEOUT):
        """建两个共享同一个 jar 的 opener：跟随跳转的、和看 Location 的。"""
        self.proxy = str(proxy or "")
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        # 显式给 ProxyHandler：不给的话 urllib 会去读 Windows 系统代理，登录链路
        # 走代理经常被上游 TLS/WAF 掐断（表现为 SSL EOF）。默认直连，只有用户
        # 自己配了代理才走代理。
        handlers = [urllib.request.ProxyHandler(
            {"http": self.proxy, "https": self.proxy} if self.proxy else {}),
            urllib.request.HTTPCookieProcessor(self.jar)]
        self.follow = urllib.request.build_opener(*handlers)
        self.raw = urllib.request.build_opener(*handlers, _NoRedirect())
        self.state = ""
        self.auth_url = ""
        self.kc_url = ""
        self.broker_url = ""
        self.oneid_state = ""
        self.sms_token = ""
        self.mobile = ""
        self.country = "86"
        self.created = time.time()
        self.credential = None

    # ---------- HTTP ----------

    def headers(self, extra=None, referer=None):
        """一整套浏览器头；调用方再补 Content-Type 之类。"""
        row = {
            "User-Agent": WEB_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": HOST,
            "Referer": referer or (HOST + "/"),
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Sec-Ch-Ua": '"Chromium";v="149", "Microsoft Edge";v="149", "Not)A;Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "X-Request-Id": "%032x" % int(time.time() * 1000000),
        }
        row.update(extra or {})
        return row

    def request(self, method, url, data=None, headers=None, raw=False, timeout=None):
        """发一个请求，返回 (状态码, 头, 文本)；网络错误抛 CoreError。"""
        body = None
        if data is not None:
            body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers or {},
                                     method=method.upper())
        opener = self.raw if raw else self.follow
        try:
            with opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            return exc.code, exc.headers, text
        except urllib.error.URLError as exc:
            raise CoreError("网络不可用：%s（%s）" % (exc.reason, urllib.parse.urlparse(url).netloc))
        except Exception as exc:
            raise CoreError("请求失败：%s" % exc)

    def open(self, url, referer="", accept="text/html,application/json,*/*"):
        """手动跟随跳转（只在本域白名单里跳），返回最后的 (状态, 头, 文本)。"""
        current, visited = url, []
        for _hop in range(MAX_HOPS):
            status, headers, text = self.request(
                "GET", current, headers=self.headers({"Accept": accept}, referer or current),
                raw=True)
            visited.append((current, status))
            location = headers.get("location") if headers else ""
            if status not in (301, 302, 303, 307, 308) or not location:
                return status, headers, text
            nxt = urllib.parse.urljoin(current, location)
            host = urllib.parse.urlparse(nxt).netloc
            if not any(host.endswith(item) for item in ALLOWED_HOSTS):
                return status, headers, text
            current = nxt
        return status, headers, text

    def json_in(self, text, what="上游"):
        """把响应文本读成 dict；顺带按 code / errCode 判成败。"""
        try:
            payload = json.loads(text or "{}")
        except ValueError:
            raise CoreError("%s返回的不是 JSON：%s" % (what, (text or "")[:120]))
        if not isinstance(payload, dict):
            raise CoreError("%s返回的结构不认识" % what)
        if payload.get("errCode"):
            raise CoreError(str(payload.get("errMessage") or payload.get("errCode")))
        return payload


def envelope(payload, what="上游"):
    """取接口的 data 段；code 非 0 直接抛业务错误。"""
    if payload.get("code") not in (0, None, "0"):
        raise CoreError("%s：%s" % (what, payload.get("msg") or payload.get("code")))
    data = payload.get("data")
    return as_dict(data)


class Signup(object):
    """手机号登录 / 注册的四个动作：开链、发码、校验、落库。"""

    # ---------- 第 1-5 步 ----------

    @classmethod
    def start(cls, proxy=""):
        """铺好 Keycloak → OneID 的授权链，返回可发短信的 Flow。"""
        flow = Flow(proxy)
        status, _headers, text = flow.request(
            "POST", HOST + "/v2/plugin/auth/state?platform=workbuddy", data={},
            headers=flow.headers({"Content-Type": "application/json",
                                  "X-Requested-With": "XMLHttpRequest"}))
        payload = flow.json_in(text, "设备授权")
        data = envelope(payload, "设备授权")
        flow.state = str(data.get("state") or "")
        flow.auth_url = str(data.get("authUrl") or "")
        if not flow.state:
            raise CoreError("上游没有返回设备授权 state（接口可能变了）")

        status, headers, _text = flow.request(
            "GET", HOST + "/console/auth/login?" + urllib.parse.urlencode(
                {"platform": "workbuddy", "state": flow.state,
                 "domain": urllib.parse.urlparse(HOST).netloc}),
            headers=flow.headers({"X-Domain": urllib.parse.urlparse(HOST).netloc}),
            raw=True)
        location = headers.get("location") if headers else ""
        if not location:
            raise CoreError("没有拿到 Keycloak 授权地址（HTTP %s）" % status)
        flow.kc_url = urllib.parse.urljoin(HOST, location)

        status, _headers, text = flow.request(
            "GET", flow.kc_url, headers=flow.headers({"Accept": "text/html,*/*"}))
        found = re.search(r'data-idp="oneid"[^>]*href="([^"]+)"', text)
        if not found:
            raise CoreError("登录页里没有 OneID 入口（上游可能改版了）")
        flow.broker_url = urllib.parse.urljoin(HOST, found.group(1).replace("&amp;", "&"))

        status, headers, _text = flow.request(
            "GET", flow.broker_url,
            headers=flow.headers({"Accept": "text/html,*/*"}, referer=flow.kc_url),
            raw=True)
        authorize = headers.get("location") if headers else ""
        if not authorize:
            raise CoreError("OneID 没有返回授权地址（HTTP %s）" % status)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize).query)
        flow.oneid_state = (query.get("state") or [""])[0]
        if not flow.oneid_state:
            raise CoreError("OneID 授权地址缺少 state")
        # 预访问一次，建立 OneID 侧会话（失败不致命，后面发码还会再校验）
        flow.request("GET", authorize, headers=flow.headers({"Accept": "text/html,*/*"}))
        return flow

    # ---------- 第 6 步 ----------

    @staticmethod
    def mobile(mobile, country="86"):
        """规范化手机号；不支持的区号直接拒绝。"""
        country = str(country or "86").lstrip("+").strip()
        if country not in MOBILES:
            raise CoreError("不支持的地区代码：%s（支持 86 / 852 / 853）" % country)
        digits = re.sub(r"[\s()-]", "", str(mobile or ""))
        if digits.startswith("+" + country):
            digits = digits[len(country) + 1:]
        elif digits.startswith(country) and len(digits) == len(country) + (
                11 if country == "86" else 8):
            digits = digits[len(country):]
        if not re.fullmatch(MOBILES[country], digits):
            raise CoreError("手机号格式不对（%s）" % country)
        return digits, country

    @classmethod
    def send_code(cls, flow, mobile, country="86"):
        """发短信验证码，返回 {expires_in, phone(masked)}。"""
        digits, country = cls.mobile(mobile, country)
        if flow.sms_token and flow.mobile and flow.mobile != digits:
            raise CoreError("当前会话正在等上一个号码的验证码，请先完成或重开")
        _status, _headers, text = flow.request(
            "POST", OAUTH2 + "/v1/auth/sms/code/send",
            data={"client_code": "codebuddy", "mobile": "+%s %s" % (country, digits),
                  "scopes": ["openid", "mobile", "profile"]},
            headers=flow.headers({"Content-Type": "application/json",
                                  "X-TOA-LANG": "zh-CN"}))
        payload = flow.json_in(text, "发送验证码")
        token = str(payload.get("state_token") or "")
        if not token:
            if payload.get("captcha") or payload.get("status") == "need_captcha":
                raise CoreError("这个号码短时间内请求次数过多，需要图形验证码；"
                                "换个号码或等一会儿再试")
            raise CoreError("上游没有返回 state_token（%s）"
                            % (payload.get("errMessage") or payload.get("msg") or "未知原因"))
        flow.sms_token, flow.mobile, flow.country = token, digits, country
        masked = digits[:3] + "****" + digits[-2:]
        return {"expires_in": int(payload.get("expires_in") or 60), "phone": masked}

    # ---------- 第 7-11 步 ----------

    @classmethod
    def verify(cls, flow, code, log=None):
        """校验验证码并换取凭证，返回可直接入账号库的一行。"""
        code = re.sub(r"\D", "", str(code or ""))
        if len(code) != 6:
            raise CoreError("请输入 6 位数字验证码")
        if not flow.sms_token:
            raise CoreError("还没发过验证码")
        if flow.credential:
            return flow.credential
        cls._verify_sms(flow, code)
        token = cls._exchange(flow)
        account = cls._fetch_account_info(flow, token)
        row = cls._row(token, account)
        flow.credential = row
        say(log, "登录成功：%s（%s）" % (row["nickname"], row["uid"][:8]))
        return row

    @staticmethod
    def _verify_sms(flow, code):
        """第 7 步：校验验证码，拿新的 state_token。"""
        _status, _headers, text = flow.request(
            "POST", OAUTH2 + "/v1/auth/sms/code/verify",
            data={"state_token": flow.sms_token, "code": code},
            headers=flow.headers({"Content-Type": "application/json",
                                  "X-TOA-LANG": "zh-CN"}))
        payload = flow.json_in(text, "校验验证码")
        token = str(payload.get("state_token") or "")
        if token:
            flow.sms_token = token
        return payload

    @classmethod
    def _exchange(cls, flow):
        """第 8-11 步：取 OneID 授权码，落 console 会话，换取客户端凭证。"""
        accounts, oneid_code = [], ""
        status, _headers, text = flow.request(
            "GET", OAUTH2 + "/v1/auth/accounts?" + urllib.parse.urlencode(
                {"state_token": flow.sms_token}),
            headers=flow.headers())
        try:
            payload = flow.json_in(text, "账号列表")
        except CoreError:
            payload = {}
        if not payload.get("errCode"):
            accounts = payload.get("accounts") or []
            oneid_code = str(payload.get("code") or "")
        if not oneid_code:
            account_id = ""
            for item in accounts:
                if isinstance(item, dict) and item.get("id"):
                    account_id = str(item["id"])
                    break
            _status, _headers, text = flow.request(
                "POST", OAUTH2 + "/v1/auth/select_account",
                data={"state_token": flow.sms_token,
                      "account_id": account_id or "codebuddy@"},
                headers=flow.headers({"Content-Type": "application/json"}))
            chosen = flow.json_in(text, "选择账号")
            oneid_code = str(chosen.get("code") or "")
        if not oneid_code:
            raise CoreError("没有拿到 OneID 授权码（验证码可能已被使用，请重来一次）")

        endpoint = (HOST + "/auth/realms/copilot/broker/oneid/endpoint?"
                    + urllib.parse.urlencode({"code": oneid_code, "state": flow.oneid_state}))
        status, _headers, text = flow.open(endpoint, referer=flow.kc_url)
        names = {cookie.name for cookie in flow.jar}
        if not ({"KEYCLOAK_SESSION", "KEYCLOAK_IDENTITY"} & names):
            raise CoreError("没能建立登录会话（上游拒绝了这次授权，HTTP %s）" % status)
        # 第 10 步：让 console 会话认领设备 state，否则下一步一直 11217
        flow.open(HOST + "/console/auth/login?" + urllib.parse.urlencode(
            {"platform": "workbuddy", "state": flow.state,
             "domain": urllib.parse.urlparse(HOST).netloc}), referer=HOST + "/")
        # 第 11 步：换凭证
        _status, _headers, text = flow.request(
            "GET", HOST + "/v2/plugin/auth/token?" + urllib.parse.urlencode(
                {"state": flow.state}),
            headers=flow.headers({"X-No-Authorization": "true"}))
        payload = flow.json_in(text, "换取凭证")
        data = envelope(payload, "换取凭证")
        if not data.get("accessToken"):
            raise CoreError("上游没有返回 accessToken")
        return data

    @staticmethod
    def _fetch_account_info(flow, token):
        """用刚拿到的 token 拉一次账号信息（uid / 昵称 / 手机号）。"""
        domain = str(token.get("domain") or urllib.parse.urlparse(HOST).netloc)
        _status, _headers, text = flow.request(
            "GET", HOST + "/v2/plugin/accounts",
            headers=flow.headers({
                "Authorization": "Bearer " + token["accessToken"], "X-Domain": domain,
                "X-No-User-Id": "true", "X-No-Enterprise-Id": "true",
                "X-No-Department-Info": "true"}))
        try:
            payload = json.loads(text or "{}")
        except ValueError:
            return {}
        data = as_dict(as_dict(payload).get("data"))
        rows = data.get("accounts") or []
        return rows[0] if rows else {}

    @classmethod
    def _row(cls, token, account):
        """拼成账号库的一行（与「收录」抓到的字段同构）。"""
        access = str(token.get("accessToken") or "")
        refresh = str(token.get("refreshToken") or "")
        uid = str(as_dict(account).get("uid") or jwt_uid(access) or "")
        if not uid:
            raise CoreError("凭证里没有 uid，无法入账号库")
        expires = jwt_exp(access) or (
            int(time.time()) + int(token.get("expiresIn") or 0))
        claims = jwt_claims(access)
        return {
            "uid": uid,
            "nickname": str(as_dict(account).get("nickname")
                            or claims.get("name") or uid[:8]),
            "uin": str(as_dict(account).get("uin") or ""),
            "phone": str(as_dict(account).get("phoneNumber") or ""),
            "domain": str(token.get("domain") or REALMS["cn"]["domain"]),
            "realm": "cn",
            "platform": "CLI",
            "product": "workbuddy",
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": expires,
            "refreshExpiresAt": int(time.time()) + int(token.get("refreshExpiresIn") or 0),
            "enterpriseId": str(as_dict(account).get("enterpriseId") or ""),
            "addedAt": time.time(),
            "source": "signup",
            "apiKey": False,
            "enabled": True,
            "tasksEnabled": True,
        }

    # ---------- 一条龙：接码平台自动跑 ----------

    @classmethod
    def auto_register(cls, log=None, proxy="", settings=None):
        """租号 → 发码 → 等码 → 校验 → 返回账号行；接码平台没配好会直接报错。"""
        box = SmsBox(settings)
        if not box.token:
            raise CoreError("还没配置接码平台：设置里的 sms.token 为空")
        phone = box.buy()
        say(log, "已租到号码：%s" % phone["masked"])
        try:
            flow = cls.start(proxy=proxy)
            sent = cls.send_code(flow, phone["phone"], phone.get("country", "86"))
            say(log, "验证码已发出（%s 内有效），等待接码…" % sent["expires_in"])
            code = box.wait(phone["phone"])
            if not code:
                raise CoreError("接码超时：平台没有收到验证码")
            say(log, "已收到验证码，正在换凭证…")
            return cls.verify(flow, code, log=log)
        finally:
            box.release(phone["phone"])

    # ---------- 官方页面接力（短信路径被图形验证码挡住时的兜底） ----------

    @staticmethod
    def official_start(proxy=""):
        """拿设备授权 state 与官方登录页地址（浏览器接力用）。"""
        return _Official.start(proxy)

    @staticmethod
    def official_poll(state, timeout=180, interval=2.5, proxy="", log=None):
        """轮询换凭证：用户在浏览器里登录完成后，这里收回 accessToken。"""
        return _Official.poll(state, timeout, interval, proxy, log)


class _Official(object):
    """官方页面接力：脚本只拿 state 与登录页地址，登录本身由用户在浏览器里完成。

    上游的 /v2/plugin/auth/token 不需要 cookie，所以登录完成后用一个零 cookie 的
    客户端轮询就能把凭证收回来——这也是短信路径被图形验证码挡住时唯一的出路。
    """

    @staticmethod
    def start(proxy=""):
        """申请设备授权 state，返回 {state, url}。"""
        flow = Flow(proxy)
        _status, _headers, text = flow.request(
            "POST", HOST + "/v2/plugin/auth/state?platform=workbuddy", data={},
            headers=flow.headers({"Content-Type": "application/json",
                                  "X-Requested-With": "XMLHttpRequest"}))
        data = envelope(flow.json_in(text, "设备授权"), "设备授权")
        state = str(data.get("state") or "")
        url = str(data.get("authUrl") or "")
        if not state or not url:
            raise CoreError("上游没有返回设备授权地址")
        return {"state": state, "url": url}

    @staticmethod
    def poll(state, timeout=180, interval=2.5, proxy="", log=None):
        """轮询换凭证；超时抛 CoreError。返回可直接入库的一行。"""
        flow = Flow(proxy)
        deadline = time.time() + timeout
        while time.time() < deadline:
            _status, _headers, text = flow.request(
                "GET", HOST + "/v2/plugin/auth/token?" + urllib.parse.urlencode(
                    {"state": state}),
                headers=flow.headers({"X-No-Authorization": "true",
                                      "Accept": "application/json, text/plain, */*"}))
            try:
                payload = json.loads(text or "{}")
            except ValueError:
                payload = {}
            data = as_dict(as_dict(payload).get("data"))
            if payload.get("code") == 0 and data.get("accessToken"):
                account = Signup._fetch_account_info(flow, data)
                row = Signup._row(data, account)
                row["source"] = "official"
                say(log, "浏览器登录成功：%s" % row["nickname"])
                return row
            time.sleep(interval)
        raise CoreError("等浏览器登录超时（%d 秒）；确认在打开的页面里登录完成后再试"
                        % timeout)


class SmsBox(object):
    """接码平台（疾驰短信那套接口）：租号 / 等码 / 释放号码。"""

    DEFAULT = {"base": "https://www.jichisms.com", "token": "", "engine": "a",
               "project_id": "139372", "project_name": "", "wait": 90}

    def __init__(self, settings=None):
        """从 settings 的 sms 段取配置（没配也给一份空默认值）。"""
        section = dict(self.DEFAULT)
        section.update(as_dict(settings if settings is not None else Prefs.sms()))
        self.base = str(section.get("base") or self.DEFAULT["base"]).rstrip("/")
        self.token = str(section.get("token") or "").strip()
        self.engine = str(section.get("engine") or "a").lower()
        self.project_id = str(section.get("project_id") or "")
        self.project_name = str(section.get("project_name") or "")
        self.wait_seconds = int(section.get("wait") or 90)

    def _call(self, path, data=None, method="POST"):
        """打平台的一个接口，返回解析好的 JSON。"""
        url = self.base + path
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body if method == "POST" else None, method=method,
            headers={"User-Agent": "WorkBuddySwitch/1.0", "fcToken": self.token,
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise CoreError("接码平台返回 HTTP %s" % exc.code)
        except Exception as exc:
            raise CoreError("接码平台不可用：%s" % exc)

    def buy(self):
        """租一个号，返回 {phone, masked, sid}。"""
        payload = {"project_id": self.project_id}
        if self.engine == "b":
            payload.update({"engine": "b", "project_name": self.project_name})
        result = self._call("/api/user/getPhone", payload)
        if result.get("code") != 1:
            raise CoreError("租号失败：%s" % (result.get("msg") or result.get("code")))
        data = result.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        phone = re.sub(r"\D", "", str(as_dict(data).get("phone") or ""))
        if not phone:
            raise CoreError("平台没有返回号码")
        return {"phone": phone, "masked": phone[:3] + "****" + phone[-2:],
                "sid": as_dict(data).get("sid") or ""}

    def code(self, phone):
        """读一次验证码；还没到就给空串。"""
        payload = {"project_id": self.project_id, "phone": phone}
        if self.engine == "b":
            payload.update({"engine": "b"})
        result = self._call("/api/user/getVerifyCode", payload)
        if result.get("code") != 1:
            return ""
        message = str(result.get("msg") or "")
        found = re.findall(r"\b(\d{6})\b", message) or re.findall(r"\b(\d{4,6})\b", message)
        return found[0] if found else ""

    def wait(self, phone, interval=3.0):
        """轮询等码，超时返回空串。"""
        deadline = time.time() + self.wait_seconds
        while time.time() < deadline:
            found = self.code(phone)
            if found:
                return found
            time.sleep(interval)
        return ""

    def release(self, phone):
        """还号（失败不影响主流程）。"""
        payload = {"project_id": self.project_id, "phone": phone}
        if self.engine == "b":
            payload.update({"engine": "b"})
        try:
            result = self._call("/api/user/releasePhone", payload)
            return result.get("code") == 1
        except Exception:
            return False
