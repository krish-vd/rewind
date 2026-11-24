"""A throwaway copy of the project that can be set to any step of a session.

Files the session never edited are copied once, as they are on disk now.
Files it did edit are rewritten for each probe from the reconstructed state.
Your working tree is never touched.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
from pathlib import Path

from .reconstruct import Timeline, file_key, known

DEFAULT_IGNORE = (
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".angular", ".next", ".turbo", ".parcel-cache",
    ".gradle", ".idea", ".DS_Store", "*.pyc",
)


class SandboxError(Exception):
    pass


def git_root(path: str) -> str | None:
    p = Path(path)
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return str(d)
    return None


def choose_root(timeline: Timeline, preferred: str | None = None) -> str:
    """The smallest folder that holds every file the session edited, widened to its git repo."""
    if preferred:
        return os.path.abspath(preferred)
    paths = [os.path.abspath(fh.path) for fh in timeline.files.values()]
    if not paths:
        raise SandboxError("This session edited no files, so there is nothing to bisect.")
    cwd = os.path.abspath(timeline.session.cwd) if timeline.session.cwd else None
    if cwd:
        inside = [p for p in paths if _is_within(p, cwd)]
        if inside:
            paths = inside  # files elsewhere (memory, config) stay as they are
    try:
        common = os.path.commonpath([os.path.dirname(p) for p in paths])
    except ValueError as exc:
        raise SandboxError("Edited files are on different drives; pass --root.") from exc
    root = git_root(common) or common
    if _too_broad(root):
        raise SandboxError(f"The edited files only share {root}, which is too broad to copy; pass --root.")
    return root


def _is_within(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(folder)]) == os.path.normcase(folder)
    except ValueError:
        return False


def _too_broad(root: str) -> bool:
    r = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    drive_root = os.path.splitdrive(r)[1] in ("", os.sep)
    return drive_root or r == os.path.normcase(str(Path.home())).rstrip("\\/")


class Sandbox:
    def __init__(self, timeline: Timeline, root: str, hold: list[str] | None = None,
                 ignore: tuple[str, ...] = DEFAULT_IGNORE, dest: str | None = None):
        self.timeline = timeline
        self.root = os.path.abspath(root)
        self.hold = [h.replace("\\", "/").strip("/") for h in (hold or [])]
        self.ignore = ignore
        self.dir = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="rewind-"))
        self._written: dict[str, object] = {}
        self.outside: list[str] = []
        self.held: list[str] = []
        self.targets: dict[str, tuple[Path, str]] = {}
        for key, fh in timeline.files.items():
            rel = os.path.relpath(os.path.abspath(fh.path), self.root)
            if rel.startswith(".."):
                self.outside.append(fh.path)
                continue
            rel_posix = rel.replace("\\", "/")
            if self._is_held(rel_posix):
                self.held.append(rel_posix)
                continue
            self.targets[key] = (self.dir / rel, fh.eol)

    def _is_held(self, rel: str) -> bool:
        for h in self.hold:
            if rel == h or rel.startswith(h + "/") or fnmatch.fnmatch(rel, h):
                return True
        return False

    def create(self) -> int:
        """Copy the project. Returns the number of files copied."""
        if not os.path.isdir(self.root):
            raise SandboxError(f"Root folder does not exist: {self.root}")
        count = 0
        patterns = self.ignore

        def ignore(_dir, names):
            return {n for n in names if any(fnmatch.fnmatch(n, p) for p in patterns)}

        dest = self.dir
        if dest.exists() and any(dest.iterdir()):
            raise SandboxError(f"Sandbox folder is not empty: {dest}")
        shutil.copytree(self.root, dest, ignore=ignore, dirs_exist_ok=True, symlinks=True)
        for _ in dest.rglob("*"):
            count += 1
        return count

    def apply(self, j: int) -> list[tuple[str, str]]:
        """Set every edited file to its state after the first j mutations.

        Returns (path, quality) for files whose state is not exact.
        """
        state = self.timeline.state_at(j)
        weak = []
        for key, (content, quality) in state.items():
            if key not in self.targets:
                continue
            target, eol = self.targets[key]
            if quality not in ("exact", "derived", "inferred"):
                weak.append((self.timeline.files[key].path, quality))
            if not known(content):
                continue
            if self._written.get(key, object()) == content:
                continue
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                data = content.replace("\n", eol) if eol != "\n" else content
                with open(target, "w", encoding="utf-8", newline="") as fh:
                    fh.write(data)
            self._written[key] = content
        return weak

    def cwd_for(self, session_cwd: str | None) -> Path:
        """Where to run the command: the session's working folder inside the sandbox, if it is inside the root."""
        if session_cwd:
            rel = os.path.relpath(os.path.abspath(session_cwd), self.root)
            if not rel.startswith("..") and (self.dir / rel).is_dir():
                return self.dir / rel
        return self.dir

    def remove(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def export_files(timeline: Timeline, j: int, root: str, dest: str) -> list[str]:
    """Write only the edited files, as they were after j mutations, into dest."""
    out = []
    for key, (content, _q) in timeline.state_at(j).items():
        fh = timeline.files[key]
        rel = os.path.relpath(os.path.abspath(fh.path), root)
        if rel.startswith(".."):
            rel = os.path.join("_outside", os.path.splitdrive(os.path.abspath(fh.path))[1].lstrip("\\/"))
        if not isinstance(content, str):
            continue
        target = Path(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(content.replace("\n", fh.eol) if fh.eol != "\n" else content)
        out.append(str(target))
    return out


__all__ = ["Sandbox", "SandboxError", "choose_root", "export_files", "file_key", "git_root"]
