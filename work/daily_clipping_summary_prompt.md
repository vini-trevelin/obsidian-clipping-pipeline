Process pending Obsidian clippings from the local Obsidian iCloud vault on the current machine.

Workflow:
1. Run `python work/obsidian_clipper_pipeline.py discover`.
2. If there are pending clipping files, read each pending clipping and produce one note per clipping in English.
3. The summary should remove garbage from the clipping, keep the real ideas, and preserve useful references from the clip. Do not restrict yourself to plain text only. If the clipping contains meaningful images, graphs, embeds, or external links, surface them inside the note when possible.
4. Use this section structure exactly:
   - `## Summary`
   - `## Key Points`
   - `## Why It Matters`
   - `## Where I Can Apply This`
   - `## Follow Up`
   - `## Related`
   - `## Takeaway`
5. Create `work/generated_summary_payload.json` using this schema:
   - `target_date`: today's date in `YYYY-MM-DD`
   - `items`: array of objects with `source_path`, `source_url`, `summary_title`, `summary_markdown`, and optionally `inferred_tags`
6. Run `python work/obsidian_clipper_pipeline.py apply --payload "work/generated_summary_payload.json" --send-email`.
7. Verify that:
   - summary notes were written under `01 - Main Notes/Insights`
   - today's daily note under `00 - Notepad` has links added inside the `> [!NOTE] Insights` block
   - original clipping files were moved into `Clippings/.processed`
   - each note uses `Status: [[done]]`
   - each note uses `Tags: [[Insights]]` plus up to 2 inferred tags

Constraints:
- Do not process files already inside `Clippings/.processed`
- Preserve the user's vault structure
- Keep file edits surgical
- If SMTP environment variables are missing, continue without failing the run
- Prefer the deterministic helper to infer tags from `03 - Indexes` when the payload does not specify them
- If autodetection fails, set `OBSIDIAN_VAULT_ROOT` before running the script
