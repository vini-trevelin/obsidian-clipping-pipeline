# obsidian-clipping-pipeline

Local pipeline for processing Obsidian Web Clipper notes into cleaned insight notes.

## What it does

- reads pending notes from `Clippings/`
- generates cleaned summary notes under `01 - Main Notes/Insights/`
- stores Insight notes under `01 - Main Notes/Insights/YYYY/YYYY-MM-DD/`
- writes `Status: [[done]]`
- writes `Tags: [[Insights]]` plus up to 2 inferred tags from `03 - Indexes/`
- adds links to the current daily note `Insights` block
- moves processed source clippings to `Clippings/.processed/`

## Cross-platform vault detection

The script auto-detects the Obsidian iCloud vault in:

- Windows: `~/iCloudDrive/iCloud~md~obsidian/main`
- macOS: `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/main`

If your vault lives elsewhere, set `OBSIDIAN_VAULT_ROOT` or pass `--vault-root`.

## Commands

List pending clippings:

```bash
python work/obsidian_clipper_pipeline.py discover
```

Apply an AI-generated payload:

```bash
python work/obsidian_clipper_pipeline.py apply --payload work/generated_summary_payload.json --send-email
```

Migrate legacy summaries from the old storage layout:

```bash
python work/obsidian_clipper_pipeline.py migrate
```

Reorganize existing flat Insight notes into the dated layout:

```bash
python work/obsidian_clipper_pipeline.py reorganize
```

Run tests:

```bash
python -m unittest work/test_obsidian_clipper_pipeline.py
```

## Environment

Optional email notification variables:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_TO`

Optional vault override:

- `OBSIDIAN_VAULT_ROOT`

## Operating rule for Windows + macOS

- Keep the same repo on both machines.
- Allow only one active scheduler at a time.
- The script also writes a vault-level lock file to avoid concurrent processing by mistake.
