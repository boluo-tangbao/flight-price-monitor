# 同程国际往返独立验收

## 携程和同程联合监控

在 `config.monitor.yaml` 的 `comparison.platforms` 维护平台清单。目前清单包含携程
（`ctrip`）和同程（`tongcheng`）。新增平台须先具备往返报价及详情采集能力。
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
单次验收可用 `--platforms` 临时覆盖。定时模式每轮生成一份合并榜单，显示平台状态、去返航司、
两程托运、当前已核实前五及并列报价。应用原有通知配置，启用 Server 酱时每轮推送一份榜单，
有核实的预算内方案时标在标题。

GitHub Actions 仍按正式的 `platforms` 清单执行。切换云端前，需要先验证同程的云端
浏览器会话、两程托运详情和单轮运行时限。云端验收通过后，再把工作流的执行命令切换到
联合模式，避免本地定时与云端任务重复推送。

## 同程单平台验收

第一阶段只验证上海↔大阪，同正式配置的去返日期。采集读取同程电脑版往返页面正常加载的同一组合含税总价，
只接受去返日期均一致且有两段航班号及明确总价的记录，最多输出三项。
当前同程报价的舱位、详情页实付价和两程托运仍待核实。验收不会开启定时或推送，也不参与达标排名。

独立桌面登录与验收（PowerShell，项目目录，既有 Conda 环境）：

```powershell
python scripts/verify_tongcheng_round_trip.py --browser-channel msedge --login
python scripts/verify_tongcheng_round_trip.py --browser-channel msedge
```

登录在弹出的同程专用窗口里手工完成。桌面会话保存在本机的 `user_data/tongcheng-pc`，
与同程移动端和携程分开。脚本执行原版国际往返分支；简单验证按原版处理，成功后可能刷新页面，
复杂/未解决验证记录状态后退出。`queries` 统计实际搜索文档请求（含验证后的刷新），
不再将一次脚本调用等同于一次网络查询。请保留本机档案，
无需提交到 GitHub。云端验收、托运采集及跨平台排名在获得页面对照证据后接入。


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

`python scripts/verify_tongcheng_round_trip.py --browser-channel msedge --details`

详情模式默认关闭。它选中与列表报价相符的去返航班并展开经济舱报价。脚本先读取
每张报价卡上的托运文字，例如「免费托运行李额20KG」和「托运行李2×23KG」。
报价卡以「去」「返」分组时，按分组直接记录两程的额度，两程齐全即可完成核实。
卡片未注明方向时，将额度保存在 `baggage_offer.fare_rule`，再打开对应行李提示，
按当前激活的去返标签读取成人托运。只有明确对应去程、回程或两程各自的额度才计入比价。
如果只能读到票面说明或一程额度，返回当次报价及已读信息，保留两程待核实状态。

结果复用 `RoundTripQuote.baggage_offer` 和 `core.baggage.baggage_total`，
输出 `checked_baggage_total`，与现有比价口径兼容。`selected_fare_verified` 表示
报价详情已核实；`final_fare_verified=false` 表示尚未进入填写页核对结算金额，
不代表已完成下单或保证出票。当前验收不启用同程定时，不触发携程或通知。

需要固定核对某组航班时，同时传 `--outbound-flight NH0976 --return-flight NH0975`；
目标不在当次验证过日期的报价中则停止，不自动换为新出现的最低价组合。

2026-11-21 验收：受控页面操作核实 NH0976/NH0975 的普通经济舱含税报价 ¥2755，
去返方向分别显示成人托运 2 件、每件 23KG，不得退改；未核对结算页或下单。
自动化详情仍未完成真实验收：先遇回程卡片定位超时，最后一轮最低价变化导致选择了
9C6575/MM0079，未匹配去程卡片。已补可见航班绑定及明确配对参数，修正后仅离线通过，
不得将此记为自动采集已成功。152 项离线检查中 150 通过、2 项可选浏览器检查跳过。
