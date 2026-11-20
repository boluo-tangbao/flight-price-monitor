"""Desktop browser initialization adapted from dengmeiluan/ticket-monitoring.
Apache-2.0, commit 5cfa6723f072dffb603724a23b7a2e13cf82c5d1,
crawlers/base.py::STEALTH_JS and stealth_replacements.
Extracted without the upstream scheduler or challenge solvers. Local login
profiles retain their native browser defaults; this adapter is for cloud use.
"""
import json
import re
import zlib
from . import ctrip_stealth_ext

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

def desktop_profile(browser_version, viewport):
    """Use the actual launched kernel version; never launch a second browser."""
    match = re.fullmatch(r"(\d+)\.\d+\.\d+\.\d+", str(browser_version))
    if not match:
        raise ValueError("Could not determine cloud browser kernel version")
    major = int(match.group(1))
    return {
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               f"Chrome/{major}.0.0.0 Safari/537.36"),
        "viewport": dict(viewport), "locale": "zh-CN",
        "timezone": "Asia/Shanghai", "is_mobile": False,
        "has_touch": False, "proxy": "", "stealth": True,
        "seed": zlib.crc32(b"ctrip") % 997,
    }


def initialization_script(profile):
    script = STEALTH_JS
    for key, value in stealth_replacements(profile).items():
        script = script.replace(key, value)
    script += "\n" + ctrip_stealth_ext.extra_js(profile)
    if re.search(r"__[A-Z][A-Z0-9_]*__", script):
        raise ValueError("Incomplete cloud browser initialization")
    return script
