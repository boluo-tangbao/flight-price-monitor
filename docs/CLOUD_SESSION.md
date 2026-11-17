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
打包有效期为 7 天，平台会话本身可能更早失效。成功上传只证明 Secret 已保存，不证明携程云端登录有效。

云端读取后从进程环境移除状态变量，使用非持久化浏览器 context，仅在内存加载登录态，关闭 context/browser 后销毁。
登录态不进入价格库、缓存或附件。Secret 只提供给查询步骤；手动验收工作流只允许 main，正式监控仍受启用变量控制。
工作流能够读取登录态，因此能修改获准运行代码的人仍可能窃取会话；环境分支约束不能替代保护 main 和审核代码。

上传完成后，请在 GitHub 的 Settings → Environments → ctrip-cloud → Environment secrets 中确认出现上述名称，
不要尝试读取或截图它的值。此后还需要将会话加载代码同步到 main，再手动触发 Ctrip session acceptance。
本工具不会自动触发带账号查询或开启定时监控。
