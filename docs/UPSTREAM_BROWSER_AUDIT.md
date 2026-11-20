# 原版浏览器链路审计与增量修复

核对日期：2026-11-19。上游固定快照：

- `dengmeiluan/ticket-monitoring`：`5cfa6723f072dffb603724a23b7a2e13cf82c5d1`。
- 最初移植解析器时的 ticket-monitoring 快照：`658ae9c9d50f8af656208fd972580752e349d844`。
- `zhanglong-ustc/flight-price-monitor`：`781cd2df18d0c83789fb70d56452ede4628706b0`。
- 本地修复前：`806cc0d0d5fb8b6c2d26bd0100eb01e777a2b0f5`，工作区干净。

## 核实到的遗漏

| 环节 | 原版源码行为 | 修复前的实际接线 | 本次处理 |
| --- | --- | --- | --- |
| 浏览器初始化 | `BaseCrawler._browser_context` 加启动参数，合并 `STEALTH_JS` 与 `stealth_ext.extra_js`，再创建采集页面 | 携程 PC 分支为了保留本地原生浏览器行为，移除 args 且不安装初始化；云端也走这条分支 | 仅带登录态的云端路径接回原版初始化与参数，本地登录兼容行为保留 |
| 运行时身份 | `_ua` 对齐内核主版本；`fingerprint` 统一 UA/视口/桌面标记/语言/时区与固定 seed | 云端保留 Chromium 默认 UA；没有原版完整初始化 | 从已经启动的浏览器读取主版本，不额外启动探测浏览器；桌面身份及固定 seed 供一个合并脚本使用 |
| 往返预热标签页 | `_fetch_rt` 在同一 context 开首页标签页、等待、关闭，再建结果页 | 最初没有预热，昨天只补了同页首页访问与 2.5 秒等待 | 恢复独立预热/结果标签页顺序，验证即停止 |
| 预热成效检查 | 通用 `warmup` / 日历路径用 `RISK_COOKIES` 和 `wait_risk_cookies` 检查状态；原版 `_fetch_rt` 自身仍只固定等待 | 无 Cookie 就绪证据 | 为本地往返适配器增加有界就绪检查；未就绪不发搜索，存在 Cookie 不冒充服务端认证 |
| 会话持久化 | 完整 profile + `session-state.json`；补缺失会话 Cookie；符合认证条件时更新快照 | 安全裁剪的 Cookie/origin/IndexedDB Secret + 临时 context；不回写 | 保留最小状态方案，明确与原版长期档案仍有差异；不扩大账号资料上传范围 |
| 验证处理 | `new_page` 安装 `challenge.install_navigation_guard`，按配置注册 SolverHandler | 没接自动处置，检测到验证即停 | 保持停止；未引入解题、刷新或新增查询 |

原版浏览器初始化和指纹层在最初的 `658ae9c...` 快照中已经存在，属于当时未移植的依赖，
不能把本次遗漏全部归为最近上游更新。

证据：[原版基类与初始化](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/crawlers/base.py#L1398)、
[最初快照的基类](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/crawlers/base.py#L228)、
[原版往返分支](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/crawlers/ctrip.py#L857)、
[Cookie 等待与通用预热](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/crawlers/base.py#L1685)。

## “预热模块”和“能解决验证”的适用范围

原版配置 `crawler.warmup.platforms` 是 `[qunar, fliggy, tuniu]`。
携程 `warmup_url` 为空，往返分支自带 PC 首页预热。因此 `human.py::warm` 虽然存在于通用
warmup，未在携程 `_fetch_rt` 里调用。不能将“没导入 human.py”直接判为这次往返失败的根因。

原版也有自动验证码处理，但并非所有拦截都尝试：
`crawlers/slider.py::_STRONG_MARKS` 明确包含 **whaleguard**；strong/unknown 不自动处理。
配置说明同样写明行为型强验证不自动尝试。不能承诺接入验证码模块后这次 HTTP 432 就会通过。

证据：[预热配置与验证码范围](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/config.example.yaml#L169)、
[Whaleguard 分级](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/crawlers/slider.py#L60)、
[strong 不处理的守卫](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/crawlers/slider.py#L907)、
[导航接缝](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/crawlers/challenge.py#L275)。

## 部署声明与实测的边界

ticket-monitoring README 描述的是自托管、服务端弹窗登录与浏览器 profile 复用，
GitHub CI 负责单测与界面检查，不能用 CI 绿色证明携程账号在 GitHub 托管 runner 上出价成功。
flight-price-monitor README 虽推荐 Actions，其当前示例 platforms 仅启用 qunar，未启用 ctrip，
也没有“携程国际往返 + 携带账号 + GitHub 托管 runner”的成功证据。

证据：[ticket-monitoring 登录及无桌面部署说明](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/README.md#L625)、
[CI 工作流](https://github.com/dengmeiluan/ticket-monitoring/blob/5cfa6723f072dffb603724a23b7a2e13cf82c5d1/.github/workflows/ci.yml)、
[FlightRadar 平台示例](https://github.com/zhanglong-ustc/flight-price-monitor/blob/781cd2df18d0c83789fb70d56452ede4628706b0/config.example.yaml#L25)。

## 本次验证与保留项

离线回归检查：带登录态云端初始化实际接线、内核版本更新、脚本合并、初始化失败关闭浏览器、
Secret 从环境移除、合成 Cookie 延迟出现/始终缺失、等待时出现验证、独立标签页、只查询一次、
首页阻断时 queries=0，以及既有本地 Edge 登录与健康告警行为。

另用真实 Chromium 访问纯本地 data 页面，阻断外网、不读真实登录态；检查脚本无页面异常，
自动化标记、UA 主版本、UA-CH、桌面/触控、屏幕、语言/时区符合所选原版身份，跨临时 context 稳定。

未访问携程、未运行 Actions、未发微信；未改航线、预算、查询时间、Secret、启用变量或已有 profile。
修复仍需云端真实验收，离线通过不代表服务端放行，也不代表两程托运总预算已验证。
