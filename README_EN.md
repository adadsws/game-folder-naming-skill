# VNDB/DLsite/Steam Game Folder Naming Skill

[简体中文](README.md) | [English](README_EN.md)

When game folders have partial titles, missing developers, or inconsistent version and tag notation, looking up each game and renaming it by hand takes time. The `renaming-game-folders` Skill reviews folders in batches against VNDB, DLsite, and Steam, fills confirmed developers and Japanese, Chinese, and English titles, organizes local versions, DLC, and tags, and previews changes before applying them with approval.

## Final result preview

These three original names simulate folders before renaming. The right column comes from the Skill's formal preview; no folders were renamed:

| Original folder name | Organized name |
|---|---|
| ` NTR Phone v0.43 官中步兵` | `[Shybox] NTR Phone [GODOT v0.43 %官中步兵]` |
| `痴情哥哥与病弱妹妹的乡间生活 DLC1-5 官中步兵` | `[Sister Position] エッチで一途なド田舎兄さまと、古式ゆかしい病弱妹 =痴情哥哥与病弱妹妹的乡间生活 [引擎未知 +DLC1~5 %官中步兵]` |
| `重生酒馆 v1.10 官中` | `[Seikou Soft.] 転生酒場 =重生酒馆 =Rebirth Pub [UNITY v1.10 %官中]` |

The second name drops its English title under the 100-character limit; its DLC range and tags remain.

These are three sections of the same Skill HTML preview. Click an image to view the [full seven-column table](docs/images/three-folder-skill-preview.png).

[![1. NTR Phone before and after, with source queries](docs/images/preview-1-ntr-phone.png)](docs/images/three-folder-skill-preview.png)

[![2. Sister Position game before and after, with source queries](docs/images/preview-2-sister-position.png)](docs/images/three-folder-skill-preview.png)

[![3. Rebirth Pub before and after, with source queries](docs/images/preview-3-rebirth-pub.png)](docs/images/three-folder-skill-preview.png)

The Skill uses reviewed matches and does not guess missing translations. Local tag meanings stay intact while their notation is normalized. The generated `preview.html` shows names, sources, and search evidence. Previewing changes no folders; applying requires that preview's approval code.

## Setup

1. Install Python 3.11+; no third-party Python packages are needed.
2. Copy the project and load the [renaming-game-folders Skill](.agents/skills/renaming-game-folders/SKILL.md).
3. Prepare the game directory. Before searching, authorize sending this run's titles, queries, and source IDs to VNDB, DLsite, and Steam. Use `--include-path` to limit large roots to selected subtrees.

## Usage

Invoke the Skill with a scan root, for example: “Use `renaming-game-folders` to preview renaming the game folders under `D:\Games`.”

Review `preview.html`, `decisions.md`, and `folder-structure-preview/`; then provide this preview's approval code to apply. Folder or result changes require a new preview and approval.
