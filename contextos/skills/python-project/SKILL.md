---
name: python-project
description: Structure, code and test a small Python project so every feature is verified with unittest. Use when the build target is Python.
---

# Python project

## Layout
```
<package>/__init__.py
<package>/<module>.py        one responsibility per module
tests/test_<module>.py       one test file per module
requirements.txt             only if a third-party package is truly needed
README.md                    how to install, run and test
```
Prefer the standard library. Every extra dependency is something the user must install.

## Tests: the definition of done
- Use `unittest` (stdlib), so tests run with no installs:
  `python -m unittest discover -s tests -v`
- Write the test for a feature together with the feature. A feature is done only
  when its tests pass when actually run, not when the code looks right.
- Test behaviour through the public function, including one edge case and one
  error case per feature.
- Tests must not touch the network or real user files.
- **Tests create their own input files.** Never read `test.txt`, `sample.csv` or any
  file that only exists on your machine or next to the test. Create inputs inside the
  test:
  ```python
  def setUp(self):
      self._tmp = tempfile.TemporaryDirectory()
      self.path = os.path.join(self._tmp.name, "sample.txt")
      with open(self.path, "w", encoding="utf-8") as f:
          f.write("one two two\n")
  def tearDown(self):
      self._tmp.cleanup()
  ```

## Code
- Functions small and named for what they do; type hints on public functions.
- Validate input at the boundary (CLI args, file contents, user input) and raise
  clear errors; don't sprinkle defensive checks inside.
- CLI: `argparse` with subcommands and a `main(argv=None)` that returns an exit code.
  Test the CLI by calling `main([...])` directly and capturing output with
  `contextlib.redirect_stdout(io.StringIO())` - not by launching a subprocess, which
  depends on which `python` is on PATH and is slow. If you must spawn a process, use
  `sys.executable`, never the bare word `python`.
- Never `eval`, `exec`, `pickle.loads` untrusted data, or `shell=True`.
- Read secrets from environment variables, never from source code.

## When a test fails
Read the full error, fix the cause (not the test), rerun. If the same fix fails
twice, re-read the code involved before trying again.
