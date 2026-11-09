# GitHub 周榜 Top 1 贴图

每周一北京时间 09:00 获取周榜第一名，用 gpt-6.1-sol 写说明，生成三张图片并发送邮件预览。源榜单未更新或任务失败时，每小时重试。已生成的期次不会自动覆盖或重复发预览。

贴图标题按 20 个 UTF-16 单元、正文连同署名按 1000 个单元保守校验，图片为 3 至 9 张。署名 MJZHOU 放在正文末尾；微信创作来源选择“内容由AI生成”。

回复对应预览邮件，单独写“确认”。监听器按邮件 UID 检查指定发件人及收件人，使用只读 BODY.PEEK，同时识别已读邮件；引用的历史“确认”不会触发发表。内容及图片的哈希与确认绑定。

真正发表使用 `wechat/publish.mjs`。登录和最终发表验证可能分别扫码，二维码通过邮件发送。最终提交前持久保存日志；中断后只核实已提交记录，避免重复提交。后台“审核中”和“保存草稿”都不算“已发表”；成功通知附公开链接。

## 部署

凭据保存在服务器 `app.env` 和 `.secrets/gptkey.txt`，均不进入代码仓库。参照 `app.env.example` 配置。部署目录为 `~/github-weekly`。

`remote_install.sh` 安装独立 Python venv、npm 依赖、systemd 用户监听服务和定时器，并只清理本项目旧 cron。启用用户 linger 后，即使 SSH 退出仍继续运行。无 systemd 的 UTC 机器使用 cron 降级方案。

```sh
systemctl --user status github-weekly-preview.service
systemctl --user list-timers github-weekly-run.timer
journalctl --user -u github-weekly-preview.service -n 100
```

每期状态在 `data/<期次>/post.json`，当前队列在 `data/pending.json`；发表日志、二维码和后台截图保存在各期目录。`browser-journal.json` 记录提交过程，`wechat-result.json` 记录后台结果。

不要删除已提交期次的浏览器日志后重新发表。后台结果不明确时，程序进入核实流程，不重复点击发表。

```sh
python -m unittest discover -s tests -v
node --check wechat/publish.mjs
```
