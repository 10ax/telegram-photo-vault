from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath


class MegaCmdError(RuntimeError):
    pass


class MegaService:
    def __init__(
        self,
        target_folder: str,
        *,
        mega_ls_command: str = "mega-ls",
        mega_get_command: str = "mega-get",
        mega_rm_command: str = "mega-rm",
    ) -> None:
        self.target_folder = self._normalize_remote_path(target_folder)
        self.mega_ls_command = mega_ls_command
        self.mega_get_command = mega_get_command
        self.mega_rm_command = mega_rm_command

    @staticmethod
    def _normalize_remote_path(path: str) -> str:
        value = path.strip()
        if not value:
            raise ValueError("Remote path cannot be empty.")

        normalized = str(PurePosixPath(value))
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        if normalized != "/":
            normalized = normalized.rstrip("/")
        return normalized

    @staticmethod
    def _join_remote_path(base_path: str, name: str) -> str:
        return str(PurePosixPath(base_path) / name)

    def _is_within_target(self, path: str) -> bool:
        if self.target_folder == "/":
            return True
        return path == self.target_folder or path.startswith(f"{self.target_folder}/")

    async def _run_command(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            command = " ".join(args)
            error_text = stderr.decode("utf-8", errors="replace").strip()
            raise MegaCmdError(f"Command failed ({process.returncode}): {command}\n{error_text}")

        return stdout.decode("utf-8", errors="replace")

    def _parse_mega_ls_output(self, output: str) -> list[str]:
        files: set[str] = set()
        current_dir = self.target_folder

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if line.endswith(":"):
                current_dir = self._normalize_remote_path(line[:-1])
                continue

            if line.startswith("/"):
                normalized = self._normalize_remote_path(line)
                if normalized == current_dir:
                    continue
                if self._is_within_target(normalized):
                    files.add(normalized)
                continue

            if line.endswith("/"):
                continue

            full_path = self._join_remote_path(current_dir, line)
            normalized = self._normalize_remote_path(full_path)
            if self._is_within_target(normalized):
                files.add(normalized)

        return sorted(files)

    async def list_new_files(self) -> list[str]:
        output = await self._run_command(self.mega_ls_command, "-R", self.target_folder)
        return self._parse_mega_ls_output(output)

    async def download_file(self, remote_path: str, local_target: str | Path) -> Path:
        normalized_remote = self._normalize_remote_path(remote_path)
        target = Path(local_target)
        target.parent.mkdir(parents=True, exist_ok=True)

        await self._run_command(self.mega_get_command, normalized_remote, str(target))
        return target

    async def delete_file(self, remote_path: str) -> None:
        normalized_remote = self._normalize_remote_path(remote_path)
        await self._run_command(self.mega_rm_command, normalized_remote)
