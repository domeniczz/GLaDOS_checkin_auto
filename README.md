# GLaDOS 自动签到，实现无限白嫖

原仓库地址：https://github.com/lukesyy/glados_automation

复制了一个仓库，进行了些修改，防止原仓库被封

环境变量：`GLADOS_COOKIE`（必要） 和 `WXPUSHER_SPT`（非必要，用于微信推送）和 `DEFAULT_CURRENT_POINTS`（非必要，积分初始值，默认 281）

`GLADOS_COOKIE`多个账号需使用 '&' 隔开，示例：cookie&cookie

`WXPUSHER_SPT` 申请地址：https://wxpusher.zjiecode.com



# Github Actions

1. 点击右上角 **fork** 按钮
2. 在自己仓库的 Settings → Secrets and variables → Actions 中配置环境变量：
   - `GLADOS_COOKIE`：GLaDOS 账号 cookie，多账号用 `&` 分隔
   - `WXPUSHER_SPT`（可选）：WxPusher 推送 token
   - `DEFAULT_CURRENT_POINTS`（可选）：积分初始值，默认 `281`，可直接在工作流文件 `.github/workflows/runGladosAction.yml` 中修改
3. 在 Settings → Actions → General → Workflow permissions 中选择 **Read and write permissions**（用于自动提交积分记录）
4. 点亮右上角的星星 **star** 激活 actions
5. 然后点击 Actions 标签查看运行的详细状况

![image](https://user-images.githubusercontent.com/70319988/231369203-c812910a-963d-45b8-98a5-95b2623c25d7.png)
![image](https://user-images.githubusercontent.com/70319988/199923789-639e8295-b03e-4abd-858e-ff427015512a.png)
![image](https://user-images.githubusercontent.com/70319988/199923884-d81dd457-ecc5-4de9-b480-191d25217c47.png)

 # 青龙面板

直接把 glados_Qinglong.py 文件放到青龙里，环境变量：
- `GLADOS_COOKIE`：同上，多账号用 `&` 分隔
- `PUSHPLUS_TOKEN`（可选）：青龙脚本暂未升级，仍使用 PushPlus 推送，申请地址 http://www.pushplus.plus
