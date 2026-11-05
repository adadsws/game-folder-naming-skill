# 游戏文件夹规范化

[简体中文](README.md) | [English](README_EN.md)

## 最终效果预览

Codex 审核本地游戏目录及 VNDB、DLsite、Steam 的作品资料后，生成 `preview.html`、`preview.md`、待判断清单和改名清单。预览并排显示原名称、预计名称、来源证据与日中英查询结果，并模拟批准后的目录结构。示例名称：

```text
[制作方] 日文标题 =中文标题 =English Title [RPGMAKER 本体v1.2 +DLCv1.0 %官中]
```

未确认的条目会标为跳过。预览不会修改游戏目录；执行须使用该次预览专属的批准码。

## 如何配置

1. 安装 Python 3.11 或更高版本；项目脚本只使用 Python 标准库。
2. 克隆或复制项目，在 Codex 中打开项目根目录。项目 Skill 位于 `.agents/skills/renaming-game-folders/`。
3. 准备需要处理的游戏目录。联网搜索会向 VNDB、DLsite、Steam 发送获批的标题、关键词和来源 ID；开始前向 Codex 明确授权处理范围。大型目录可指定相对扫描根的 `--include-path`，只处理选定子树。

## 如何使用

向 Codex 说明扫描根及需要处理的范围，例如：“使用 `renaming-game-folders` 预览 `U:\Games` 下的游戏文件夹改名”。Codex 按 [Skill 流程](.agents/skills/renaming-game-folders/SKILL.md) 扫描目录、审核层级和名称、搜索来源，再生成完整预览。

检查 `preview.html`、`decisions.md` 和 `folder-structure-preview/`。确认本次预览后，提供该预览中的批准码执行；目录或预览结果变化后须重新生成预览并重新批准。需要撤销时，可用执行回执生成单独的撤销预览。
