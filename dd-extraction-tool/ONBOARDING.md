# Using dd-identify and dd-extract

**What it does.** Two commands. `dd-identify` reads every PDF page in a data room and lists the pages that are financial statements. `dd-extract` then pulls a fixed set of figures off those pages. Every figure carries the file and page it came from, so you can check it in seconds.

**Running it.** From `dd-extraction-tool/`, load your key, then run identify, then extract against the report it produced. The README has the exact commands. Expect roughly 2 seconds per page for identify and 4 for extract: a 500-page data room takes about 25 minutes.

**Before you use the output.** Read the `skipped` and `review` lists first, every time. `skipped` is what it could not read: scans, non-PDFs, failed calls. `review` is what needs your eye, including possible injected instructions. A page that fails one run often succeeds on a rerun, so a short `skipped` list is worth rerunning.

**Four things to know.**
- `confidence` is always "high". Ignore it.
- The same page under two filenames is reported twice, so don't sum blindly.
- `NOT_STATED` currency means the page names no currency.
- It checks nothing for consistency. Anomalies are still your job.
