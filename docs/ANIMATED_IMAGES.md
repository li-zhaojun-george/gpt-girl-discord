# 动态表情与动画贴纸

继续使用现有 Codex 登录。程序把动画采样为按时间排列的画面，再由同一后端读取动作、表情变化和画面文字；表情解释结合当前话题、发图者和文字来源，最后自然接话。没有接入其他视觉 API。

## 聊天方式

- 可上传 GIF、动态 WebP、APNG，或发送 Discord 原生 APNG、GIF、Lottie 贴纸。PNG/JPEG 等静态识图仍正常。
- 可以先说“这把开局以为稳了，结果又输了”，再单独发动态表情；现有30秒同人图文关联继续生效。
- 可以回复动画问“最后怎么了”，或紧接着问“先哭还是先笑”。追问重新读取原消息和动画，不只复述旧回答。
- 普通接梗回应当前话题，不默认逐帧讲解。明确询问细节时才解释动作顺序；群友经历不会因为角色语气变成机器人的经历。
- 动画不绕过安静时段、参与模式、频道白名单、其他收件人或退出记录。给别人的对话仍可保持沉默。

## 读取范围

每轮最多4个媒体，每个媒体最多提供6幅采样画面，按时间顺序排列在一张拼图中。帧号和时间标签由程序生成，不是原图文字。优先保留开始、结束、时间分布及明显变化处；不连续上传所有帧，也不宣称看过完整视频或听到声音。短暂动作可能漏采，时长缺失时用估算值并明确标记，不据此断言精确速度。

媒体上限为10 MiB、单帧2000万像素、600帧、单轮30秒、总解码2亿像素。图片每格最长边512，静态图仍最长边2048。GIF/APNG 使用 Pillow 已合成的帧处理透明与残影；APNG 的默认封面不算动画第一帧。超限、损坏、超时会标为读取失败，不能退回首帧冒充看懂动画。

Lottie 仅支持自包含矢量动画：JSON最多2 MiB，画布不超过1024×1024，最多150000个结构节点，限制层数、嵌套与引用。外部图片/文件/字体、文字层、表达式及重复器不进入渲染；递归或缺失的素材引用拒绝。遇到这些类型可上传导出的 GIF/APNG。本阶段不解析自定义 emoji、任意网页、Tenor/GIF 网页链接的 MP4 预览、音频或普通视频。

## 实现与验证

`animation_frames.py` 在独立子进程中解码和渲染，每个媒体限时12秒；超时或取消会结束子进程。不给渲染器传入 Discord/API 凭证，不执行动画内脚本或解析外部资源。这个进程隔离用于限制生命周期，不等同于完整 OS 安全沙箱。像素只在内存、管道和 Codex 临时输入目录中短暂存在，视觉/动画推测不写入长期记忆。

`vision.py` 按 Discord 消息中的格式与数字 ID 构造贴纸 CDN 地址，不以贴纸名、JSON字段或缩略图猜动作。视觉观察保留格式、帧数、采样时序与覆盖范围。后续话题检索、表情理解、接话判断及最终回答沿用来源核对和失效规则。

```powershell
python -B -m unittest discover -q
python -B animation_regression.py --executable C:\path\codex.exe --model YOUR_MODEL --output animation-live.json
```

离线测试覆盖各格式实际解码、先后顺序、短暂变化、非等长帧、APNG封面、透明/残影、Lottie矢量运动、资源限制、取消清理、不同媒体部分失败、连续追问和来源变化。真实 Codex 回放包括三段合成动画（含反差图追问）和两张实际官方动画贴纸；只模拟聊天，不启动机器人或发送 Discord 消息。模型输出与解释保存在报告中，期待结果仅用于检验，不传给模型。通过这些样本不能保证所有动画都能准确理解。

参考：[Discord 贴纸格式](https://docs.discord.com/developers/resources/sticker)、[Discord CDN 地址](https://docs.discord.com/developers/reference#image-formatting)、[Pillow 动画格式处理](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html)、[rlottie-python](https://github.com/laggykiller/rlottie-python)。
