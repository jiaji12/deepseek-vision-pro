---
name: see-vision
description: >
  让 DeepSeek 等纯文字模型具备看图能力。当用户发送图片/截图（发图、发图片、发截图、
  看图、识别图片、OCR、分析截图、这是截图、你看看这个图、帮我读图）时，本技能配合
  识图代理把图片交给视觉模型（智谱 GLM-4V-Flash，免费）识别成文字，再交给 DeepSeek
  回答。安装本技能后 DeepSeek 发图自动识别，无需手动操作。若识别失败或代理未运行，
  提示用户运行安装目录下的 安装.bat。
---

# see-vision — 给 DeepSeek 装识图眼睛

## 这是什么

DeepSeek 官方 API 是纯文字模型，看不了图片。本技能用一个**本地识图代理**（`see_proxy.py`，端口 8777）拦在 ZCode 和 DeepSeek 之间：

```
用户发图 → ZCode → 本地代理(8777) → 智谱GLM-4V识别成文字 → DeepSeek看文字回答
```

效果：
- DeepSeek 收到的是文字，**永远不会说"看不了图片"**
- 识别用的智谱 `glm-4v-flash` 是**免费模型**，识图不花钱
- 全自动，不依赖模型自觉

## 使用

装好后（见下文安装），直接在 ZCode 里：

1. **发图 + 提问**：粘贴/拖拽一张图，配上问题，DeepSeek 自动看图回答
2. **多张图**：可以一次发多张，代理会分批识别并对应
3. 识图结果几秒内返回

## 安装（一次性）

1. 把整个 `see-vision` 文件夹放到 `~/.zcode/skills/` 目录下
2. 双击文件夹里的 **`安装.bat`**（或运行 `python install.py`）
3. 按提示填智谱 key（免费）和 DeepSeek key
4. 重启 ZCode，开新对话发图即可

安装脚本会自动：复制文件、写密钥、改 ZCode 配置（DeepSeek 指向本地代理）、设置开机自启、启动代理。

## 文件说明

| 文件 | 作用 |
|---|---|
| `see_proxy.py` | 识图代理（核心），拦在 ZCode 和 DeepSeek 之间 |
| `see_proxy_watchdog.py` | 看门狗，代理挂了自动拉起 |
| `see.py` | 调视觉模型识图的脚本（智谱 GLM-4V-Flash） |
| `see_proxy_config.json` | 代理配置（端口、密钥、识图模型） |
| `config.json` | see.py 的识图配置 |
| `安装.bat` / `install.py` | 一键安装脚本 |

## 故障排查

- **DeepSeek 说"看不了图片"**：代理没在跑。看 `see_proxy.log`，或重新双击 `安装.bat`。
- **识图慢**：确认 `see_proxy_config.json` 里 `agnes_model` 是 `glm-4v-flash`（免费快速）。
- **改了密钥不生效**：改 `see_proxy_config.json` 后重启代理（杀进程重新跑，或重启电脑）。
- **ZCode 报连接错误**：代理没起来，端口 8777 未监听。重新运行 `安装.bat`。
