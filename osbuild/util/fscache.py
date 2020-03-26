"""File-system Cache

This module implements a generic file-system cache. It allows files and
directory trees to be stored in a cache located on a file-system, as well as
retrieved via a lookup function.

The cache has several advanced features:

  * All operations allow parallel readers and writers. That is, the cache can
    be shared across multiple applications.

  * A maintenance routine is provided, which traverses the cache and deletes
    old objects based on an LRU logic. This allows keeping the cache small
    while retaining often used objects.

  * Cache setup requires a directory to be specified by the user, which will
    then be used to store the cache. This directory can be located on nearly
    any file-system type, including NFS, which would allow sharing the cache
    even across different nodes on a network.
"""


import contextlib
import fcntl
import os
import re
import struct


__all__ = [
    "Cache",
]


class UnknownKeyError(Exception):
    """Unknown Cache Key

    This error is raised by operations that take a user-provided key to check
    for a cached object. In case that object does not exist, this exception is
    raised.
    """
    pass


class DuplicateKeyError(Exception):
    """Duplicate Cache Key

    This error is raised by operations that take a user-provided key to create
    a new cache entry. In case an object with that key does already exist, this
    exception is raised.
    """
    pass


class LockedError(Exception):
    """Already Locked

    This error is raised by try-lock operations when the lock cannot be
    acquired since a conflicting lock is already in place.
    """
    pass


def _check_dirname(string):
    # This checks whether `string` is a valid directory name. We only allow
    # basic characters, to prevent misuse and simplify directory structures.
    # Callers rely on this function to verify `string` is non-empty and does
    # not contain slashes. Everything else is just for visual improvements.
    return re.match(r'^[-_a-zA-Z0-9]+$', string)


def _fcntl_flock(fd, lock_type, blocking = False):
    """Perform File-locking Operation

    This function performs a linux file-locking operation on the specified
    file-descriptor. The specific type of lock must be specified by the caller.
    This function does not allow to specify the byte-range of the file to lock.
    Instead, it always applies the lock operations to the entire file.

    This function always uses the open-file-description locks provided by
    modern linux kernels. This means, locks are tied to the
    open-file-description. That is, they are shared between dupped
    file-descriptors. Furthermore, acquiring a lock while already holding a
    lock will update the lock to the new specified lock type.

    Parameters
    ----------
    fd : int
        The file-descriptor to use for the locking operation.
    lock_type : int
        The type of lock to use. This can be one of: `fcntl.F_RDLCK`,
        `fcntl.F_WRLCK`, `fcntl.F_UNLCK`.
    blocking : bool, optional
        Whether the lock-operation should block until it can acquire the lock.
        By default, this is `False` and the operation will raise `LockedError`
        on lock contention.

    Raises
    ------
    LockedError
        A conflicting lock is already present on the file, and the caller
        requested a non-blocking operational mode.
    """


    assert fd >= 0
    valid_types = [fcntl.F_RDLCK, fcntl.F_WRLCK, fcntl.F_UNLCK]
    assert any(lock_type == v for v in valid_types)

    #
    # The `OFD` constants are not available through the `fcntl` module, so we
    # need to use their integer representations directly. They are the same
    # across all linux ABIs:
    #
    #     F_OFD_SETLK = 36
    #     F_OFD_SETLK = 37
    #     F_OFD_SETLKW = 38
    #

    lock_cmd = 38 if blocking else 37

    #
    # We use the linux open-file-descriptor (OFD) version of the POSIX file
    # locking operations. They attach locks to an open file description, rather
    # than to a process. They have clear, useful semantics.
    # This means, we need to use the `fcntl(2)` operation with `struct flock`,
    # which is rather unfortunate, since it varies depending on compiler
    # arguments used for the python library, as well as depends on the host
    # architecture, etc.
    #
    # The structure layout of the locking argument is:
    #
    #     struct flock {
    #         short int l_type;
    #         short int l_whence;
    #         off_t l_start;
    #         off_t l_len;
    #         pid_t int l_pid;
    #     }
    #
    # The possible options for `l_whence` are `SEEK_SET`, `SEEK_CUR`, and
    # `SEEK_END`. All are provided by the `fcntl` module. Same for the possible
    # options for `l_type`, which are `L_RDLCK`, `L_WRLCK`, and `L_UNLCK`.
    #
    # Depending on which architecture you run on, but also depending on whether
    # large-file mode was enabled to compile the python library, the values of
    # the constants as well as the sizes of `off_t` can change. What we know is
    # that `short int` is always 16-bit on linux, and we know that `fcntl(2)`
    # does not take a `size` parameter. Therefore, the kernel will just fetch
    # the structure from user-space with the correct size. The python wrapper
    # `fcntl.fcntl()` always uses a 1024-bytes buffer and thus we can just pad
    # our argument with trailing zeros to provide a valid argument to the
    # kernel. Note that your libc might also do automatic translation to
    # `fcntl64(2)` and `struct flock64` (if you run on 32bit machines with
    # large-file support enabled). Also, random architectures change trailing
    # padding of the structure (MIPS-ABI32 adds 128-byte trailing padding,
    # SPARC adds 16?).
    #
    # To avoid all this mess, we use the fact that we only care for `l_type`.
    # Everything else is always set to 0 in all our needed locking calls.
    # Therefore, we simply use the largest possible `struct flock` for your
    # libc and set everything to 0. The `l_type` field is guaranteed to be
    # 16-bit, so it will have the correct offset, alignment, and endianness
    # without us doing anything. Downside of all this is that all our locks
    # always affect the entire file. However, we do not need locks for specific
    # sub-regions of a file, so we should be fine. Eventually, what we end up
    # with passing to libc is:
    #
    #     struct flock {
    #         uint16_t l_type;
    #         uint16_t l_whence;
    #         uint32_t pad0;
    #         uint64_t pad1;
    #         uint64_t pad2;
    #         uint32_t pad3;
    #         uint32_t pad4;
    #     }
    #

    type_flock64 = struct.Struct('=HHIQQII')
    arg_flock64 = type_flock64.pack(lock_type, 0, 0, 0, 0, 0, 0)

    try:
        fcntl.fcntl(fd, lock_cmd, arg_flock64)
    except BlockingIOError as e:
        raise LockedError


