# GitHub 周榜 Top 1 贴图

每周一北京时间 09:00 获取周榜第一名，用 gpt-6.1-sol 写说明，生成三张图片并发送邮件预览。源榜单未更新或任务失败时，每小时重试。已生成的期次不会自动覆盖或重复发预览。

标题统一为「每周 Github 项目推荐 - 项目短名」，按微信的20字显示计数校验（ASCII占半字）；正文连同署名按1000个UTF-16单元保守校验，图片为3至9张。署名MJZHOU放在正文末尾；微信创作来源选择“内容由AI生成”。

每期先读取Top 1项目的README和相关功能文档，固定到具体Git提交，将引用保存在`research.json`。正文以项目用途、至少三项功能亮点、一个使用场景与实际限制为主。每项功能和每张配图均需关联资料来源。资料不足或模型失败时停止并重试，不退回空泛模板。

三张配图使用可读中文知识卡片和具体任务流程、功能机制、操作/结果信息。界面布局标明“功能示意，非真实截图”，演示任务标明“示例任务”；不使用泛用机器人、发光球或抽象节点装饰。

回复对应预览邮件，单独写“确认”。监听器按邮件 UID 检查指定发件人及收件人，使用只读 BODY.PEEK，同时识别已读邮件。主题匹配支持英文`Re:`、中文`回复：`等前缀，即使QQ改写邮件引用ID，也必须精确对应本期预览主题；引用的历史“确认”不会触发发表。内容及图片的哈希与确认绑定。

真正发表使用 `wechat/publish.mjs`。登录和最终发表验证可能分别扫码，二维码通过邮件发送。最终提交前持久保存日志；中断后只核实已提交记录，避免重复提交。后台“审核中”和“保存草稿”都不算“已发表”；成功通知附公开链接。

编辑已有草稿时先通过编辑器清空正文，避免旧内容被重复粘贴。保存后重新载入并核对标题、正文及图片数量，等待微信完成封面处理；回读不一致或封面报错时停止发表。

## 部署

凭据保存在服务器 `app.env` 和 `.secrets/gptkey.txt`，均不进入代码仓库。参照 `app.env.example` 配置。部署目录为 `~/github-weekly`。

`remote_install.sh` 安装独立 Python venv、npm 依赖、systemd 用户监听服务和定时器，并只清理本项目旧 cron。启用用户 linger 后，即使 SSH 退出仍继续运行。无 systemd 的 UTC 机器使用 cron 降级方案。

```sh
systemctl --user status github-weekly-preview.service
systemctl --user list-timers github-weekly-run.timer
journalctl --user -u github-weekly-preview.service -n 100
```

每期状态在 `data/<期次>/post.json`，当前队列在 `data/pending.json`；发表日志、二维码和后台截图保存在各期目录。`browser-journal.json` 记录提交过程，`wechat-result.json` 记录后台结果。

修改已发表内容时可运行`weekly_pipeline.py revise data/<期次> --prepare-only`生成独立修订预览，保留原版本及确认记录。新版发送预览后需回复新版邮件确认，旧版确认不会触发新版发表。

不要删除已提交期次的浏览器日志后重新发表。后台结果不明确时，程序进入核实流程，不重复点击发表。

```sh
python -m unittest discover -s tests -v
node --check wechat/publish.mjs
```
