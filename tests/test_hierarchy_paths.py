"""层级决策保留真实目录 basename 的空白。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIRECTORY = (
    Path(__file__).resolve().parents[1]
    / ".agents" / "skills" / "renaming-game-folders" / "scripts"
)
sys.path.insert(0, str(SCRIPT_DIRECTORY))

from rename_pipeline.hierarchy import build_scan_evidence, compile_hierarchy_semantics


class HierarchyPathsTests(unittest.TestCase):
    def test_preserves_leading_space_in_game_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / " Leading title v1.0"
            game.mkdir()
            (game / "Game.exe").touch()
            scan = build_scan_evidence(root)
            candidate = scan["executable_candidates"][0]
            decisions = {
                "schema_version": 2,
                "decision_schema_version": 2,
                "scan_fingerprint": scan["scan_fingerprint"],
                "game_roots": [{
                    "relative_path": game.name,
                    "candidate_ids": [candidate["candidate_id"]],
                    "type": "single_game_wrapper",
                    "confidence": 0.99,
                    "reason": "直属游戏启动项",
                }],
                "containers": [],
            }

            compiled = compile_hierarchy_semantics(scan, decisions)

            self.assertEqual(compiled["game_roots"][0]["relative_path"], game.name)
            self.assertEqual(compiled["game_roots"][0]["source_path"], str(game.resolve()))


if __name__ == "__main__":
    unittest.main()
