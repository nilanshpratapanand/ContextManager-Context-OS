"""Main-channel auto-update: every start installs the newest commit on main."""
import io, pathlib, sys, tempfile, unittest, zipfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from contextos import update as U

SHA1, SHA2 = "a" * 40, "b" * 40


def zip_of(files, top="repo-main"):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(f"{top}/{k}", v)
    return b.getvalue()


class MainChannel(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self._orig = (U.ROOT, U.latest_commit, U._get, U._requirements_hash)
        U.ROOT = self.root
        U._requirements_hash = lambda: ""

    def tearDown(self):
        U.ROOT, U.latest_commit, U._get, U._requirements_hash = self._orig

    def test_installs_new_commit_and_records_it(self):
        U.latest_commit = lambda repo, branch="main": SHA1
        U._get = lambda url, timeout=5.0, limit=0: zip_of({"contextos/server.py": "v1"})
        self.assertEqual(U.run_main("o/r"), U.UPDATED)
        self.assertEqual((self.root / "contextos/server.py").read_text(), "v1")
        self.assertEqual(U.installed_commit(self.root), SHA1)

    def test_same_commit_does_nothing(self):
        U.latest_commit = lambda repo, branch="main": SHA1
        U._get = lambda url, timeout=5.0, limit=0: zip_of({"contextos/server.py": "v1"})
        U.run_main("o/r")
        called = []
        U._get = lambda *a, **k: called.append(1)
        self.assertEqual(U.run_main("o/r"), 0); self.assertEqual(called, [])

    def test_next_commit_updates_files_and_keeps_user_data(self):
        U.latest_commit = lambda repo, branch="main": SHA1
        U._get = lambda url, timeout=5.0, limit=0: zip_of({"contextos/server.py": "v1"})
        U.run_main("o/r")
        (self.root / ".env").write_text("GROQ_API_KEY=mine")
        U.latest_commit = lambda repo, branch="main": SHA2
        U._get = lambda url, timeout=5.0, limit=0: zip_of({"contextos/server.py": "v2", ".env": "evil"})
        self.assertEqual(U.run_main("o/r"), U.UPDATED)
        self.assertEqual((self.root / "contextos/server.py").read_text(), "v2")
        self.assertEqual((self.root / ".env").read_text(), "GROQ_API_KEY=mine")

    def test_offline_is_silent(self):
        U.latest_commit = lambda repo, branch="main": None
        self.assertEqual(U.run_main("o/r"), 0)

    def test_bad_archive_keeps_running(self):
        U.latest_commit = lambda repo, branch="main": SHA1
        U._get = lambda url, timeout=5.0, limit=0: zip_of({"README.md": "not contextos"})
        self.assertEqual(U.run_main("o/r"), 0)

    def test_check_only_reports_without_installing(self):
        U.latest_commit = lambda repo, branch="main": SHA1
        called = []
        U._get = lambda *a, **k: called.append(1)
        self.assertEqual(U.run_main("o/r", check_only=True), 0); self.assertEqual(called, [])

    def test_release_channel_still_available(self):
        import os
        os.environ["CONTEXTOS_UPDATE_CHANNEL"] = "release"
        try:
            seen = []
            orig = U.latest_release
            U.latest_release = lambda repo: seen.append(repo) or None
            self.assertEqual(U.run(), 0); self.assertTrue(seen)
        finally:
            U.latest_release = orig; os.environ.pop("CONTEXTOS_UPDATE_CHANNEL")


if __name__ == "__main__":
    unittest.main()
