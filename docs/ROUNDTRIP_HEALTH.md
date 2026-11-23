# 往返与采集健康：源码调研及本地增量接入

核查日期：2026-11-17。上游源码固定快照：`658ae9c9d50f8af656208fd972580752e349d844`。
本地起点：`cd01e82a2f0638c171c93510bc94f911e2060543`，变更前 `git status --short` 为空。
本次只修改本地文件，没有提交、推送或修改上游，没有启动实际航线监控或发送通知。

## 结论

`ticket-monitoring` 有真正的平台往返组合报价实现，不只是反向单程相加。
但“平台给出的往返产品起价”和“去回程具体航班、舱位全部选定后可支付的订单总价”必须区分。
源码能证明前者；本次没有账户登录后的实时报价、下单或支付验证，不能承诺后者。
如果你的严格验收要求是确定两段航班及票规的一份可支付订单，本轮接入还未达到这个标准。

上游健康监控比本地丰富得多，但由日志解析、路径账本、轮次脉冲、主进程状态及通知共同组成，
并不是一个可直接复制的完整模块。最小风险方案是复用小型往返解析器，在本地建立明确结果状态和独立健康表。

## 上游往返能力：以当前代码为准

所有链接固定到本次审核快照。

| 渠道 | 查询入口及价格证据 | 覆盖、限制 |
| --- | --- | --- |
| 携程网页 | PC `online/list/round-{from}-{to}?depdate=去程_回程&containstax=1`；`_parse_rt_body` 只取“¥N起”邻接“往返含税价/往返总价”的卡片 | 国内、国际；需要可信登录会话。属于组合起价，通常尚未指定返程具体航班。H5 双段请求仍可能只有去程价，不能复用为往返 |
| 同程网页 | 国内 `book1/roundtrips` 读接口 `atp`；国际登录后 book1 RT 页，监听 `miflightapi/ts/list`，读每条 `res.tp` 与 `lng` | 国内、国际；国际 `lng` 有去/返航班与日期，比纯起价更有配对证据；登录、页面签名和风控仍会阻断 |
| 途牛网页 | 国内 `listFlight` 双段请求；国际 `intelProduct/queryFlight` 双段 `segmentList`，读 `productList.avgTotalPrice` | 国内、国际；国际是平台自报总价，不能取两个单程最低价替代；城市码、会话令牌和频控依赖较多 |
| 飞猪网页 | 国际 `cheapestCalendar`，`tripType=1` 加 `backDate`；解析时校验去返两天，并按响应中的票费和税计算总价 | 原项目的国际往返只有日期级报价，缺少每个票档的航班和去返托运。国内网页和淘宝 H5 的往返未接入 |

当前项目的上海与大阪往返可用 `python scripts/compare_round_trip.py --platforms fliggy --diagnostics` 单独验收。桌面适配按「选为去程」「选为返程」进入票档，再逐个悬停「退改签规则及行李额」，从表格分别读取两个方向的成人托运。每轮最多查看四个直飞去程各两个返程。只在同一票档的含税价、航班和去返两程已含托运都核实后参与联合排名。日历参考价只进入诊断，可用 `--fliggy-calendar-only` 单独查询。

飞猪桌面使用独立的 `user_data/fliggy-pc` 档案，遇登录或验证停止。桌面票档目前已按用户截图离线验证，实站仍待验收。飞猪通过参数单独启用。

`python scripts/compare_round_trip.py --fliggy-login` 打开同一档案供手动登录。需要指定浏览器时，登录和查询都使用相同的 `--fliggy-channel`。飞猪桌面适配保留原项目的往返日历契约，桌面票档采集独立实现。原项目有自动首页预热，本地适配走普通浏览器的手动登录与档案复用。

