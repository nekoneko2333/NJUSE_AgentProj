from __future__ import annotations

from pathlib import Path


class WorkspaceError(ValueError):
    pass


class Workspace:
    """将所有文件工具限制在用户显式选择的根目录。"""

    def __init__(self, root: str) -> None:
        candidate = Path(root).expanduser().resolve()
        if not candidate.is_dir():
            raise WorkspaceError("workspace_not_found")
        self.root = candidate

    def resolve(self, relative_path: str = ".") -> Path:
        target = (self.root / relative_path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError("path_outside_workspace") from error
        return target
