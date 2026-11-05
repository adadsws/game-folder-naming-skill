# Game Folder Renaming

[简体中文](README.md) | [English](README_EN.md)

## Final result preview

Give Codex a game directory. It reviews the folders and consults VNDB, DLsite, and Steam for confirmed developers and Japanese, Chinese, and English titles. Local versions, DLC, and tags are organized into a consistent format. These are real before-and-after names from historical approved manifests; local paths are omitted:

| Original folder name | Organized name |
|---|---|
| `重生酒馆 Rebirth Pub V1.10 官中` | `[Seikou Soft.] 転生酒場 =重生酒馆 =Rebirth Pub [UNITY v1.10 %官中]` |
| `NTR手机  NTR Phone v0.43 官中步兵` | `[Shybox] NTR Phone [GODOT v0.43 %官中步兵]` |
| `痴情哥哥与病弱妹妹的乡间生活 DLC1-5  steam 版 官中步兵` | `[Sister Position] エッチで一途なド田舎兄さまと、古式ゆかしい病弱妹 =痴情哥哥与病弱妹妹的乡间生活 =A Sex-obsessed Older Brother in the Country, and his Traditional, Sickly Little Sister [引擎未知 +DLC1~5 %官中步兵]` |

[Open the three-example HTML preview](docs/three-renames-preview.html). Developers and titles come only from reviewed matches; missing languages are not guessed. Reviewed local tag meanings stay intact, although their notation may change, such as `官中` → `%官中`. The second result has only an English title: original title fields are not retained unconditionally.

A real run produces `preview.html`, `preview.md`, a decision list, and a simulated folder tree, with source evidence and three language searches. Previewing does not change game folders. Applying requires the approval code for that specific preview.

## Setup

1. Install Python 3.11 or newer. The scripts use only the Python standard library.
2. Clone or copy the project and open its root directory in Codex. The project Skill is in `.agents/skills/renaming-game-folders/`.
3. Prepare the game folder to scan. Before searching online, explicitly authorize sending this run's game titles, search terms, and source IDs to VNDB, DLsite, and Steam. For a large root, `--include-path` can select subtrees relative to it.

## Usage

Tell Codex the scan root and scope, for example: “Use `renaming-game-folders` to preview renaming the game folders under `D:\Games`.” Codex follows the [Skill workflow](.agents/skills/renaming-game-folders/SKILL.md) to scan, review, search, and produce the complete preview.

Review `preview.html`, `decisions.md`, and `folder-structure-preview/`. After approving this preview, provide its approval code to apply the changes. Changes to the folders or preview require a new preview and approval. An execution receipt can be used to generate a separate undo preview.
