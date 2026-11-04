# Newsroom RSS Collector — Stage 1

Collects RSS articles from 20 feeds every hour and writes them to the `News Database` Google spreadsheet.

## Google Sheet layout

The worksheet tab defaults to `NEWS DATABASE` (the spreadsheet file itself can be named `News Database`).

| Column | Field | Stage 1 behaviour |
| --- | --- | --- |
| A | ID | Assigns a unique article ID (`N00001`, etc.) |
| B | Status | Sets every new article to `NEW` |
| C | Date | Publication date/time in Asia/Kolkata |
| D | Category | `Gaming` or `Indian Music` |
| E | Title | RSS article title |
| F | Source | Outlet name |
| G | URL | Article URL |
| H | Summary | RSS summary/description |
| I | Verdict | Never touched |
| J | Reason | Never touched |
| K | Score | Never touched |
| L | Story ID | Shared ID for articles about the same underlying story (`S00001`, etc.) |
| M | Outlets | Number of distinct outlets represented by that Story ID |

## Important grouping behaviour

- Exact duplicate URLs are skipped.
- Near-identical headlines from the same outlet are treated as reposts and skipped.
- Similar articles from different outlets are **not** deduplicated. They remain separate rows and share a Story ID.
- `Outlets` is a numeric count of distinct outlets covering that Story ID.
- If a new article joins an existing story, the Outlets count is refreshed on the existing rows in that story group.
- Stage 1 writes columns A:H and L:M only. It does not write to Verdict, Reason, or Score (I:K).

## Status handoff to Stage 2

Stage 1 assigns `NEW`. Stage 2 should select only `NEW` rows, mark them `PROCESSING` when claimed, and mark them `PROCESSED` after successful completion. Stage 1 does not change existing statuses.

## GitHub repository setup

Add these repository secrets:

- `GOOGLE_CREDENTIALS` — the full service-account JSON key contents.
- `SHEET_ID` — the ID from the Google Sheet URL.

Optional repository variable:

- `SHEET_TAB` — worksheet tab name. Defaults to `NEWS DATABASE`.

The Google service-account email must have Editor access to the spreadsheet.

## Run

GitHub Actions runs hourly at minute 17 UTC and can also be started manually from the Actions tab using **Run workflow**.

GitHub scheduled workflows are best-effort and may occasionally start late. They do not guarantee exact minute-level execution.
