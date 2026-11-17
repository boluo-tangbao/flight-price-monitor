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
| 飞猪网页 | 国际 `cheapestCalendar`，`tripType=1` 加 `backDate`；解析时校验响应回程日恰好等于请求值 | 仅国际往返日历；有总价口径，不保证航班明细。窗口返回多个日期，必须双日期匹配。税费需按响应加税而非固定税垫。国内往返显式 unsupported，淘宝 H5 也不支持往返 |
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

当你决定启动实际监控时，先在自己的正常环境完成携程登录；PC 与 H5 的会话可用性需要现场验证，
现有 `--login ctrip` 仍是原有 H5 登录入口，登录后也可能被 PC 页面判为需要登录，届时以健康记录为准。
本次没有索取或读取凭据，也没有执行这个联网验证。

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
