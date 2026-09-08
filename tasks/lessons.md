# Engineering lessons

- Pass `encoding="utf-8"` to every text read/write, including temporary editing scripts. Windows defaults can corrupt non-ASCII source and documentation.
- Use a unique pytest `--basetemp` when the shared Windows temporary directory is inaccessible. Do not treat fixture permission errors as product failures.
- A SQLite connection context manages transactions, not connection lifetime. Close disposable connections explicitly so Windows can release or replace their files.
- Use single-quoted PowerShell here-strings for multiline Python and Markdown. Keep literal backticks and dollar signs out of interpolated shell strings.
- Validate GitHub-rendered Markdown by element type, allowing attributes such as `role` and `class`; literal opening-tag comparisons can falsely reject valid output.