class Cache:
    """File-system Cache Manager

    Objects of this class represent a file-system cache. They require a
    file-descriptor to a directory from the user and then manage a cache of
    file-system objects in this directory. Note that the underlying directory
    can be shared across multiple cache managers, even across different
    processes or even across the network (e.g., using NFS to share the
    file-system).
    """

    _store_fd = None
    _staging_fd = None
    _lock_dirname = "lock"
    _entry_dirname = "entry"

    def __init__(self, directory_fd, store_name = "store", staging_name = "staging"):
        """Initialize a File-system Cache Manager

        Parameters
        ----------
        directory_fd : int, None
            A file-descriptor to a directory to be used as file-system cache.
            Initially, this directory should be empty. It will be populated by
            the file-system cache. Pass `None` to create a cache that discards
            any stored objects immediately and thus serves as a no-op.
            This function does not retain the file-descriptor, nor does it
            reference it once this function returns.
        store_name : String, optional
            The directory name to use inside of the cache directory for the
            directory holding stored entries.
        staging_name : String, optional
            The directory name to use inside of the cache directory for the
            directory holding staging objects being prepared for cache
            insertion, removal, or other maintenance.
        """

        assert (directory_fd is None) or directory_fd >= 0
        assert _check_dirname(store_name)
        assert _check_dirname(staging_name)

        if directory_fd is None:
            # If no directory is specified, we simply make this a no-op cache
            # that drops any stored entry, and makes every request a cache
            # miss. In most code-paths, we simply check for `_store_fd` to be
            # not None.
            self._store_fd = None
            self._staging_fd = None
            return

        try:
            os.mkdir(store_name, dir_fd = directory_fd)
        except FileExistsError:
            pass

        try:
            os.mkdir(staging_name, dir_fd = directory_fd)
        except FileExistsError:
            pass

        flags = os.O_RDWR | os.O_CLOEXEC | os.O_DIRECTORY | os.O_PATH
        self._store_fd = os.open(store_name, flags, dir_fd = directory_fd)
        self._staging_fd = os.open(staging_name, flags, dir_fd = directory_fd)

    def __del__(self):
        if self._staging_fd is not None:
            os.close(self._staging_fd)
            self._staging_fd = None
        if self._store_fd is not None:
            os.close(self._store_fd)
            self._store_fd = None

    @contextlib.contextmanager
    def load(self, key):
        """Load from the Cache

        Returns
        -------
        int
            Foobar
        """

        assert _check_dirname(key)

        if self._store_fd is None:
            # In case caching is disabled, we just always pretend the cache is
            # empty and any request is a cache miss.
            raise UnknownKeyError

        lock_fd = None
        entry_fd = None

        try:
            #
            # First step is to open the `lock` file of the cache entry. This
            # will return `ENOENT` if the cache-entry or lock file does not
            # exist. In both cases, we consider this a race and simply treat it
            # as cache miss.
            #
            try:
                flags = os.O_RDONLY | os.O_CLOEXEC
                lock_fd = os.open(os.path.join(key, self._lock_dirname), flags, dir_fd = self._store_fd)
            except FileNotFoundError:
                raise UnknownKeyError

            #
            # Second step is to acquire a read-lock on the lock-file. This will
            # prevent parallel writers from deleting the cache entry. If we
            # fail to acquire the lock, it must mean there is a parallel
            # writer. This means either the entry is about to be created or
            # destroyed. In both cases, we treat it as cache miss.
            #
            try:
                _fcntl_flock(lock_fd, fcntl.F_RDLCK)
            except LockedError:
                raise UnknownKeyError

            flags = os.O_RDWR | os.O_CLOEXEC | os.O_DIRECTORY | os.O_PATH
            entry_fd = os.open(os.path.join(key, self._entry_dirname), flags, dir_fd = self._store_fd)
            yield entry_fd
        finally:
            if entry_fd is not None:
                os.close(entry_fd)
                entry_fd = None
            if lock_fd is not None:
                # Explicitly unlock to prevent parallel `fork(2)` operations
                # from pinning the FD and as such the lock. This would not be
                # that problematic, since it would get closed on `exec(2)`, but
                # lets just be explicit and reduce the chance of the lock being
                # held for longer than really needed.
                # Also note that `F_UNLCK` works even if we do not have any
                # lock acquired, so no harm in calling it unconditionally here.
                _fcntl_flock(lock_fd, fcntl.F_UNLCK)
                os.close(lock_fd)
                lock_fd = None

        raise UnknownKeyError

    def store(self, key, directory):
        """Store into the Cache
        """

        assert _check_dirname(key)

        if self._store_fd is None:
            return

        try:
            try:
                os.mkdir(key, dir_fd = self._store_fd)
            except FileExistsError:
                raise DuplicateKeyError

            flags = os.O_RDWR | os.O_CLOEXEC | os.O_DIRECTORY | os.O_PATH
            dir_fd = os.open(key, flags, dir_fd = self._store_fd)

            flags = os.O_RDWR | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL
            lock_fd = os.open("lock", flags, dir_fd = dir_fd)
        finally:
            pass
