# 同程国际往返独立验收

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
