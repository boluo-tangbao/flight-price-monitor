# Python environment

Use the existing Conda environment `airline_monitoring` for every local project
test, diagnostic, and flight query.

Its interpreter is `D:\Anaconda_envs\envs\airline_monitoring\python.exe`.
Invoke this interpreter explicitly, or activate `airline_monitoring` and verify
`sys.executable` before running Python.

Install project dependencies into this environment. All test libraries must
resolve from this environment. Remove temporary dependency overrides such as
`work/test-deps` from `PYTHONPATH` when testing.
