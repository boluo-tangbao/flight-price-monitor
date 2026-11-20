# 携程最小登录态上传

先关闭项目打开的 Edge 窗口，然后在项目目录、已安装依赖的 Conda 环境执行：

```powershell
python -m pip install -r requirements-session.txt
python scripts/upload_ctrip_session.py --browser-channel msedge
```

工具使用 Git Credential Manager 已有的 GitHub 登录认证，权限不足或网络不可用时会明确失败；不要求把密码、Cookie 或令牌发到聊天。
它创建/验证 GitHub Environment `ctrip-cloud`，部署分支仅允许 main。已有环境若开放其他分支则拒绝上传，避免覆盖原安全设置。
之后读取 GitHub 环境公钥，打开本地项目专用 `user_data/ctrip-msedge` 获取状态；导出浏览器的 HTTP/HTTPS 流量指向本机不可用端口，
不导航到机票网站，不执行查价。只保留 ctrip.com 及其子域 Cookie、HTTPS origin 存储（包含需要时的 IndexedDB），不包含密码库/历史记录/其他网站数据。

状态只在内存中构造，通过 libsodium SealedBox 按 GitHub 公钥加密后 PUT 至环境 Secret `CTRIP_STORAGE_STATE_B64`，
随后只核对 Secret 元信息。没有明文状态文件、剪贴板内容、Cookie 日志、令牌参数或调试输出。
B64 是编码；加密由 GitHub 公钥接口和 Secret 存储提供，不把 Base64 说成加密。
默认不再设置人为的打包截止时间，携程 Cookie 的原始到期时间保持不变；服务器也可能提前撤销会话。
只在携程实际要求重新登录时更新，不必固定每七天上传。需要自行设定限制时可显式添加 `--ttl-days 7`。
新版加载器兼容已上传的旧 v1 包，取消其原来自动附加的七天限制，不必仅为此重新上传；云端须同步新版加载代码后才生效。
成功上传只证明 Secret 已保存，不证明携程云端登录有效。
当前云端 context 是临时的，浏览器运行中得到的续期状态不会自动回写 Secret，因此不承诺一次上传永久有效。

云端读取后从进程环境移除状态变量，使用非持久化浏览器 context，仅在内存加载登录态，关闭 context/browser 后销毁。
登录态不进入价格库、缓存或附件。Secret 只提供给查询步骤；手动验收工作流只允许 main，正式监控仍受启用变量控制。
工作流能够读取登录态，因此能修改获准运行代码的人仍可能窃取会话；环境分支约束不能替代保护 main 和审核代码。

上传完成后，请在 GitHub 的 Settings → Environments → ctrip-cloud → Environment secrets 中确认出现上述名称，
不要尝试读取或截图它的值。此后还需要将会话加载代码同步到 main，再手动触发 Ctrip session acceptance。
本工具不会自动触发带账号查询或开启定时监控。

## 查询前预热与验证页判断

往返采集会先访问携程机票首页、检查访问状态、等待 2.5 秒并再次检查，
然后等待首页可发送的 Cookie 中出现原版使用的 `cticket`、`_bfa`、`MKT_CKID` 任一名称（最多再等 12 秒）。
等待只检查当前页和浏览器状态，不刷新、不重新导航；等待中遇到验证立即停止。
就绪后关闭首页标签页，再在同一浏览器会话中新建往返结果标签页，与原版往返分支的标签页顺序一致。
Cookie 未就绪记 `error / warmup_missing_risk_cookie`，不记无票；存在 Cookie 只表示本地状态就绪，不证明登录或服务端放行。
这是一次首页预热加一次行程查询，不循环刷新、不重试搜索；首页遇到 HTTP 拒绝、明确登录要求或验证标记时，不继续查询。

普通的“验证码登录”“登录后查看我的订单”和安全帮助文字不单独构成风控证据。
日志区分 HTTP 拒绝、明确登录页与验证标记；记录阶段（homepage / homepage_ready / roundtrip）、HTTP 状态、域名和固定规则名称。
不输出 Cookie、页面正文或 URL 查询参数。验证标记只是程序检测证据，不能据此断言账户被封或 IP 被封。

离线回归覆盖首页加载后才出现验证、首页即拒绝时不读取正文、结果页拒绝不重试、正常登录链接不遮蔽有效报价等场景。
带登录态的云端验收仍需手动触发；即使 status=ok 且返回报价，baggage_verified=false 仍表示两程托运预算尚未验收。

## 云端浏览器初始化补齐（2026-11-19）

原版 `ticket-monitoring` 的往返解析器依赖其浏览器基类。之前独立接入时，云端携程走了保留
浏览器默认参数的本地兼容路径，未启用原版 `STEALTH_JS`、`stealth_ext` 与相应启动参数。
现在带登录态的云端临时 context 默认使用 `cloud_pc_browser_mode: upstream`：
加载原版桌面初始化与扩展，UA 主版本取实际启动的浏览器，沿用固定的 `ctrip` profile seed，
在任何采集页面创建前合并安装一次初始化脚本。无需新增 Python 依赖。

本地无 Secret 的登录与持久化档案维持原来的浏览器默认行为；不读取或搬迁本地账号资料。
云端仍只从 Secret 加载最小状态，不在磁盘落下明文会话、不缓存 profile，不回写 Secret。
因此这个适配器并不等同于原版长期复用的完整浏览器档案。
若要回退云端初始化，可在 crawler 配置中显式设 `cloud_pc_browser_mode: native`。
upstream 模式支持现有 Chromium 和 Chrome；Edge 使用 native，避免套入 Chrome 身份脚本。

验收 JSON 的 `attempts=1` 表示一次验收，`queries` 改为实际发起的往返页面导航次数，
首页即停止时为 0。`navigation` 记录首页/搜索计数和就绪布尔值；`browser` 仅记录模式、
内核主版本与脚本安装布尔值，不记录 Cookie、页面正文或状态包。
`account_used` 仍只表示提供了登录态，不证明站点认可会话。

离线浏览器检查使用合成状态/本地 data 页面、禁用外网，不读真实登录态：

```powershell
$env:RUN_OFFLINE_BROWSER_CHECK='1'
python -m unittest discover -s tests -p test_ctrip_browser_identity.py -q
```

补齐这些模块不能保证服务端放行。此次未接入原版自动解题、刷新重试或模拟器；
验证即停止的约束保持生效，定时启用开关和查询时间不变。完整源码对照见 [浏览器链路审计](UPSTREAM_BROWSER_AUDIT.md)。