同程国际往返复用固定上游提交的 `_fetch_intl_rt`、独立桌面档案和会话恢复。上游的国际单程与国内 PC 支线各有首页导航步骤，国际往返在进入单程支线之前直接分流。同程本地适配未移植上游的全局预热调度、软拒恢复和请求节流。验证处理接线保留，`--diagnose` 遇验证只识别并停止。同程的票档和托运展开由本项目实现。
| 去哪儿网页 | 源码保留历史往返页面解析 | 当前 `core/channels.py` 已将网页 PC/H5 全维度标为 `n`，上游注明停服；不能因旧实现存在而推荐为可用渠道。这是上游当前声明，本轮没有独立测试站点停服情况 |
| App 渠道 | 智行、去哪儿、携程、同程、飞猪各自 App 往返页面 | 矩阵标记有实现；去哪儿仅国内、同程仅国际、飞猪仅国内且日期有边界，智行/携程有双端通路。依赖 Windows、MuMu、ADB、App 登录快照，不适合直接搬到 GitHub Actions Linux runner |

证据：[携程解析与入口](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/crawlers/ctrip.py#L721)、
[同程](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/crawlers/tongcheng.py#L723)、
[途牛国际往返](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/crawlers/tuniu.py#L1524)、
[飞猪日期校验](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/crawlers/fliggy.py#L477)、
[当前能力矩阵与范围](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/core/channels.py)。

上游 `Route.ret_date` 保留单值兼容，`ret_dates` 支持多返程日，`main.py` 按返程日传给
`safe_fetch(..., ret_date=...)`。标准 `FlightPrice` 顶层仍只有去程日期和 price，往返证据主要放在
`extra` 元素中的 `tripType: RT`、`backDate`、`basis`、返程航班字段。
`flight_prices` 没有独立 trip_type/return_date 列；存储与报表依靠 extra 防止单程、往返混桶。
`specials` 的 `ret_price` 和反向配对则可能涉及两张单程，不应把“特价发现的往返配对”当成同订单总价。

证据：[模型](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/core/models.py)、
[编排](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/main.py#L3011)、
[存储的往返隔离注释](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/core/storage.py#L1371)、
[设计文档](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/docs/INTL_DESIGN.md#L169)。
上游设计文档、注释及个别测试含历次探针旧结论，例如同程“不支持”、途牛“仅国内”，与当前实现不一致；本报告优先采用现有分发逻辑、解析字段和能力矩阵，未将文档中的历史价格作为本轮实测。

## 上游健康监控能否直接复用

| 需求 | 上游实现 | 判断 |
| --- | --- | --- |
| 各平台每轮成功、失败、耗时、条数 | `core/pulse.py`，持久化 `data/pulse.json`，`/api/pulse` | 可参考，但 `rows>0` 才判 ok；按渠道聚合，不能单独满足每航线每日期健康 |
| 平台健康时间线、部分成功、登录、维护、空结果 | `core/health.py::parse_health`，读取 monitor.log | 有；依赖特定日志词句；区分日历和明细，包含 domestic/international 证据时间，但不是完整查询键的严格事件账本 |
| 每平台/航线/日期/入口结果 | `core/storage.py::save_fetch_path`、`fetch_paths` 表及 BaseCrawler 的 path_note | 有，含 status、HTTP 状态、耗时、attempt_id、证据及 90 天清理；需要接入爬虫产生事件才能可靠工作 |
| 超时、验证码/风控、登录、无票和解析故障 | 各爬虫显式路径状态，日志健康解析及查询账本 | 有多层机制，可靠性取决于各入口证据。仅凭空列表不能判断。日志解析中 1999 软拒进入 empty 计数但备注“不是无票”，不能只读状态名 |
| 最近成功和连续失败、通知 | `main.py` 的 zero_streak / last_ok / last_alert，加 `core/channel_alert.py` | 有平台级连续零数据告警、冷却、免打扰；计数在运行时字典，进程重启复位。它统计零数据而非严格故障，不能直接作为无票/故障区分机制 |

证据：[pulse](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/core/pulse.py)、
[日志健康解析](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/core/health.py)、
[路径存储](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/core/storage.py#L834)、
[告警判定](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/core/channel_alert.py)、
[主程序通知接线](https://github.com/dengmeiluan/ticket-monitoring/blob/658ae9c9d50f8af656208fd972580752e349d844/main.py#L3844)。

## 本地对照及已落地范围

原本地实现：`Route` / `FlightPrice` 没有回程维度，`build_config.py --return-dates` 生成反向单程航线；
`safe_fetch` 捕获异常返回空列表，平台内部也会吞异常；仅存价格和低价通知去重，无健康表。
Actions 每轮重建配置，未持久化 SQLite，跨运行健康和通知状态无法延续。

本次新增：

1. 显式 `trip_type: round_trip` + `return_dates`；保留旧单程配置语义，拒绝漏填行程类型、非法日期和回程不晚于去程。
2. 复用携程 PC 往返卡解析器（保留 Apache-2.0 许可证和来源），独立适配器只接受“往返含税价”，不接受裸价、单程价或不明确总价；1 成人、经济舱、CNY。
3. 往返写入新增 `round_trip_quotes`，不改旧价格表；单程历史、报表和低价告警继续使用旧表。往返通知去重包含两日期、币种、口径。
4. 新增 `collection_runs` / `collection_health`；查询键含平台、起点、终点、去程日、回程日；保存耗时、条数、结果、最后成功、连续失败、通知状态。最后成功指查询正常完成，包含有明确无票证据的 empty；不等同于最近有报价。
5. 状态：`ok`、有明确证据的 `empty`、`timeout`、`blocked`、`login_required`、`parse_error`、`error`、`unknown_empty`、`unsupported`。老采集器空列表默认 unknown_empty；日志中的异常、超时及风控提示可提供间接分类，标记 legacy_log_signal，不冒充原始响应证据。
6. 健康告警默认关闭，配置后复用 Server酱，每轮汇总、连续失败门槛、冷却、跨午夜免打扰、恢复通知；仅成功送达才更新去重。unknown_empty 会累计异常次数但通知仍保留其不确定状态。unsupported 不计失败。
7. 配置生成器默认保留现有文件；仅显式 `--overwrite` 覆盖。Actions 从环境变量注入 SendKey，兼容旧 `SERVERCHAN_KEY`，新增默认数据库缓存、私有运行附件。

限制：首版严格接受城市级查询（如 SHA→TYO），PVG/NRT/HND/KIX 等需要机场筛选的请求直接 unsupported，
不会把 PVG 静默改成上海所有机场。SHA 本身在此入口代表上海城市范围，包含 PVG/SHA；机场级筛选留待航线阶段。
其他四家本地爬虫尚未移植往返实现，显式 unsupported。往返尚未加入旧 Pages 图表，通过 SQLite/状态导出查看。
健康超时来自每个浏览器/网络操作和异常；尚无独立子进程硬截止。整个进程不启动或卡死时，它不能自我通知；
状态导出可标 stale，但要主动提醒“调度器没有运行”仍需外部心跳观察者。本次未建立外部服务。

## 使用及验证

没有改写用户的 config.yaml、会话目录、现有数据库或航线列表，没有自动启用示例。
健康记录在下一次正常运行时自动新增表，通知须显式启用 health.enabled。

示例见 `config.roundtrip.example.yaml`，合并所需字段到自己的配置即可；不要用整份示例覆盖已有配置。

```yaml
health:
  enabled: true
  failure_threshold: 3
  cooldown_hours: 6
  quiet_start: "23:00"
  quiet_end: "07:00"
```

完成携程 PC 登录：在项目目录中使用已安装依赖和 Chromium 的 Python 环境运行：

```powershell
python main.py -c config.roundtrip.example.yaml --login ctrip-pc
```

当前项目未发现 config.yaml，因此上面指定已有示例作为登录配置；它默认使用 user_data/ctrip，
与默认往返采集共用会话目录。将来使用自定义配置时，登录与采集都指定同一份配置、从同一项目目录启动，
确保 crawler.user_data_dir 一致。PC 登录命令使用桌面 UA 和可见浏览器，
不创建价格数据库、不执行采集、不发送通知。不要与另一个正在使用同一会话目录的采集进程同时启动。

PC 登录现在先打开空白页，由用户在地址栏手动粘贴 `https://flights.ctrip.com/` 进入；
程序不自动导航到携程。在页面点击登录，自行扫码或完成验证码；看到已登录后回到终端按 Enter 关闭窗口。
支持在同一窗口新建标签页登录；若仍停留在空白页或已关闭携程标签页，则报告 login_incomplete，不报告完成。
原有 `--login ctrip` 保留手机网页入口。保存了会话不代表平台必然允许报价，需要后续实际往返页面验收。
本轮没有索取或读取凭据，也没有执行联网登录验证。

### whaleguard block 的诊断

先用日常 Chrome/Edge 在同机同网手动访问携程；普通浏览器也被拦截时，应暂停采集，按网站提示处理或联系携程，
不能仅凭提示认定是账户、IP 或浏览器原因。不要循环刷新登录页。

携程 PC 登录和往返查询现使用浏览器自身的 UA 与默认自动化标识，不沿用旧 Chrome 126 UA 或隐藏 webdriver 的脚本。
支持已安装 Edge（`--browser-channel msedge`）或 Chrome（`--browser-channel chrome`），不自动安装或替换日常浏览器。

```powershell
python main.py -c config.roundtrip.example.yaml --login ctrip-pc --browser-channel msedge
```

Edge 使用项目独立目录 `user_data/ctrip-msedge`，Chrome 使用 `user_data/ctrip-chrome`；
原 Chromium 会话 `user_data/ctrip` 保留。新浏览器首次需要登录，不读取你日常浏览器的配置或登录记录。
之后查询时也要带相同的 `--browser-channel msedge`，或在自己的配置里设置：

```yaml
crawler:
  pc_browser_channel: msedge
```

PC 登录遇到 whaleguard/访问被拒绝将报告 blocked 并以退出码 3 结束，不报告登录完成、不自动重试。
浏览器兼容配置的调整不保证风控解除；实际原因与会话有效性仍需现场验证。

后续用户确认：同一个程序启动的浏览器，手动在地址栏进入携程可以正常访问，自动导航则被拦。
因此不能将本次问题直接归因于 Chromium、浏览器版本或网络；PC 登录采用手动导航。
目前只改变登录阶段，自动往返查询仍使用程序导航，保存会话后是否能正常查询需单独验收。

### 登录后误报 login_incomplete

同步 Playwright 在终端 input() 阻塞期间可能尚未分发手动导航与新标签事件；直接读取 page.url/ctx.pages
会读到旧空白页状态。回车后现先通过一次短暂的 Playwright 等待处理事件，再检查当前页面，并接受根域 ctrip.com。
失败诊断只显示域名、不显示路径或查询参数；会话目录不删除。窗口在回车后关闭是程序退出时清理浏览器的行为，
本身不证明登录失效。重开同一浏览器选项后手动进入携程，若仍显示已登录，直接回终端确认即可，无需再次扫码。

```powershell
python -m unittest discover -s tests -v
python scripts/collection_status.py --db data/prices.db --out data/collection-status.json
```

后者在至少运行过一次新版后使用，输出健康状态和最近 100 条往返报价，并标注超过 24 小时未更新的条目。
原始状态与往返报价不提交到公开 Pages。Actions 缓存只覆盖默认 `data/prices.db`；自定义数据库路径需同步改 workflow。
缓存可能因 GitHub 回收而消失，不能替代持久化备份；登录会话没有上传到 Actions，云端未登录可能正常返回 login_required/blocked。
已提交的 config.yaml 优先于 Actions 输入；需要改航线时改配置，或明确生成另一份文件，避免意外覆盖。

当前验证包括离线解析、模拟浏览器响应、临时 SQLite、编排集成、旧配置保护、通知去重和免打扰测试；
没有实际运行平台爬虫或验证平台当日仍能出价，因此“代码路径可用”和“现在可购买”不混为一谈。

## 后续接入顺序

先用一对城市完成携程 PC 登录、单程正控和往返总价人工对照，再迁入同程国际 `tp+lng` 的确定配对证据；
随后是途牛国际 `avgTotalPrice+segmentList`，最后才加飞猪国际日历（单独标记 calendar，不冒充航班报价）。
去哪儿网页不接入；App 作为后续可选独立进程，避免引入模拟器依赖到现有主项目。
航空公司官网采集器不在本轮实现范围，待航线与出行日期确定后按实际承运航司逐个接入。

回滚只需恢复本次修改的代码和工作流；新增表可保留，不影响旧版本。原文件备份及变更清单保存在本次聊天工作目录，
写回前逐文件核对原始哈希，若期间有新改动就拒绝覆盖。
