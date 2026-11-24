# Extracted from dengmeiluan/ticket-monitoring, Apache-2.0.
# Pinned commit: 5cfa6723f072dffb603724a23b7a2e13cf82c5d1.
# Browser/session and international RT methods retained from upstream.
# Unrelated scheduling, notification, channel policy and diagnostics omitted.


import os, sys, re, json, random, time, logging, tempfile, threading, zlib
from contextlib import ExitStack, contextmanager
from typing import Optional
from playwright.sync_api import sync_playwright, BrowserContext, Page, Response
from .challenge import ChallengeContext, install_navigation_guard
from . import stealth_ext
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_PROFILE_LOCKS = {}

_PROFILE_LOCKS_GUARD = threading.Lock()

PATH_GATE_SLEEP_CAP_S = 15.0

CHANNEL_GATE = threading.BoundedSemaphore(2)

_PLATFORM_LOCKS = {}

_PLATFORM_LOCKS_GUARD = threading.Lock()

def _dedupe_cookies(cookies):
    """按 (name, domain, path) 去重，保留先出现的（Playwright 遇重复会拒收）。"""
    out, seen = [], set()
    for c in (cookies or []):
        try:
            key = (str(c.get("name") or ""), str(c.get("domain") or ""),
                   str(c.get("path") or "/"))
        except AttributeError:
            out.append(c)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out

class SessionBusyError(RuntimeError):
    """同一渠道资料目录正由登录或采集使用。"""

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

_KERNEL_MAJOR = [0]

_KERNEL_LOCK = threading.Lock()

def kernel_major() -> int:
    """本机 Playwright Chromium 的真实主版本（带磁盘缓存）。

    用途只有一个：让 UA 里的 Chrome/xx 与真实内核一致。首次调用会起一次
    无头浏览器读 version（约 1 秒），之后读 data/.kernel_major。
    失败返回 0（调用方退回原 UA，不阻塞采集）。
    """
    if _KERNEL_MAJOR[0]:
        return _KERNEL_MAJOR[0]
    with _KERNEL_LOCK:
        if _KERNEL_MAJOR[0]:
            return _KERNEL_MAJOR[0]
        cache = os.path.join("data", ".kernel_major")
        try:
            with open(cache, encoding="utf-8") as f:
                v = int((f.read() or "0").strip() or 0)
            if v > 0:
                _KERNEL_MAJOR[0] = v
                return v
        except (OSError, ValueError):
            pass
        v = 0
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True)
                v = int(str(b.version or "0").split(".")[0] or 0)
                b.close()
        except Exception:
            v = 0
        if v > 0:
            _KERNEL_MAJOR[0] = v
            try:
                os.makedirs("data", exist_ok=True)
                with open(cache, "w", encoding="utf-8") as f:
                    f.write(str(v))
            except OSError:
                pass
        return v

SERVICE_CLOSED_MARKS = ("停止提供", "已停止服务", "服务已下线", "业务调整",
                        "请前往", "请下载")

def scoped_id(pid: str, plat: str, fallback):
    """按**渠道**取身份参数（r1063 T58）：参数层有意见才采用，否则用配置值。

    为什么需要：UA / 移动 UA / TLS 指纹这些"伪装参数"只读 `self.config` 的全局
    值，界面上按渠道改就白改。优先级与 `slider._pval`、`Pacing._scoped` 一致：
    **参数层是默认来源，不是永远优先**。

    fail-soft：参数层出任何问题都退回 `fallback`，伪装参数读不到不该让采集挂掉。
    """
    try:
        from .core import strategy_params as _sp
        got = _sp.resolve(str(pid), plat=str(plat or ""))
        if str(getattr(got, "source", "default")) != "default":
            return got.value
    except Exception:
        pass
    return fallback

MOBILE_UA = MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/126.0.0.0 Mobile Safari/537.36")

STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(Navigator.prototype, 'webdriver',
        {get: () => undefined, configurable: true}); } catch (e) {}
  // oscpu 是 Firefox 独有；Chromium 两档都不该出现（默认就没有，
  // 这里显式清掉，负控断言钉住 'oscpu' in navigator === false）。
  try {
    if ('oscpu' in navigator) {
      try { delete Navigator.prototype.oscpu; } catch (e) {}
      try { delete navigator.oscpu; } catch (e) {}
    }
  } catch (e) {}
  // NetworkInformation：headless 下时有时无且不自洽。两档统一给一个
  // 由 profile seed 派生的自洽对象（同 profile 稳定，不随机）。
  try {
    const conn = {
      effectiveType: __CONN_EFFECTIVE__,
      type: __CONN_EFFECTIVE__,
      saveData: __CONN_SAVEDATA__,
      rtt: __CONN_RTT__,
      downlink: __CONN_DOWNLINK__,
      onchange: null
    };
    Object.defineProperty(Navigator.prototype, 'connection',
      {get: () => conn, configurable: true});
  } catch (e) {}
  try {
    if (__ADDCHROME__) {
      if (!window.chrome) { window.chrome = {runtime: {}}; }
    } else {
      try { delete window.chrome; } catch (e) {
        Object.defineProperty(window, 'chrome',
          {get: () => undefined, configurable: true});
      }
    }
  } catch (e) {}
  try { Object.defineProperty(navigator, 'languages',
        {get: () => ['zh-CN', 'zh'], configurable: true}); } catch (e) {}
  try { Object.defineProperty(navigator, 'vendor',
        {get: () => __VENDOR__, configurable: true}); } catch (e) {}
  try { Object.defineProperty(navigator, 'platform',
        {get: () => __PLAT__, configurable: true}); } catch (e) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency',
        {get: () => __CORES__, configurable: true}); } catch (e) {}
  try {
    if (__MEM__ > 0) {
      Object.defineProperty(navigator, 'deviceMemory',
        {get: () => __MEM__, configurable: true});
    } else {
      try { delete navigator.deviceMemory; } catch (e) {}
      try { delete Navigator.prototype.deviceMemory; } catch (e) {}
    }
  } catch (e) {}
  try { Object.defineProperty(navigator, 'maxTouchPoints',
        {get: () => __MAXTOUCH__, configurable: true}); } catch (e) {}

  // screen / DPR：headless 默认 800×600 会和声明的 viewport 打架。
  try {
    const screenProps = [
      ['width', __SW__], ['height', __SH__],
      ['availWidth', __AW__], ['availHeight', __AH__],
      ['colorDepth', 24], ['pixelDepth', 24]
    ];
    for (const [k, v] of screenProps) {
      try {
        Object.defineProperty(Screen.prototype, k,
          {get: () => v, configurable: true});
      } catch (e) {
        try {
          Object.defineProperty(window.screen, k,
            {get: () => v, configurable: true});
        } catch (e2) {}
      }
    }
    Object.defineProperty(window, 'devicePixelRatio',
      {get: () => __DPR__, configurable: true});
  } catch (e) {}

  // userAgentData：移动档是 Safari UA，不该出现 Chromium 专属对象。
  try {
    if (__UAD__) {
      const uad = __UAD__;
      if (!uad.getHighEntropyValues) {
        uad.getHighEntropyValues = () => Promise.resolve({
          brands: uad.brands, mobile: uad.mobile, platform: uad.platform,
          architecture: 'x86', bitness: '64', model: '',
          platformVersion: '10.0.0', uaFullVersion: uad.fullVersion,
          wow64: false
        });
      }
      Object.defineProperty(Navigator.prototype, 'userAgentData',
        {get: () => uad, configurable: true});
    } else {
      try { delete navigator.userAgentData; } catch (e) {}
      try { delete Navigator.prototype.userAgentData; } catch (e) {}
    }
  } catch (e) {}

  // plugins / mimeTypes：headless 默认空数组，真实 Chrome 有 PDF 插件。
  try {
    const PLUGINS = __PLUGINS__;
    const MIMES = __MIMES__;
    const mimeObjs = MIMES.map((m) => ({
      type: m.type, suffixes: m.suffixes, description: m.description,
      enabledPlugin: null
    }));
    const pluginObjs = PLUGINS.map((p) => {
      const po = {name: p.name, filename: p.filename,
                  description: p.description, length: p.mimes.length};
      const mine = p.mimes.map((idx) => mimeObjs[idx]);
      mine.forEach((m, i) => {
        po[i] = m;
        if (m) { m.enabledPlugin = po; }
      });
      po.item = (i) => po[i] || null;
      po.namedItem = (n) => mine.find((m) => m && m.type === n) || null;
      return po;
    });
    const pluginArray = {
      length: pluginObjs.length,
      item: (i) => pluginObjs[i] || null,
      namedItem: (n) => pluginObjs.find((p) => p.name === n) || null,
      refresh: () => {}
    };
    pluginObjs.forEach((p, i) => { pluginArray[i] = p; pluginArray[p.name] = p; });
    const mimeArray = {
      length: mimeObjs.length,
      item: (i) => mimeObjs[i] || null,
      namedItem: (n) => mimeObjs.find((m) => m.type === n) || null
    };
    mimeObjs.forEach((m, i) => { mimeArray[i] = m; mimeArray[m.type] = m; });
    Object.defineProperty(navigator, 'plugins',
      {get: () => pluginArray, configurable: true});
    Object.defineProperty(navigator, 'mimeTypes',
      {get: () => mimeArray, configurable: true});
  } catch (e) {}

  try {
    const q = navigator.permissions && navigator.permissions.query;
    if (q) {
      navigator.permissions.query = (p) =>
        (p && p.name === 'notifications')
          ? Promise.resolve({state: Notification.permission})
          : q.call(navigator.permissions, p);
    }
  } catch (e) {}

  // WebGL：把调试扩展里"真实显卡"换成一台普通办公机（UNMASKED_* 是
  // 最常被拿去做指纹的两个参数，headless 默认值（SwiftShader）几乎是
  // 自报家门）。
  const VENDOR = __GLV__;
  const RENDERER = __GLR__;
  const patchGL = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    if (!orig) return;
    proto.getParameter = function (p) {
      if (p === 37445) return VENDOR;    // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return RENDERER;  // UNMASKED_RENDERER_WEBGL
      return orig.apply(this, arguments);
    };
    const origExt = proto.getSupportedExtensions;
    if (origExt) {
      proto.getSupportedExtensions = function () {
        const list = origExt.apply(this, arguments) || [];
        if (list.indexOf('WEBGL_debug_renderer_info') < 0) {
          list.push('WEBGL_debug_renderer_info');
        }
        return list;
      };
    }
  };
  try { patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}

  // Canvas：确定性微扰（同一 profile 每次一致、不同 profile 不同）。
  // 只动 getImageData 的抽样像素（±1），肉眼不可见、不影响渲染，但让
  // "canvas 哈希"不再是跨会话稳定的唯一标识。用随机数反而更可疑：
  // 同一会话两次读数不一致，本身就是机器特征。
  try {
    const SEED = __SEED__;
    const origGet = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function () {
      const d = origGet.apply(this, arguments);
      try {
        const a = d.data;
        for (let i = 0; i < a.length; i += 997) {
          a[i] = (a[i] + ((SEED + i) % 3) - 1) & 255;
        }
      } catch (e) {}
      return d;
    };
  } catch (e) {}
})();
"""

_DESKTOP_GL = (
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce MX250 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
)

_PDF_MIMES = (
    {"type": "application/pdf", "suffixes": "pdf",
     "description": "Portable Document Format"},
    {"type": "text/pdf", "suffixes": "pdf",
     "description": "Portable Document Format"},
)

_DESKTOP_PLUGINS = (
    {"name": "PDF Viewer", "filename": "internal-pdf-viewer",
     "description": "Portable Document Format", "mimes": [0, 1]},
    {"name": "Chrome PDF Viewer", "filename": "internal-pdf-viewer",
     "description": "Portable Document Format", "mimes": [0, 1]},
    {"name": "Chromium PDF Viewer", "filename": "internal-pdf-viewer",
     "description": "Portable Document Format", "mimes": [0, 1]},
)

_MOBILE_PLUGINS = (
    {"name": "PDF Viewer", "filename": "internal-pdf-viewer",
     "description": "Portable Document Format", "mimes": [0, 1]},
)

def stealth_replacements(fp: dict) -> dict:
    """指纹占位符的唯一替换来源（测试可直接审计）。

    同一 profile 的 seed 固定 → GPU/尺寸/插件不变；移动/桌面两档
    的 vendor/platform/触摸/DPR/userAgentData 互斥，不能缝在一起。
    """
    mobile = bool((fp or {}).get("is_mobile"))
    vp = (fp or {}).get("viewport") or {}
    width = int(vp.get("width") or (390 if mobile else 1366))
    height = int(vp.get("height") or (844 if mobile else 900))
    seed = int((fp or {}).get("seed") or 0)
    if mobile:
        # Android Chrome（r1063 T25）：与 MOBILE_UA 同族。旧值是 iPhone/Safari
        # 那一套（Apple vendor / iPhone platform / Apple GPU / 无 chrome /
        # 无 userAgentData），与 Chromium 运行时的能力面直接对不上——
        # 换掉 UA 却留这套，等于把矛盾从"UA 与引擎"搬到"页面与头"。
        glv, glr = "Qualcomm", "Adreno (TM) 730"
        vendor, plat = "Google Inc.", "Linux armv8l"
        cores, mem, touch, dpr = 8, 8, 5, 2.625      # Pixel 8 口径
        plugins = ()
    else:
        glv, glr = _DESKTOP_GL[seed % len(_DESKTOP_GL)]
        vendor, plat = "Google Inc.", "Win32"
        cores, mem, touch, dpr = 8, 8, 0, 1
        plugins = _DESKTOP_PLUGINS
    # userAgentData：**两档都给**（两档都是 Chromium）。移动档 `mobile: true`
    # + `platform: "Android"`，与 MOBILE_UA 和 sec-ch-ua-* 三处自洽。
    uad = None
    m = re.search(r"Chrome/(\d+)", str((fp or {}).get("ua") or ""))
    major = m.group(1) if m else "126"
    uad = {
        "brands": [
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
            {"brand": "Not-A.Brand", "version": "99"},
        ],
        "mobile": bool(mobile),
        "platform": "Android" if mobile else "Windows",
        "fullVersion": "%s.0.0.0" % major,
    }
    return {
        "__ADDCHROME__": "true",      # 两档都是 Chromium：都该有 window.chrome
        "__VENDOR__": json.dumps(vendor),
        "__PLAT__": json.dumps(plat),
        "__CORES__": str(cores),
        "__MEM__": str(mem),
        "__MAXTOUCH__": str(touch),
        "__DPR__": str(dpr),
        "__SW__": str(width),
        "__SH__": str(height),
        "__AW__": str(width),
        "__AH__": str(height),
        "__UAD__": (json.dumps(uad, ensure_ascii=False)
                    if uad is not None else "null"),
        "__PLUGINS__": json.dumps(plugins, ensure_ascii=False),
        "__MIMES__": json.dumps(_PDF_MIMES, ensure_ascii=False),
        "__GLV__": json.dumps(glv),
        "__GLR__": json.dumps(glr),
        "__SEED__": str(seed),
        # NetworkInformation：rtt 50–100、downlink 8–12，按 seed 单射派生
        # （seed%51 与 seed//51 组合唯一），同 profile 稳定、不同 profile 不同。
        "__CONN_EFFECTIVE__": json.dumps("4g"),
        "__CONN_SAVEDATA__": "false",
        "__CONN_RTT__": str(50 + seed % 51),
        "__CONN_DOWNLINK__": "%.1f" % (8.0 + (seed // 51) * 0.2),
    }

class BaseCrawler:

    name: str = "base"

    use_mobile: bool = False

    login_mobile: bool = False

    profile_name: str = ""

    login_suspect: bool = False

    PATH_STATUS = ("ok", "empty", "login", "blocked", "timeout",
                   "error", "parse_error", "unsupported", "calendar",
                   "soft_reject",
                   # `paced` = **我方主动不发请求**（配额让位/节流跳过）。
                   # 它不是「被对方拒绝」，不该算进爬取率分母——否则
                   # 「主动让步」会被读成「渠道不行」（qunar 国际航线就是因为
                   # 让位被记成 soft_reject，成功率看起来只有 50%）。
                   "paced")

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.headless: bool = config.get("headless", True)
        # 用户主动探针可复用可见会话取明细；定时监控不弹窗。
        self.probe_mode: bool = False
        self.timeout_ms: int = int(config.get("timeout_seconds", 45)) * 1000
        self.delay_min: float = float(config.get("delay_min", 2))
        self.delay_max: float = float(config.get("delay_max", 5))
        self.debug: bool = bool(config.get("debug", False))
        self.debug_dir: str = config.get("debug_dir", "debug")
        # 资料根目录：与账本同一口径（config > TM_USER_DATA_DIR > user_data）。
        # 这一处是第 5 个写点（浏览器 profile / 登录态快照都落在这里），
        # 漏了它测试照样会把合成资料写进真 user_data。
        # 直接调用、不写兜底副本：`core.yield_stats` 不反向依赖 crawlers，
        # 没有循环导入风险；留一份同样的字面量只会让"唯一口径"名存实亡。
        from .core.yield_stats import user_data_root
        self.user_data_root = os.path.abspath(
            os.path.join(_REPO_ROOT, user_data_root(config)))
        # 可选本地代理（r319，如 Clash http://127.0.0.1:7897）：浏览器与
        # httpx 采集面统一走它，让出口 IP 可变（IP 级限流绕行）。注意
        # 国内站+境外出口=风控风险升档，建议国内直落节点或负载均衡组
        self.proxy: str = str(config.get("proxy", "") or "").strip()
        # 按渠道绑出口（6 区）：proxy_map 优先于全局 proxy。
        # 多网卡/Clash 多监听端口/策略组时，让不同渠道走不同出口，
        # 一家被限流不牵连另一家。空=回全局 proxy（空=直连）。
        _pmap = config.get("proxy_map") or {}
        if isinstance(_pmap, dict) and str(_pmap.get(self.name) or "").strip():
            self.proxy = str(_pmap[self.name]).strip()
        # 会话年龄看门狗：profile 太久没更新会被站点当"新设备"，超过
        # N 天时强制预热一次（0=关闭）
        try:
            self.session_max_days = max(0, int(
                config.get("session_max_days", 0) or 0))
        except (TypeError, ValueError):
            self.session_max_days = 0
        # 指纹一致性（默认开）：关掉它 = 保留 navigator.webdriver 等自动化标记
        self.stealth: bool = bool(config.get("stealth", True))
        # 本渠道累计「等」了多少秒（r1063 T30）：闸门睡眠 + 渠道内部限速/退避。
        # 它与抓取耗时分开——「在等」和「在抓」是两种问题，优化手段不同。
        self.waited_s: float = 0.0
        # 登录窗口默认沿用该 profile 自己的 PC/H5 身份；只有显式开启
        # login_desktop 才强制桌面（手机 profile 上滑块更难拖时的退路，
        # 代价是登录与采集指纹不一致）。
        self.login_desktop: bool = bool(config.get("login_desktop", False))
        # 挑战处置引导：`crawler.slider.auto` 开了就自动挂上滑块处理器。
        # 放在这里而不是 main/webui——只要采集器被创建就生效，不用每个入口
        # 各写一遍（之前就是因为没人注册，配置开了也不生效）。
        try:
            from .challenge import ensure_slider_handler
            ensure_slider_handler(
                config, user_data_dir=self.user_data_root,
                profile=str(self.profile_name or self.name),
                logger=getattr(self, "logger", None))
        except Exception:
            pass

    @property
    def user_data_dir(self) -> str:
        # 不同平台/登录槽位不同目录，避免互相污染
        path = os.path.join(self.user_data_root,
                            self.profile_name or self.name)
        os.makedirs(path, exist_ok=True)
        return path

    @property
    def session_state_path(self) -> str:
        return os.path.join(self.user_data_root,
                            self.profile_name or self.name,
                            "session-state.json")

    def _read_session_state(self) -> dict:
        try:
            with open(self.session_state_path, encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            self.logger.warning("[%s] 无法读取会话快照", self.name)
            return {}

    @property
    def session_age_days(self) -> float:
        """会话距上次保存的天数（无快照=0，视作待建立不算老化）。"""
        ts = 0.0
        try:
            ts = float(self.session_saved_at or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            return 0.0
        return max(0.0, (time.time() - ts) / 86400.0)

    @property
    def session_saved_at(self) -> float:
        """明确确认登录并保存的时刻；旧 profile 迁移不算重新登录。"""
        try:
            return float(self._read_session_state().get("saved_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _write_session_state(self, state: dict, saved_at: float):
        state = dict(state, saved_at=saved_at)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.user_data_dir,
                    prefix=".session-state-", suffix=".tmp", delete=False) as f:
                tmp = f.name
                json.dump(state, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.session_state_path)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _cookie_key(c: dict) -> tuple:
        return (c.get("name"), c.get("domain"), c.get("path", "/"))

    @classmethod
    def _restore_plan(cls, existing: list, saved: list,
                      now: float = 0.0) -> list:
        """选择要写回上下文的快照 Cookie（缺失或比档案更新）。

        旧逻辑只补「档案没有的键」：档案里同名 Cookie 一旦过期/变旧，
        新快照永远盖不上去，重开浏览器就像掉登录。这里按 expires
        比较，只让快照替换「已过期」或「到期更早」的同名 Cookie；
        档案里更新的持久 Cookie 不被旧快照覆盖。
        """
        now = now or time.time()
        have = {}
        for c in existing or []:
            if isinstance(c, dict) and c.get("name"):
                have[cls._cookie_key(c)] = c
        out = []
        for c in saved or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            cur = have.get(cls._cookie_key(c))
            if cur is None:
                out.append(c)
                continue
            try:
                se = float(c.get("expires", -1))
                ce = float(cur.get("expires", -1))
            except (TypeError, ValueError):
                continue
            if ce > 0 and ce <= now:
                out.append(c)
            elif se > 0 and ce > 0 and se > ce:
                out.append(c)
        return out

    def _state_has_auth(self, state: dict) -> bool:
        # 只认渠道声明的登录凭证；RISK_COOKIES 里的统计/风控 Cookie
        # 未登录也在场，拿它们当判据会把掉登录状态覆盖进快照。
        # 同名还不够：空值/已过期的持久 Cookie 是残留，不是登录证据
        # （2026-11-02 实测 QN1/cookie2 匿名就下发，曾导致登出态回写）。
        want = tuple(getattr(self, "AUTH_COOKIES", ()) or ())
        # 弹窗登录（Google/淘宝等第三方）的凭证未必落在主页 cookie 里，
        # 有的只写 localStorage。AUTH_STORAGE_KEYS 让渠道声明这部分键名，
        # 与 cookie 名单同时参与判定（匹配为子串，兼容带前缀的键）。
        want_ls = tuple(getattr(self, "AUTH_STORAGE_KEYS", ()) or ())
        if not want and not want_ls:
            return False
        now = time.time()
        for c in (state or {}).get("cookies") or []:
            if not isinstance(c, dict) or c.get("name") not in want:
                continue
            if not str(c.get("value") or "").strip():
                continue
            try:
                exp = float(c.get("expires", -1))
            except (TypeError, ValueError):
                continue
            if exp > 0 and exp <= now:
                continue
            return True
        if want_ls:
            for o in (state or {}).get("origins") or []:
                if not isinstance(o, dict):
                    continue
                for it in o.get("localStorage") or []:
                    if not isinstance(it, dict):
                        continue
                    _n = str(it.get("name") or "")
                    if not _n:
                        continue
                    if not any(w in _n for w in want_ls):
                        continue
                    if str(it.get("value") or "").strip():
                        return True
        return False

    def _collect_state(self, ctx) -> dict:
        """汇总**同一浏览器上下文里所有页面（含弹窗）**的 cookie 与
        localStorage，返回 storage_state 同构的字典。

        为什么不能只用 ctx.storage_state()：第三方登录（Google / 淘宝
        等）常在 window.open 出来的页面完成，凭证可能只落在弹窗页的
        localStorage；storage_state 的 origins 偶尔拿不到刚写入的条目，
        于是「人明明登进去了，判定仍说没登录」。这里逐页补一遍。
        """
        state = {}
        try:
            state = ctx.storage_state() or {}
        except Exception:
            state = {}
        cookies = state.get("cookies") or []
        try:
            # context 级 cookie 覆盖所有域（含弹窗页打开的域）
            cookies = ctx.cookies() or cookies
        except Exception:
            pass
        origins = {}
        for o in state.get("origins") or []:
            if not isinstance(o, dict):
                continue
            org = str(o.get("origin") or "")
            if not org:
                continue
            origins[org] = {
                str(x.get("name")): dict(x)
                for x in (o.get("localStorage") or []) if isinstance(x, dict)
            }
        try:
            pages = list(ctx.pages)
        except Exception:
            pages = []
        _JS_LS = ("() => { try { const o = {};"
                  " for (let i = 0; i < localStorage.length; i++) {"
                  "  const k = localStorage.key(i);"
                  "  if (k) o[k] = localStorage.getItem(k); }"
                  " return {origin: location.origin, ls: o}; }"
                  " catch (e) { return null; } }")
        for pg in pages:
            try:
                if pg.is_closed():
                    continue
                got = pg.evaluate(_JS_LS)
            except Exception:
                continue
            if not isinstance(got, dict):
                continue
            org = str(got.get("origin") or "")
            if not org:
                continue
            cur = origins.setdefault(org, {})
            for k, v in (got.get("ls") or {}).items():
                cur[str(k)] = {"name": str(k),
                               "value": "" if v is None else str(v)}
        return {"cookies": cookies,
                "origins": [{"origin": o,
                             "localStorage": list(v.values())}
                            for o, v in origins.items()]}

    def _auth_gap_hint(self) -> str:
        """判定失败时告诉用户「缺什么」，别只丢一句未检测到凭证。"""
        want = tuple(getattr(self, "AUTH_COOKIES", ()) or ())
        want_ls = tuple(getattr(self, "AUTH_STORAGE_KEYS", ()) or ())
        bits = []
        if want:
            bits.append("Cookie: " + " / ".join(want))
        if want_ls:
            bits.append("localStorage: " + " / ".join(want_ls))
        if not bits:
            return ("该渠道没有声明登录凭证名单（AUTH_COOKIES / "
                    "AUTH_STORAGE_KEYS 为空），无法判定登录态——"
                    "这是渠道配置问题，不是你没有登录")
        return ("未找到以下任一项：" + "；".join(bits)
                + "。若你是扫码/第三方弹窗登录，请确认登录已在弹窗内完成"
                  "并等页面跳回，再点「我已登录完成」")

    def _should_refresh_snapshot(self, save_session) -> bool:
        """采集态是否回写快照（沿用 saved_at，不冒充重新登录）。"""
        return bool(
            save_session is None
            and self.session_saved_at > 0
            and getattr(self, "AUTH_COOKIES", ()))

    def _session_state_allowed(self, state: dict, save_session) -> bool:
        """登录窗口/采集态的快照是否允许落盘。

        登录窗口的“我已登录完成”只是用户声明，不是登录证据；若快照里
        没有 AUTH_COOKIES（空值/过期不算），覆盖旧快照会把上一份可用
        会话弄丢。采集态同理：登出态不覆盖。
        migrate 是旧 profile 迁移，不要求认证。
        """
        if not state:
            return False
        if save_session in (None, "login"):
            return self._state_has_auth(state)
        return True

    def load_session_cookies(self, domains=None) -> list:
        """读取最新快照，保留 Cookie 的域、路径和期限，不缓存认证值。"""
        if not os.path.isfile(self.session_state_path) and os.path.isdir(
                os.path.join(self.user_data_root, self.name, "Default")):
            # 既有用户无需重登：首次 HTTP 采集导出 Chromium 已存会话。
            # 仅迁移快照，不设置新登录时间，也不代表渠道已接受会话。
            with self.browser(headless=True, save_session="migrate"):
                pass
        state = self._read_session_state()
        domains = [domains] if isinstance(domains, str) else domains
        allowed = [str(d).lower().lstrip(".") for d in domains or []]
        now = time.time()
        cookies = []
        for c in state.get("cookies") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            domain = str(c.get("domain") or "").lower().lstrip(".")
            if allowed and not any(domain == d or domain.endswith("." + d)
                                   for d in allowed):
                continue
            try:
                expiry = float(c.get("expires", -1))
            except (TypeError, ValueError):
                continue
            if expiry >= 0 and expiry <= now:
                continue
            cookies.append(dict(c))
        return cookies

    def _ua(self) -> str:
        # 按渠道求值（r1063 T58）：界面上按渠道换 UA 要真的生效
        _mob = scoped_id("identity_headers.mobile_ua", self.name,
                         self.config.get("mobile_user_agent"))
        _desk = scoped_id("identity_headers.ua", self.name,
                          self.config.get("user_agent"))
        ua = ((_mob or MOBILE_UA) if self.use_mobile else (_desk or DESKTOP_UA))
        if not self.stealth:
            return ua
        maj = kernel_major()
        if not maj:
            # 记账（r1063 T60）：UA 里的 Chrome 主版本与真实内核不同步，本身就是
            # 一条高熵矛盾特征（UA-CH / 特性检测任一条都能看出来）。
            try:
                from .core import degraded as _dg
                _dg.note("ua_kernel", str(getattr(self, "name", "") or ""),
                         "读不到内核主版本，UA 无法与真实内核对齐")
            except Exception:
                pass
            return ua
        # UA 里的 Chrome/126 与真实内核 151 不一致，本身就是一条高熵
        # 矛盾特征（UA-CH / 特性检测任一条都能看出来）。统一成真实主版本。
        if "Chrome/" in ua:
            import re as _re
            return _re.sub(r"Chrome/\d+(?:\.\d+)*",
                           "Chrome/%d.0.0.0" % maj, ua)
        return ua

    def _viewport(self) -> dict:
        if self.use_mobile:
            return {"width": 390, "height": 844}
        return {"width": 1366, "height": 900}

    def _geo(self) -> tuple:
        """(locale, timezone)：与出口 IP 的地理一致（可选，默认 zh-CN/上海）。

        为什么需要：挂着境外/异地出口，而页面报 `Asia/Shanghai` + `zh-CN`，
        「IP 在 A 地、时区在 B 地」是风控里最便宜的一条矛盾判定。仓库既有
        结论也是这个方向（国内站 + 境外出口 = 风控升档）。

        配置（默认不写 = 完全保持原行为）::

            crawler:
              geo:
                locale: zh-CN
                timezone: Asia/Shanghai
                # 也可按渠道覆盖：qunar: {locale: zh-CN, timezone: Asia/Shanghai}

        时区非法时退回 Asia/Shanghai（宁可少一次适配，也不要启动失败）。
        """
        cfg = self.config.get("geo") if isinstance(self.config, dict) else None
        cfg = cfg if isinstance(cfg, dict) else {}
        per = cfg.get(self.name) if isinstance(cfg.get(self.name), dict) else {}
        loc = str(per.get("locale") or cfg.get("locale") or "zh-CN").strip()
        tz = str(per.get("timezone") or cfg.get("timezone")
                 or "Asia/Shanghai").strip()
        if not loc:
            loc = "zh-CN"
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)          # 不合法会抛，交给下面兜底
        except Exception:
            tz = "Asia/Shanghai"
        return loc, tz

    def fingerprint(self) -> dict:
        """采集/预热/自检/验证窗口共用的浏览器身份参数（单一来源）。

        headless 与窗口位置属于"怎么显示"，不进入身份；UA、视口、移动
        标记、locale/timezone、出口代理与 canvas 种子必须一致——同一
        profile 在采集和人工验证里呈现两套身份，本身就会被风控当成
        会话被接管。

        locale/timezone 可由 ``crawler.geo`` 覆盖（默认不变），用于让
        「时区/语言」与出口 IP 的地理对上；**键集固定 9 个**，不新增。
        """
        loc, tz = self._geo()
        return {
            "ua": self._ua(),
            "viewport": self._viewport(),
            "locale": loc,
            "timezone": tz,
            "is_mobile": bool(self.use_mobile),
            "has_touch": bool(self.use_mobile),
            "proxy": self.proxy,
            "stealth": bool(self.stealth),
            "seed": zlib.crc32(
                (self.profile_name or self.name).encode("utf-8")) % 997,
        }

    def _profile_wait_s(self) -> float:
        """``crawler.profile_wait_s``：拿不到会话锁时最多等多久（默认 0=不等）。

        默认 0 保持现行语义（立刻抛 SessionBusyError 让位）。设成 15–30 秒
        的意义：登录窗口/探针只占锁几十秒，让采集**等一会儿**比丢一整轮更
        划算——历史上 SessionBusyError 占了失败样本的 38.9%，全是这种「抢
        不到就整轮放弃」。
        """
        try:
            v = float(self.config.get("profile_wait_s", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(120.0, v))

    @contextmanager
    def browser(self, headless: Optional[bool] = None,
                window_front: bool = False, window_idx: int = 0,
                save_session: Optional[str] = None):
        """启动持久化浏览器上下文。headless=None 时使用配置。"""
        cloud_state = getattr(self, "cloud_storage_state", None)
        profile = (self.user_data_dir if cloud_state is None else
                   os.path.join(self.user_data_root, self.profile_name or self.name))
        path = os.path.normcase(os.path.realpath(profile))
        with _PROFILE_LOCKS_GUARD:
            lock = _PROFILE_LOCKS.setdefault(path, threading.Lock())
        if not lock.acquire(blocking=False):
            wait_s = self._profile_wait_s()
            if wait_s <= 0 or not lock.acquire(timeout=wait_s):
                raise SessionBusyError(
                    "[%s] 会话正在登录或采集，请完成当前操作后重试" % self.name)
            self.logger.info("[%s] 会话被占用，等待 %.0fs 后取得（profile_wait_s）",
                             self.name, wait_s)
        try:
            with self._browser_context(headless, window_front, window_idx,
                                       save_session) as ctx:
                yield ctx
        finally:
            lock.release()

    def login_window_size(self) -> tuple:
        """登录窗口尺寸（宽, 高）——按端给默认值，可用配置覆盖。

        `web.login_window`（PC，默认 1280×900）与 `web.login_window_h5`
        （H5，默认 480×880）。H5 是手机页面，给手机比例窗口才点得到控件。
        任何非法配置都退回默认值——**这里抛异常等于弹不出窗**。
        """
        # r1063 T181：端从**槽位**来（login_surface），没有才退回采集口径。
        # 反例（现场）：CtripCrawler.use_mobile=True 是采集走 m 站的意思，
        # 但它的 **PC 登录槽位**必须开桌面尺寸窗口，否则 passport 页面被裁。
        _surf = str(getattr(self, "login_surface", "") or "").lower()
        if _surf in ("h5", "mobile", "m"):
            is_h5 = True
        elif _surf in ("pc", "desktop", "web"):
            is_h5 = False
        else:
            is_h5 = bool(getattr(self, "use_mobile", False))
        cfg = (self.config or {}).get("web") or {}
        default = (480, 880) if is_h5 else (1280, 900)
        key = "login_window_h5" if is_h5 else "login_window"
        v = cfg.get(key) or {}
        try:
            w = int(v.get("w") or default[0])
            h = int(v.get("h") or default[1])
        except (TypeError, ValueError, AttributeError):
            return default
        if w < 320 or h < 480:          # 太小的窗口人没法登录
            return default
        return (w, h)

    @contextmanager
    def _browser_context(self, headless, window_front, window_idx, save_session):
        hl = self.headless if headless is None else headless
        fp = self.fingerprint()
        cloud_state = getattr(self, "cloud_storage_state", None)
        cloud_profile = cloud_state is not None and getattr(self, "cloud_persistent_profile", False)
        if cloud_state is not None:
            save_session = None
        with sync_playwright() as p, ExitStack() as browser_cleanup:
            x, y = 40 + window_idx * 170, 24 + window_idx * 150
            launch_args = [f"--window-position={x},{y}"]
            if window_front:
                # r1063 T140：登录窗口尺寸可配，且按端给默认值
                # （PC 1280×900 够看表格；H5 480×880 是手机比例，点得到）
                _w, _h = self.login_window_size()
                try:
                    self.logger.info(
                        "[登录窗] %s 端=%s 尺寸=%dx%d",
                        self.name,
                        (getattr(self, "login_surface", "") or "（未标注）"),
                        _w, _h)
                except Exception:
                    pass
                launch_args += [f"--window-size={_w},{_h}",
                                f"--window-position={x},{y}"]
            # 旁路关闭（WebRTC 内网 IP 泄漏等）：不改页面行为，只掐泄漏面。
            # 与 STEALTH_JS 同一条开关，stealth=false 时零干预。
            launch_args += stealth_ext.launch_args(fp)
            # 可选启动参数（当前只有 channel）：单独走 `**_kw`；代理那行必须
            # 保持原字面量——test_r319_proxy 用源码钉住它，防止有人顺手把
            # 「出口统一」删掉（那是 IP 级限流的唯一开关）。
            _kw = {}
            # 可选：用系统真 Chrome（crawler.browser_channel: chrome）。
            # 默认空=沿用自带 Chromium，不改变任何既有行为。
            _chan = stealth_ext.channel(self.config)
            if _chan:
                _kw["channel"] = _chan
            if cloud_profile:
                profile = browser_cleanup.enter_context(tempfile.TemporaryDirectory(prefix="tongcheng-actions-"))
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=profile, headless=hl, viewport=fp["viewport"], locale=fp["locale"],
                    timezone_id=fp["timezone"], is_mobile=fp["is_mobile"], has_touch=fp["has_touch"],
                    **_kw,
                )
            elif cloud_state is not None:
                # The hosted runner can share the desktop lifecycle without
                # importing a persistent profile or saving account data to disk.
                browser = p.chromium.launch(headless=hl, **_kw)
                browser_cleanup.callback(browser.close)
                ctx = browser.new_context(storage_state=cloud_state,
                                          viewport=fp["viewport"], locale=fp["locale"],
                                          timezone_id=fp["timezone"],
                                          is_mobile=fp["is_mobile"], has_touch=fp["has_touch"])
            else:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=self.user_data_dir,
                    headless=hl,
                    user_agent=fp["ua"],
                    viewport=fp["viewport"],
                    locale=fp["locale"],
                    timezone_id=fp["timezone"],
                    is_mobile=fp["is_mobile"],
                    has_touch=fp["has_touch"],
                    # 可选本地代理（r319）：全部渠道采集出口统一走它
                    proxy=({"server": fp["proxy"]} if fp["proxy"] else None),
                    args=launch_args + [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ] + (["--disable-blink-features=AutomationControlled"]
                         if fp["stealth"] else []),
                    **_kw,
                )
            if cloud_state is None and fp["stealth"]:
                try:
                    # 种子按 profile 固定：同一档案跨重启指纹一致（像真人），
                    # 不同档案互不相同（不把多个身份缝成同一个）
                    _rep = stealth_replacements(fp)
                    _js = STEALTH_JS
                    for _k, _v in _rep.items():
                        _js = _js.replace(_k, _v)
                    # 补齐层（WebRTC/音频/设备枚举/chrome 子对象/外框尺寸）：
                    # **拼进同一个 init script**，不上第二个 add_init_script——
                    # 「stealth 开时恰好 1 个脚本」是既有测试钉住的契约。
                    _ext = stealth_ext.extra_js(fp)
                    if _ext:
                        _js = _js + "\n" + _ext
                    ctx.add_init_script(_js)
                except Exception as _ste:
                    # 记账（r1063 T61）：伪装脚本没注进去 = **整个 stealth 失效**，
                    # 页面依旧是自动化特征。以前这里只有 pass。
                    try:
                        from .core import degraded as _dg
                        _dg.note("stealth_init", str(self.name),
                                 "注入伪装脚本失败：%s" % str(_ste)[:80])
                    except Exception:
                        pass
            state = None
            try:
                if cloud_profile:
                    self._restore_cloud_profile(ctx)
                # Chromium 重开可能丢弃 expires=-1 的会话 Cookie；快照
                # 只补缺失/更旧的键，不覆盖档案里更新的持久 Cookie。
                if cloud_state is None and os.path.isfile(self.session_state_path):
                    saved = self.load_session_cookies()
                    if saved:
                        plan = self._restore_plan(ctx.cookies(), saved)
                        if plan:
                            try:
                                ctx.add_cookies(_dedupe_cookies(plan))
                            except Exception as _ce:
                                if "Multiple cookies exist" not in str(_ce):
                                    raise
                                _seen, _by_name = set(), []
                                for _c in _dedupe_cookies(plan):
                                    _n = str(_c.get("name") or "")
                                    if _n in _seen:
                                        continue
                                    _seen.add(_n)
                                    _by_name.append(_c)
                                ctx.add_cookies(_by_name)
                yield ctx
                # 采集成功后回写一次快照（沿用原 saved_at，不代表重新
                # 登录）：否则短命会话 Cookie 只停留在登录那一刻，进程
                # 重启后快照已过期，表现成「过一会又要登录」。
                _refresh = cloud_state is None and self._should_refresh_snapshot(save_session)
                try:
                    # 登录收尾必须走合并态：用户可能是在弹窗里完成的登录，
                    # 只取主页面 storage_state 会把弹窗里的凭证漏掉。
                    if save_session == "login":
                        state = self._collect_state(ctx)
                    else:
                        state = (ctx.storage_state()
                                 if (save_session or _refresh) else None)
                except Exception as e:
                    # 验证窗口关得太快时上下文可能已经关闭；若周期快照
                    # 已成功，可以安全忽略收尾失败。登录窗口没有任何快照
                    # 成功过时必须抛出——否则会把"没保存"报成"已保存"。
                    if save_session == "login" and not self._session_snap_ok:
                        raise
                    state = None
                    if save_session:
                        self.logger.warning("[%s] 会话收尾快照失败: %s",
                                            self.name, e)
            finally:
                # 被拒页面在关上下文前落盘：同一个采集线程、同一个 page，
                # 是 Playwright sync 唯一安全的截图时机
                try:
                    if cloud_state is None and self._scene_pending:
                        self.capture_scene(self._last_page, tag="block")
                except Exception:
                    pass
                ctx.close()
            # 保存、关闭、发布必须全部成功，调用方才能报告登录已保存。
            if state is not None:
                if not self._session_state_allowed(state, save_session):
                    if save_session == "login":
                        # 用户点了“我已登录完成”，但快照里没有登录凭证：
                        # 不能把登出态写成刚登录，也不覆盖旧快照。
                        raise RuntimeError(
                            "未检测到登录凭证（AUTH_COOKIES），未覆盖旧快照。"
                            + self._auth_gap_hint())
                    # 采集回写只在认证 Cookie 仍在场时发生：已掉登录的
                    # 状态不覆盖上一份可用快照。
                    state = None
                if state is not None:
                    saved_at = (time.time() if save_session == "login"
                                else self.session_saved_at)
                    self._write_session_state(state, saved_at)

    def new_page(self, ctx: BrowserContext) -> Page:
        page = ctx.new_page()
        page.set_default_timeout(self.timeout_ms)
        page.set_default_navigation_timeout(self.timeout_ms)
        self._last_page = page
        self._watch_retry_after(page)
        # 挑战处置接缝：导航完成后交给已注册的处理器（没注册=纯转发，
        # 行为与开销都不变）。处理器解完自行 reload，采集器照常解析。
        if getattr(self, "cloud_storage_state", None) is None:
            install_navigation_guard(page, lambda url: ChallengeContext(
                plat=self.name, url=url, crawler=self))
        return page

    retry_after_s: int = 0

    last_block_url: str = ""

    last_block_api: str = ""

    last_query: tuple = ()

    _scene_pending: str = ""

    last_soft_reject: dict = {}

    _soft_pending: bool = False

    _last_page = None

    last_scene: dict = {}

    warmup_url: str = ""

    warmup_done_at: float = 0.0

    RISK_COOKIES: tuple = ()

    AUTH_COOKIES: tuple = ()

    warmup_cookie_ok: bool = True

    warmup_cookies: list = []

    _session_snap_ok: bool = False

    last_busy: bool = False

    PARSE_SCENE_KEEP = 10

    PARSE_SCENE_MAX_BYTES = 400_000

    LOGIN_WALL_FILE = "login_wall.json"

    LOGIN_PROBE_S = 2 * 3600

    SERVICE_CLOSED_FILE = "service_closed.json"

    SERVICE_PROBE_S = 6 * 3600
