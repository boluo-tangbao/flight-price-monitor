# 同程国际往返独立验收

## 携程、同程和飞猪联合监控

在 `config.monitor.yaml` 的 `comparison.platforms` 维护平台清单。目前清单包含携程
（`ctrip`）、同程（`tongcheng`）和飞猪（`fliggy`）。新增平台须先具备往返报价及详情采集能力。
以后修改清单即可，单次和定时命令保持一致。

```powershell
# 单次联合验收，输出 JSON
python scripts/compare_round_trip.py
# 启动本地定时监控，使用配置里的北京时间
python main.py -c config.monitor.yaml --compare
```

本地定时进程启动时查一轮，然后在 00:25、08:25、13:25、19:25 运行。
进程需要保持运行，查询使用本机已登录的浏览器档案。携程和同程的浏览器可在
`comparison.crawler` 下分别通过 `pc_browser_channel`、`tongcheng_pc_browser_channel` 设置。
单次验收可用 `--platforms` 临时覆盖。一次采集同时生成两种排序：

- `ranked_verified`：同一普通经济舱去返方案的两程托运均已核实且票价已含，按含托运总价排名。
- `ranked_ticket`：按平台报出的原始往返票价排名，保留起价、需要加购及托运待核实的候选。
  列表和日历参考价的口径会在推送中标明。

定时模式每轮只推送一条消息，展示两榜前五（已核实榜保留并列）、两程托运和平台采集状态；
标题的预算结论只引用已核实榜。单次验收默认在 JSON 输出两榜，只要取到了报价便正常结束。

### GitHub Actions 切换到三平台

1. 在项目目录的 `airline_monitoring` Conda 环境，关闭项目打开的同程、飞猪浏览器后，
   运行下面的上传命令。各平台分别使用独立档案，只将对应站点登录态加密上传到
   限定 `main` 分支的 `ctrip-cloud` Environment。飞猪包括项目专用档案中的淘宝登录域，
   携程已有 `CTRIP_STORAGE_STATE_B64` 时继续使用。参数应与本地登录浏览器一致。

   ```powershell
   python -m pip install -r requirements-session.txt
   python scripts/upload_ctrip_session.py --platform tongcheng --browser-channel msedge
   python scripts/upload_ctrip_session.py --platform fliggy --browser-channel msedge
   ```

2. 推送代码到 `main`，在 Actions → **Flight Price Monitor** → **Run workflow** 勾选
   `preview_comparison`。这次验收输出两榜和各平台状态，结果在 `collection-status` 附件的
   `comparison.json`，不发微信，也不读写定时任务的历史缓存。云端使用无头 Chromium。
   如果会话缺失或无法解码，报告会标记该平台且跳过其网络查询，其他平台继续执行。
   `ranked_ticket` 是原始票价，`ranked_verified` 是经两程托运硬条件筛选的报价。
   本地查询通过不能证明平台认可 Linux 上恢复的会话，需要核对每家状态和报价。

   如果只排查同程，勾选 `preview_comparison` 并把 `preview_platform` 选为 `tongcheng`。
   这次只读同程，会在 `comparison.json` 的 `platforms.tongcheng.diagnostics` 记录浏览器内是否有
   同程已知的登录 Cookie（只有存在性，不代表服务器接受登录）、导航状态码、页面归类、行程参数状态及报价响应计数。
   第三方地址和未识别路径只记录类别。报告不含页面正文、原始查询参数或 Cookie 值。
   同程单平台预览使用 `launch_persistent_context` 打开临时 Chromium 专用档案。在访问同程前读取
   现有的 `TONGCHENG_STORAGE_STATE_B64`，恢复同程站点存储，并沿用原版规则补回有效的 Cookie。
   `comparison.json` 中的 `browser_mode=upstream_persistent_chromium` 确认本次使用了这个档案入口。
   浏览器关闭后删除临时档案，档案和登录数据都不进入附件或缓存。全平台预览和定时任务继续使用
   现有的临时会话模式，待单次验收取得往返组合及含税总价后再切换。同程要求验证时记录状态。
   往返取价与托运仍走本项目的校验。如果报价接口只返回 HTTP 406，直接报告 `tongcheng_list_http_406`，
   不继续进详情，也不把接口拒绝报成无票。

3. 验收后，在仓库 Settings → Secrets and variables → Actions → Variables 添加
   `FLIGHT_MONITOR_COMPARISON_ENABLED=true`，并保持 `FLIGHT_MONITOR_ENABLED=true`。
   下一次定时任务会发一条三平台合并推送。移除联合开关即可回到原有携程定时路径。
   云端正式运行后可停掉本地定时，避免重复推送。

