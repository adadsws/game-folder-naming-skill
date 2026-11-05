# Game Folder Renaming

[简体中文](README.md) | [English](README_EN.md)

## Final result preview

Codex reviews local game folders and VNDB, DLsite, and Steam records, then produces `preview.html`, `preview.md`, a decision list, and a rename manifest. The preview shows original and proposed names, source evidence, Japanese/Chinese/English searches, and a simulation of the resulting folder tree. Example name:

```text
[Developer] Japanese Title =Chinese Title =English Title [RPGMAKER v1.2 +DLCv1.0 %官中]
```

Unresolved entries are marked as skipped. Previewing does not change game folders. Applying changes requires the approval code for that specific preview.

## Setup

1. Install Python 3.11 or newer. The scripts use only the Python standard library.
2. Clone or copy the project and open its root directory in Codex. The project Skill is in `.agents/skills/renaming-game-folders/`.
3. Prepare the game folder to scan. Online search sends approved titles, search terms, and source IDs to VNDB, DLsite, and Steam; explicitly authorize the scope before starting. For a large root, `--include-path` can select subtrees relative to it.

## Usage

Tell Codex the scan root and scope, for example: “Use `renaming-game-folders` to preview renaming the game folders under `U:\Games`.” Codex follows the [Skill workflow](.agents/skills/renaming-game-folders/SKILL.md) to scan folders, review hierarchy and names, search sources, and produce the complete preview.

Review `preview.html`, `decisions.md`, and `folder-structure-preview/`. After approving this preview, provide its approval code to apply the changes. Changes to the folders or preview require a new preview and approval. An execution receipt can be used to generate a separate undo preview.
