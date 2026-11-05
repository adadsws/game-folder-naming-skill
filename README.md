# 游戏文件夹规范化

[简体中文](README.md) | [English](README_EN.md)

## 最终效果预览

`renaming-game-folders` Skill 审核游戏目录，结合 VNDB、DLsite、Steam 补全可信的制作方与多语标题，并整理本地版本、DLC 和标签。以下为真实改名前后名称（已省略本机路径）：

| 原文件夹名称 | 整理后的名称 |
|---|---|
| `重生酒馆 Rebirth Pub V1.10 官中` | `[Seikou Soft.] 転生酒場 =重生酒馆 =Rebirth Pub [UNITY v1.10 %官中]` |
| `NTR手机  NTR Phone v0.43 官中步兵` | `[Shybox] NTR Phone [GODOT v0.43 %官中步兵]` |
| `痴情哥哥与病弱妹妹的乡间生活 DLC1-5  steam 版 官中步兵` | `[Sister Position] エッチで一途なド田舎兄さまと、古式ゆかしい病弱妹 =痴情哥哥与病弱妹妹的乡间生活 =A Sex-obsessed Older Brother in the Country, and his Traditional, Sickly Little Sister [引擎未知 +DLC1~5 %官中步兵]` |

Skill 只采用经审核的同作资料，不猜缺失的译名；本地标签保留原有语义，但写法会规范化。正式 `preview.html` 展示名称、来源与查询证据；预览不改目录，执行需要本次批准码。

## 如何配置

1. 安装 Python 3.11+；无需第三方 Python 包。
2. 复制项目并加载 [renaming-game-folders Skill](.agents/skills/renaming-game-folders/SKILL.md)。
3. 准备游戏目录；联网前授权向 VNDB、DLsite、Steam 发送本轮标题、查询词和来源 ID。大型目录可用 `--include-path` 限定子树。

## 如何使用

调用 Skill 并提供扫描根，例如：“使用 `renaming-game-folders` 预览 `D:\Games` 下的游戏文件夹改名”。

检查 `preview.html`、`decisions.md` 和 `folder-structure-preview/`；确认后提供本次批准码执行。目录或结果变化后须重新预览并批准。
