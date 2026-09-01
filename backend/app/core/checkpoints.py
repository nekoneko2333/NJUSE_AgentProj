from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.core.models import CheckpointSnapshot
from app.storage.sqlite_store import SQLiteStore, utc_now


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class CheckpointManager:
    """记录本轮 Agent 或用户编辑的原始文本，并以哈希保护恢复操作。"""

    def __init__(self, root: Path, store: SQLiteStore, session_id: str, turn_id: str | None, label: str) -> None:
        self.root = root.resolve()
        self.store = store
        self.session_id = session_id
        self.turn_id = turn_id
        self.label = label
        self.id: str | None = None
        self.manifest: dict[str, dict[str, object]] = {}

    def capture_before(self, relative_path: str, target: Path) -> str:
        normalized = relative_path.replace("\\", "/")
        if normalized in self.manifest:
            return self.id or ""
        existed = target.exists()
        before = target.read_text(encoding="utf-8", errors="replace") if existed else ""
        if self.id is None:
            self.id = str(uuid4())
            (self.root / self.id).mkdir(parents=True, exist_ok=False)
            self.store.add_checkpoint(CheckpointSnapshot(id=self.id, session_id=self.session_id, turn_id=self.turn_id, label=self.label, files=[], created_at=utc_now()))
        self.manifest[normalized] = {
            "existed": existed,
            "before": before,
            "before_sha256": content_sha256(before),
            "after_sha256": None,
        }
        self._save()
        return self.id

    def record_after(self, relative_path: str, content: str) -> None:
        normalized = relative_path.replace("\\", "/")
        if normalized in self.manifest:
            self.manifest[normalized]["after_sha256"] = content_sha256(content)
            self._save()

    def _save(self) -> None:
        if self.id is None:
            return
        manifest_path = self.root / self.id / "manifest.json"
        manifest_path.write_text(json.dumps({"session_id": self.session_id, "turn_id": self.turn_id, "files": self.manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.set_checkpoint_files(self.id, sorted(self.manifest))

    @classmethod
    def restore(cls, root: Path, store: SQLiteStore, checkpoint_id: str, workspace_root: Path) -> tuple[bool, str, list[str]]:
        checkpoint = store.get_checkpoint(checkpoint_id)
        manifest_path = root.resolve() / checkpoint_id / "manifest.json"
        if checkpoint is None or not manifest_path.is_file():
            return False, "checkpoint_not_found", []
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
        conflicts: list[str] = []
        for relative_path, record in manifest.items():
            target = (workspace_root / relative_path).resolve()
            try:
                target.relative_to(workspace_root)
            except ValueError:
                conflicts.append(relative_path)
                continue
            current = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            if record.get("after_sha256") and content_sha256(current) != record["after_sha256"]:
                conflicts.append(relative_path)
        if conflicts:
            store.update_checkpoint_status(checkpoint_id, "conflicted")
            return False, "checkpoint_conflict", conflicts
        restored: list[str] = []
        for relative_path, record in manifest.items():
            target = (workspace_root / relative_path).resolve()
            if record.get("existed"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(record.get("before", "")), encoding="utf-8")
            elif target.exists():
                target.unlink()
            restored.append(relative_path)
        store.update_checkpoint_status(checkpoint_id, "restored")
        return True, "ok", restored
