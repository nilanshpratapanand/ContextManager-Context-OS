---
name: security-review
description: Review code for security vulnerabilities (OWASP Top 10 style) and fix them. Use in the security phase of a build or when asked to audit code.
---

# Security review

Start with `security_scan`, then read the flagged lines and anything that handles
input. A scanner finding is a lead, not a verdict: confirm it before changing code,
and look for what the rules can't see.

## Check, in order
1. **Injection.** SQL built from strings (use `?` parameters), shell commands built from
   input (`subprocess` with a list, never `shell=True`), `eval`/`exec`, template injection.
2. **Secrets.** Keys, passwords or tokens in code or committed config. Move them to
   environment variables and add the file to `.gitignore`.
3. **Path traversal.** User-supplied file names joined to a folder. Resolve the path and
   check it stays inside the allowed folder.
4. **Unsafe deserialisation.** `pickle`, `yaml.load` without `SafeLoader`, `marshal` on
   data from outside.
5. **Web apps.** Output escaped (XSS), CSRF protection on state-changing requests,
   no debug mode in production, cookies `HttpOnly` + `Secure`, bind to 127.0.0.1 unless
   it must be public.
6. **Crypto.** `secrets` not `random` for tokens; no MD5/SHA-1 for passwords (use
   `hashlib.scrypt` or bcrypt); keep TLS verification on.
7. **Dependencies.** Pin versions; run `pip-audit` if it's available.
8. **Errors.** No stack traces or internal details shown to users.

## Fixing
Fix the root cause with the smallest change, add a test that proves the fix (for
example, that `../../etc/passwd` is rejected), and rerun the whole test suite.

## Report
For each issue: file:line, severity (high/medium/low), what an attacker could do, and
what was changed. Say plainly what was not checked.