## 同程单平台验收

第一阶段只验证上海↔大阪，同正式配置的去返日期。采集读取同程电脑版往返页面正常加载的同一组合含税总价，
只接受去返日期均一致且有两段航班号及明确总价的记录。仅列表模式最多输出三项。
验收不会开启定时或推送，也不参与达标排名。

独立桌面登录与验收（PowerShell，项目目录，既有 Conda 环境）：

```powershell
python scripts/verify_tongcheng_round_trip.py --browser-channel msedge --login
python scripts/verify_tongcheng_round_trip.py --browser-channel msedge
```

登录在弹出的同程专用窗口里手工完成。桌面会话保存在本机的 `user_data/tongcheng-pc`，
与同程移动端和携程分开。脚本执行原版国际往返分支，遇到验证提示记录状态并停止。
`queries` 统计实际搜索文档请求。只验收列表票价时加 `--list-only`，结果中的 `query_mode`
为 `list_only`。请将本机档案保留在本地。


## 原版流程接入

固定上游版本为 `5cfa6723f072dffb603724a23b7a2e13cf82c5d1`。
`crawlers/tongcheng_upstream/` 独立保留原版浏览器初始化、会话快照恢复及保存、
导航验证码处理器、国际往返查询分支。登录和查询使用同一个 `tongcheng-pc` 档案与
同一套初始化，登录完成时仅在检测到认证状态后写入本机会话快照；这不证明服务端接受登录。

桌面依赖：`python -m pip install -r requirements-tongcheng-pc.txt`。
简单滑块依赖 numpy/Pillow；文字识别可回退 Windows OCR，ddddocr 是可选增强，
未安装点选检测后端时按原版停止。强验证不会承诺自动通过。

上游的全局调度、通知、策略账本与现场截图不接入本试点。保留安全响应计数，额外记录
白名单业务状态码，绝不输出原始响应、Cookie、账号资料。只对同程生效。
真实查询必须单独取得用户同意，离线测试通过不能证明同程放行或两程托运预算达标。

## 两程托运详情验收

`python scripts/verify_tongcheng_round_trip.py --browser-channel msedge`

本地脚本默认启用详情模式，输出 `query_mode: fare_details`，并保留列表报价作为对照。
它选中与列表报价相符的去返航班并展开经济舱报价。脚本先读取
每张报价卡上的托运文字，例如「免费托运行李额20KG」和「托运行李2×23KG」。
报价卡以「去」「返」分组时，按分组直接记录两程的额度，两程齐全即可完成核实。
如果票面上的「行李额」是链接，打开对应规则表，只读「托运」行或列，并按去返表头归属。
支持当前页展开的规则表，以及链接指向的同程规则页。
无法在表格、方向标签或链接所在分组确认范围时，先保留该程为待核实。
卡片未注明方向时，将额度保存在 `baggage_offer.fare_rule`，再打开对应行李提示，
按当前激活的去返标签读取成人托运。只有明确对应去程、回程或两程各自的额度才计入比价。
如果只能读到票面说明或一程额度，返回当次报价及已读信息，保留两程待核实状态。

结果复用 `RoundTripQuote.baggage_offer` 和 `core.baggage.baggage_total`，
输出 `checked_baggage_total`，与现有比价口径兼容。`selected_fare_verified` 表示
报价详情已核实；`final_fare_verified=false` 表示尚未进入填写页核对结算金额，
不代表已完成下单或保证出票。当前验收不启用同程定时，不触发携程或通知。

需要固定核对某组航班时，同时传 `--outbound-flight NH0976 --return-flight NH0975`；
固定航班时只操作与请求匹配的可见卡片。选择按钮可以在航班栏内或与航班栏并列，
也可能在同一个 `.flight-btn` 中显示余票提示，例如「余4张 选择」。
如果找不到目标卡，则保留列表价格，诊断中区分列表是否包含请求的往返组合和当前是否有可见卡片。

2026-11-21 验收：受控页面操作核实 NH0976/NH0975 的普通经济舱含税报价 ¥2755，
去返方向分别显示成人托运 2 件、每件 23KG，不得退改；未核对结算页或下单。
自动化详情仍未完成真实验收：先遇回程卡片定位超时，最后一轮最低价变化导致选择了
9C6575/MM0079，未匹配去程卡片。已补可见航班绑定及明确配对参数，修正后仅离线通过，
不得将此记为自动采集已成功。152 项离线检查中 150 通过、2 项可选浏览器检查跳过。
