#
# Test Infrastructure
#

import contextlib
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest

import osbuild
from osbuild.util import linux


class TestBase(unittest.TestCase):
    """Base Class for Tests

    This class serves as base for our test infrastructure and provides access
    to common functionality.
    """

    @staticmethod
    def have_test_checkout() -> bool:
        """Check Test-Checkout Access

        Check whether the current test-run has access to a repository checkout
        of the project and tests. This is usually the guard around code that
        requires `locate_test_checkout()`.

        For now, we always require tests to be run from a checkout. Hence, this
        function will always return `True`. This might change in the future,
        though.
        """

        # Sanity test to verify we run from within a checkout.
        assert os.access("setup.py", os.R_OK)
        return True

    @staticmethod
    def locate_test_checkout() -> str:
        """Locate Test-Checkout Path

        This returns the path to the repository checkout we run against. This
        will fail if `have_test_checkout()` returns false.
        """

        assert TestBase.have_test_checkout()
        return os.getcwd()

    @staticmethod
    def have_test_data() -> bool:
        """Check Test-Data Access

        Check whether the current test-run has access to the test data. This
        data is required to run elaborate tests. If it is not available, those
        tests have to be skipped.

        Test data, unlike test code, is not shipped as part of the `test`
        python module, hence it needs to be located independently of the code.

        For now, we only support taking test-data from a checkout (see
        `locate_test_checkout()`). This might be extended in the future, though.
        """

        return TestBase.have_test_checkout()

    @staticmethod
    def locate_test_data() -> str:
        """Locate Test-Data Path

        This returns the path to the test-data directory. This will fail if
        `have_test_data()` returns false.
        """

        return os.path.join(TestBase.locate_test_checkout(), "test/data")

    @staticmethod
    def can_modify_immutable(path: str = "/var/tmp") -> bool:
        """Check Immutable-Flag Capability

        This checks whether the calling process is allowed to toggle the
        `FS_IMMUTABLE_FL` file flag. This is limited to `CAP_LINUX_IMMUTABLE`
        in the initial user-namespace. Therefore, only highly privileged
        processes can do this.

        There is no reliable way to check whether we can do this. The only
        possible check is to see whether we can temporarily toggle the flag
        or not. Since this is highly dependent on the file-system that file
        is on, you can optionally pass in the path where to test this. Since
        shmem/tmpfs on linux does not support this, the default is `/var/tmp`.
        """

        with tempfile.TemporaryFile(dir=path) as f:
            # First try whether `FS_IOC_GETFLAGS` is actually implemented
            # for the filesystem we test on. If it is not, lets assume we
            # cannot modify the flag and make callers skip their tests.
            try:
                b = linux.ioctl_get_immutable(f.fileno())
            except OSError as e:
                if e.errno in [errno.EACCES, errno.ENOTTY, errno.EPERM]:
                    return False
                raise

            # Verify temporary files are not marked immutable by default.
            assert not b

            # Try toggling the immutable flag. Make sure we always reset it
            # so the cleanup code can actually drop the temporary object.
            try:
                linux.ioctl_toggle_immutable(f.fileno(), True)
                linux.ioctl_toggle_immutable(f.fileno(), False)
            except OSError as e:
                if e.errno in [errno.EACCES, errno.EPERM]:
                    return False
                raise

        return True

    @staticmethod
    def can_bind_mount() -> bool:
        """Check Bind-Mount Capability

        Test whether we can bind-mount file-system objects. If yes, return
        `True`, otherwise return `False`.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            original = os.path.join(tmpdir, "original")
            mnt = os.path.join(tmpdir, "mnt")

            with open(original, "w") as f:
                f.write("foo")
            with open(mnt, "w") as f:
                f.write("bar")

            try:
                subprocess.run(
                    [
                        "mount",
                        "--make-private",
                        "-o",
                        "bind,ro",
                        original,
                        mnt,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
                with open(mnt, "r") as f:
                    assert f.read() == "foo"
                return True
            except subprocess.CalledProcessError:
                return False
            finally:
                subprocess.run(
                    ["umount", mnt],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

    @staticmethod
    def have_rpm_ostree() -> bool:
        """Check rpm-ostree Availability

        This checks whether `rpm-ostree` is available in the current path and
        can be called by this process.
        """

        try:
            r = subprocess.run(["rpm-ostree", "--version"],
                               encoding="utf-8",
                               capture_output=True,
                               check=False)
        except FileNotFoundError:
            return False

        return r.returncode == 0 and "compose" in r.stdout

    @staticmethod
    def have_tree_diff() -> bool:
        """Check for tree-diff Tool

        Check whether the current test-run has access to the `tree-diff` tool.
        We currently use the one from a checkout, so it is available whenever
        a checkout is available.
        """

        return TestBase.have_test_checkout()

    @staticmethod
    def tree_diff(path1, path2):
        """Compare File-System Trees

        Run the `tree-diff` tool from the osbuild checkout. It produces a JSON
        output that describes the difference between 2 file-system trees.
        """

        checkout = TestBase.locate_test_checkout()
        output = subprocess.check_output([os.path.join(checkout, "tools/tree-diff"), path1, path2])
        return json.loads(output)

    @staticmethod
    def have_fedmir() -> bool:
        """Check FedMir Availability

        We use a custom Fedora-Mirror ("FedMir") on our CI, which allows us to
        pre-fetch RPMs and thus avoid network latencies. This function checks
        for availability of that mirror.

        We use the port `8071` for FedMir. If that port can be connected to
        locally, we assume it runs FedMir.
        """

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # Use a 100ms timeout, more than enough for local connections.
            s.settimeout(0.1)
            try:
                r = s.connect_ex(('127.0.0.1', 8071))
                return r == 0
            except OSError:
                return False


class TestRuntime(TestBase):
    """Runtime Class for Tests

    This class extends `TestBase` with a managed osbuild store and executor.
    The store is prepopulated with common objects, so tests will automatically
    use the cached values.
    """

    _store = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._store = tempfile.TemporaryDirectory(dir="/var/tmp")
        cls.osbuild = OSBuild(external_store=cls._store.name)
        cls.populate_store(cls.osbuild)

    @classmethod
    def tearDownClass(cls):
        cls.osbuild = None
        cls._store.cleanup()
        super().tearDownClass()

    @classmethod
    def populate_store(cls, osb):
        if not cls.have_fedmir() or not cls.have_test_data():
            return

        # With FedMir available, we can build `fedora-fedmir.json`, a simply
        # pipeline that pulls in all RPMs we use in other manifests. This will
        # populate the sources-cache and make sure following builds can reuse
        # the sources.
        # For now, this also builds a throwaway image with all these RPMs
        # instaled. We might want to improve this to only download the sources,
        # but not build any image.
        with osb:
            osb.compile_file(os.path.join(cls.locate_test_data(),
                                          "manifests/fedora-fedmir.json"))


class OSBuild(contextlib.AbstractContextManager):
    """OSBuild Executor

    This class represents a context to execute osbuild. It provides a context
    manager, which while entered maintains a store and output directory. This
    allows running pipelines against a common setup and tear everything down
    when exiting.
    """

    _external_store = None

    _exitstack = None
    _storedir = None
    _outputdir = None

    def __init__(self, external_store=None):
        self._external_store = external_store

    def __enter__(self):
        self._exitstack = contextlib.ExitStack()
        with self._exitstack:
            # If the caller specified an external store, use it. Otherwise,
            # we create an empty, temporary store.
            if self._external_store:
                self._storedir = self._external_store
            else:
                store = tempfile.TemporaryDirectory(dir="/var/tmp")
                self._storedir = self._exitstack.enter_context(store)

            # Create a temporary output-directory for assembled artifacts.
            output = tempfile.TemporaryDirectory(dir="/var/tmp")
            self._outputdir = self._exitstack.enter_context(output)

            # Keep our ExitStack for `__exit__()`.
            self._exitstack = self._exitstack.pop_all()

        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        # Clean up our ExitStack.
        with self._exitstack:
            pass

        self._outputdir = None
        self._storedir = None
        self._exitstack = None

    @staticmethod
    def _print_result(code, data_stdout, data_stderr):
        print(f"osbuild failed with: {code}")
        try:
            json_stdout = json.loads(data_stdout)
            print("-- STDOUT (json) -----------------------")
            json.dump(json_stdout, sys.stdout, indent=2)
        except json.JSONDecodeError:
            print("-- STDOUT (raw) ------------------------")
            print(data_stdout)
        print("-- STDERR ------------------------------")
        print(data_stderr)
        print("-- END ---------------------------------")

    def compile(self, data_stdin, checkpoints=None):
        """Compile an Artifact

        This takes a manifest as `data_stdin`, executes the pipeline, and
        assembles the artifact. No intermediate steps are kept, unless you
        provide suitable checkpoints.

        The produced artifact (if any) is stored in the output directory. Use
        `map_output()` to temporarily map the file and get access. Note that
        the output directory becomes invalid when you leave the context-manager
        of this class.
        """

        cmd_args = []

        cmd_args += ["--json"]
        cmd_args += ["--libdir", "."]
        cmd_args += ["--output-directory", self._outputdir]
        cmd_args += ["--store", self._storedir]

        for c in (checkpoints or []):
            cmd_args += ["--checkpoint", c]

        # Spawn the `osbuild` executable, feed it the specified data on
        # `STDIN` and wait for completion. If we are interrupted, we always
        # wait for `osbuild` to shut down, so we can clean up its file-system
        # trees (they would trigger `EBUSY` if we didn't wait).
        try:
            p = subprocess.Popen(
                ["python3", "-m", "osbuild"] + cmd_args + ["-"],
                encoding="utf-8",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            data_stdout, data_stderr = p.communicate(data_stdin)
        except KeyboardInterrupt:
            p.wait()
            raise

        # If execution failed, print results to `STDOUT`.
        if p.returncode != 0:
            self._print_result(p.returncode, data_stdout, data_stderr)
            assert p.returncode == 0

    def compile_file(self, file_stdin, checkpoints=None):
        """Compile an Artifact

        This is similar to `compile()` but takes a file-path instead of raw
        data. This will read the specified file into memory and then pass it
        to `compile()`.
        """

        with open(file_stdin, "r") as f:
            data_stdin = f.read()
            return self.compile(data_stdin, checkpoints=checkpoints)

    @staticmethod
    def treeid_from_manifest(manifest_data):
        """Calculate Tree ID

        This takes an in-memory manifest, inspects it, and returns the ID of
        the final tree of the stage-array. This returns `None` if no stages
        are defined.
        """

        manifest_json = json.loads(manifest_data)
        manifest_pipeline = manifest_json.get("pipeline", {})
        manifest_sources = manifest_json.get("sources", {})

        manifest_parsed = osbuild.load(manifest_pipeline, manifest_sources)
        return manifest_parsed.tree_id

    @contextlib.contextmanager
    def map_object(self, obj):
        """Temporarily Map an Intermediate Object

        This takes a store-reference as input, looks it up in the current store
        and provides the file-path to this object back to the caller.
        """

        path = os.path.join(self._storedir, "refs", obj)
        assert os.access(path, os.R_OK)

        # Yield the path to the store-entry to the caller. This is implemented
        # as a context-manager so the caller does not retain the path for
        # later access.
        yield path

    @contextlib.contextmanager
    def map_output(self, filename):
        """Temporarily Map an Output Object

        This takes a filename (or relative path) and looks it up in the output
        directory. It then provides the absolute path to that file back to the
        caller.
        """

        path = os.path.join(self._outputdir, filename)
        assert os.access(path, os.R_OK)

        # Similar to `map_object()` we provide the path through a
        # context-manager so the caller does not retain the path.
        yield path
