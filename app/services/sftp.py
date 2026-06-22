from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

import asyncssh

logger = logging.getLogger(__name__)


class SFTPUploadError(RuntimeError):
    pass


class SFTPService:
    def __init__(
        self,
        host: str,
        username: str,
        *,
        port: int = 22,
        password: str | None = None,
        client_keys: list[str] | None = None,
        known_hosts: str | None = None,
        allow_insecure_host_key: bool = False,
    ) -> None:
        if not known_hosts and not allow_insecure_host_key:
            raise ValueError(
                "known_hosts is required for SFTP host-key verification. "
                "Set allow_insecure_host_key=True only for controlled/test environments."
            )

        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_keys = client_keys
        self.known_hosts = known_hosts
        self.allow_insecure_host_key = allow_insecure_host_key
        if self.allow_insecure_host_key:
            logger.warning(
                "SFTP host-key verification is DISABLED for host '%s'. "
                "Use only in controlled/test environments.",
                self.host,
            )

    @staticmethod
    def _normalize_remote_dir(remote_dir: str) -> str:
        value = remote_dir.strip()
        if not value:
            raise ValueError("Remote directory cannot be empty.")

        normalized = str(PurePosixPath(value))
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized.rstrip("/") or "/"

    async def upload_file(self, local_path: str | Path, remote_dir: str) -> str:
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(f"Local file not found: {source}")

        normalized_dir = self._normalize_remote_dir(remote_dir)
        remote_path = str(PurePosixPath(normalized_dir) / source.name)
        known_hosts = None if self.allow_insecure_host_key else self.known_hosts

        try:
            async with asyncssh.connect(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                client_keys=self.client_keys,
                known_hosts=known_hosts,
            ) as connection:
                async with connection.start_sftp_client() as sftp:
                    await sftp.makedirs(normalized_dir, exist_ok=True)
                    await sftp.put(str(source), remote_path)
                    remote_stat = await sftp.stat(remote_path)
        except (OSError, asyncssh.Error) as exc:
            raise SFTPUploadError(f"Failed to upload file via SFTP: {source}") from exc

        local_size = source.stat().st_size
        if remote_stat.size != local_size:
            raise SFTPUploadError(
                f"Upload verification failed for {source.name}: "
                f"local={local_size} remote={remote_stat.size}"
            )

        return remote_path
