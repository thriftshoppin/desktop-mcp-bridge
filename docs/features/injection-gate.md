# Feature: injection gate and policy gates

Text returned from the screen, files, clipboard, window titles and commands is labelled untrusted and scanned for instruction-injection signatures; a strong match withholds the content, logs it and locks the bridge until the user types `y` in the feed window. Policy gates (best effort, not a boundary): process denylist, protected paths, hidden-execution, network-download and input-bypass patterns, password-field refusal, `win+l` blocked. Limits: regex signatures only, no OCR of images. Tests: `tests/test_policy.py`, `tests/test_misc.py`.
