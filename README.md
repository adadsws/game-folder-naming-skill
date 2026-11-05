# 游戏文件夹规范化

[简体中文](README.md) | [English](README_EN.md)

## 最终效果预览

给 Codex 一个游戏目录，它会审核其中的游戏文件夹，再参考 VNDB、DLsite、Steam 补全经确认的制作方和日、中、英标题；本地版本、DLC 与标签按统一格式整理。以下是历史批准清单中的真实改名前后名称，已省略本机路径：

| 原文件夹名称 | 整理后的名称 |
|---|---|
| `重生酒馆 Rebirth Pub V1.10 官中` | `[Seikou Soft.] 転生酒場 =重生酒馆 =Rebirth Pub [UNITY v1.10 %官中]` |
| `NTR手机  NTR Phone v0.43 官中步兵` | `[Shybox] NTR Phone [GODOT v0.43 %官中步兵]` |
| `痴情哥哥与病弱妹妹的乡间生活 DLC1-5  steam 版 官中步兵` | `[Sister Position] エッチで一途なド田舎兄さまと、古式ゆかしい病弱妹 =痴情哥哥与病弱妹妹的乡间生活 =A Sex-obsessed Older Brother in the Country, and his Traditional, Sickly Little Sister [引擎未知 +DLC1~5 %官中步兵]` |

[打开三例 HTML 对照预览](docs/three-renames-preview.html)。制作方与标题只取经审核的同作来源；缺少可信语种时不会猜译名。标签保留本地审核后的语义，写法可能规范化，例如 `官中` → `%官中`。第二例的目标名只有英文标题，展示了原名字段并非无条件保留。

正式运行会生成 `preview.html`、`preview.md`、待判断清单和目录结构模拟，列出来源证据及日中英查询结果。预览不会修改游戏目录；执行须使用该次预览专属的批准码。

## 如何配置

1. 安装 Python 3.11 或更高版本；项目脚本只使用 Python 标准库。
2. 克隆或复制项目，在 Codex 中打开项目根目录。项目 Skill 位于 `.agents/skills/renaming-game-folders/`。
3. 准备需要处理的游戏目录。联网前明确授权向 VNDB、DLsite、Steam 发送本轮游戏标题、查询词及来源 ID；大型目录可用 `--include-path` 指定相对扫描根的子树。

## 如何使用

向 Codex 说明扫描根及范围，例如：“使用 `renaming-game-folders` 预览 `D:\Games` 下的游戏文件夹改名”。Codex 按 [Skill 流程](.agents/skills/renaming-game-folders/SKILL.md) 扫描、审核和搜索，再生成完整预览。

检查 `preview.html`、`decisions.md` 和 `folder-structure-preview/`。确认本次预览后，提供该预览中的批准码执行；目录或预览结果变化后须重新生成预览并重新批准。需要撤销时，可用执行回执生成单独的撤销预览。
