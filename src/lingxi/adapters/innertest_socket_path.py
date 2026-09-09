"""受保护目录内的独占生命周期，强杀后仅恢复本用户的陈旧 socket。"""

import errno
import fcntl
import os
import socket
import stat


def validate_socket_directory(path):
    """受限 SSH 主体不能替换目录，避免连接到伪造服务。"""
    directory = os.path.dirname(os.path.abspath(path))
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
        raise ValueError("socket_directory_owner_invalid")
    if info.st_mode & 0o022:
        raise ValueError("socket_directory_writable")


class SocketPathOwner:
    """锁文件保持原 inode；删除锁文件会让不同实例各自拿到一把锁。"""

    def __init__(self, path):
        """路径只来自固定服务端配置。"""
        self.path, self.fd, self.bound = path, None, None

    def acquire(self):
        """先排除并行启动，再区分活动服务、陈旧 socket 与异常文件。"""
        validate_socket_directory(self.path)
        self.fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                raise ValueError("socket_lock_invalid")
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("socket_in_use") from error
            self._recover_stale()
        except Exception:
            self.close()
            raise

    def _recover_stale(self):
        """只有连接被明确拒绝的本用户 socket 才可删除，其他异常均拒绝启动。"""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("socket_path_invalid")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(self.path)
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise ValueError("socket_state_unknown") from error
            else:
                raise ValueError("socket_in_use")
        current = os.lstat(self.path)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("socket_path_changed")
        os.unlink(self.path)

    def remember_bound(self):
        """退出仅清理由本次绑定产生的文件，不能删除后来替换的路径。"""
        info = os.lstat(self.path)
        self.bound = (info.st_dev, info.st_ino)

    def close(self):
        """启动半途失败和正常退出共用清理，强杀由操作系统释放锁。"""
        try:
            if self.bound is not None:
                try:
                    info = os.lstat(self.path)
                except FileNotFoundError:
                    pass
                else:
                    if (info.st_dev, info.st_ino) == self.bound:
                        os.unlink(self.path)
        finally:
            self.bound = None
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
