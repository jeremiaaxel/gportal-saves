"""FTP operations: listing, recursive pull/push with resume-safe semantics."""

from __future__ import annotations

import ftplib
import io
import os
import posixpath
import socket
from dataclasses import dataclass


@dataclass
class FtpFile:
    path: str  # full FTP path
    size: int
    is_dir: bool


def connect(host: str, port: int, user: str, password: str, timeout: int = 30) -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=timeout)
    ftp.login(user, password)
    try:
        ftp.sendcmd("TYPE I")  # binary mode
    except ftplib.error_perm:
        pass
    try:
        ftp.prot_p()  # try TLS if available (FTPES)
    except Exception:
        pass
    return ftp


def close(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def mlsd(ftp: ftplib.FTP, path: str) -> list[FtpFile]:
    out: list[FtpFile] = []
    try:
        for name, facts in ftp.mlsd(path):
            if name in (".", ".."):
                continue
            size = int(facts.get("size", 0) or 0)
            is_dir = facts.get("type", "").lower() == "dir"
            out.append(FtpFile(posixpath.join(path, name), size, is_dir))
    except ftplib.error_perm:
        # MLSD unsupported — fall back to NLST without sizes
        for name in ftp.nlst(path):
            if name in (".", ".."):
                continue
            out.append(FtpFile(posixpath.join(path, name), 0, _probe_dir(ftp, posixpath.join(path, name))))
    return out


def _probe_dir(ftp: ftplib.FTP, path: str) -> bool:
    cwd = ftp.pwd()
    try:
        ftp.cwd(path)
        return True
    except ftplib.error_perm:
        return False
    finally:
        try:
            ftp.cwd(cwd)
        except Exception:
            pass


def walk(ftp: ftplib.FTP, root: str, out: list[FtpFile] | None = None) -> list[FtpFile]:
    """Recursively list all files under root (dirs included in listing but
    returned entries are files+dirs; filter is_dir for download list)."""
    if out is None:
        out = []
    for entry in mlsd(ftp, root):
        out.append(entry)
        if entry.is_dir:
            walk(ftp, entry.path, out)
    return out


def download(ftp: ftplib.FTP, remote: str, local: str, size_cb=None) -> int:
    """Download one file to local path. Returns bytes written."""
    os.makedirs(os.path.dirname(local), exist_ok=True)
    total = 0

    def sink(chunk: bytes) -> None:
        nonlocal total
        f.write(chunk)
        total += len(chunk)
        if size_cb:
            size_cb(len(chunk))

    with open(local, "wb") as f:
        ftp.retrbinary(f"RETR {remote}", sink)
    return total


def upload(ftp: ftplib.FTP, local: str, remote: str, size_cb=None) -> int:
    """Upload one file. Creates parent dirs. Returns bytes sent."""
    _ensure_dir(ftp, posixpath.dirname(remote))
    total = 0
    with open(local, "rb") as f:

        def ack(block: bytes) -> None:
            nonlocal total
            total += len(block)
            if size_cb:
                size_cb(len(block))

        ftp.storbinary(f"STOR {remote}", f, blocksize=65536, callback=ack)
    return total


def _ensure_dir(ftp: ftplib.FTP, remote_dir: str) -> None:
    parts = [p for p in remote_dir.split("/") if p]
    cur = ""
    for part in parts:
        cur += "/" + part
        try:
            ftp.cwd(cur)
        except ftplib.error_perm:
            try:
                ftp.mkd(cur)
                ftp.cwd(cur)
            except ftplib.error_perm as e:
                # might already exist (created by another race) — verify
                try:
                    ftp.cwd(cur)
                except Exception:
                    raise RuntimeError(f"Cannot create FTP dir {cur}: {e}") from e
    try:
        ftp.cwd("/")  # or whatever the FTP root is
    except ftplib.error_perm:
        pass


def ensure_dir(ftp: ftplib.FTP, remote_dir: str) -> None:
    _ensure_dir(ftp, remote_dir)


def delete_remote_tree(ftp: ftplib.FTP, remote_dir: str) -> None:
    """Best-effort recursive delete used before a push so stale files don't linger."""
    try:
        entries = mlsd(ftp, remote_dir)
    except ftplib.error_perm:
        return
    for entry in entries:
        if entry.is_dir:
            delete_remote_tree(ftp, entry.path)
            try:
                ftp.rmd(entry.path)
            except ftplib.error_perm:
                pass
        else:
            try:
                ftp.delete(entry.path)
            except ftplib.error_perm:
                pass


def test_connection(host: str, port: int, user: str, password: str) -> dict:
    ftp = connect(host, port, user, password)
    try:
        root_entries = mlsd(ftp, "/")
        names = [e.path for e in root_entries]
        return {"ok": True, "root": names}
    finally:
        close(ftp)


def safe_socket_opts(ftp: ftplib.FTP) -> None:
    try:
        sock = ftp.sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


# unused helper kept minimal
def read_bytes(ftp: ftplib.FTP, remote: str) -> bytes:
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {remote}", buf.write)
    return buf.getvalue()
