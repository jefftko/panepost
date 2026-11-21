# panepost（中文简介）

在 tmux 里同时跑 Claude Code、Codex、opencode 等多个 AI Agent 时，`ag` 能：

- **一眼看全**：`ag ls` 列出所有 Agent 及状态。状态分四种：等你（停在批准框或提问框）、草稿（输入框里有没发出的字）、忙、闲。
- **按名字互发消息**：`ag send <名字> "内容"`。
  - 已确认会话的 Codex 使用原生 `codex queue`；
  - tmux 发给 Claude Code、Codex 或 agy 前先确认输入框空闲且无草稿，完整核对输入后提交一次。遇到忙碌、选项菜单或无法辨认的界面会停下；
  - `ag send --dry-run %12 "示例"` 可预览发件者、收件者和发送方式；
  - 目标不唯一时拒发，并列出候选。
- **回复地址**：`prefix + a` 复制一行 `ag send %12 "…"   # claude work:ops(1)-1(%12) · 审查 API`。`work:ops(1)-1` 是 `work` 会话的第 1 个窗口 `ops` 中的第 1 号窗格。用 `%12`、`work:1.1` 或 `'work:ops(1)-1'` 都能找到同一目标。
- **在指定目录开 Agent**：`prefix + N` 弹窗选目录（★置顶，其余按最近使用排），再选 claude / codex / shell，开一个新窗口启动。
- **重启恢复**：`ag restore --plan` 生成重建 tmux 会话的命令和 `claude --resume` / `codex resume` 命令，**只列出、不执行**。

安装：`pipx install git+https://github.com/jefftko/panepost`，然后执行 `ag tmux-snippet >> ~/.tmux.conf`。

> 用中文输入法时，按 `prefix + A / N / a` 前先切到英文半角。

完整说明见英文 [README](README.md)。协议：MIT。
