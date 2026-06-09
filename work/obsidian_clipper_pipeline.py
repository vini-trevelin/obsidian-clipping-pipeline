from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import smtplib
import socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator


DEFAULT_CLIPPINGS_DIR = "Clippings"
DEFAULT_MAIN_NOTES_DIR = "01 - Main Notes"
DEFAULT_INSIGHTS_DIR = "Insights"
DEFAULT_STORAGE_DIR = "05 - Storage"
DEFAULT_LEGACY_SUMMARY_SUBDIR = "Summaries"
DEFAULT_DAILY_DIR = "00 - Notepad"
DEFAULT_TEMPLATES_DIR = "04 - Templates"
DEFAULT_INDEXES_DIR = "03 - Indexes"
DEFAULT_PROCESSED_DIRNAME = ".processed"
DEFAULT_LOCK_DIRNAME = "_pipeline_state"
DEFAULT_LOCK_FILE = "clipping_summary.lock.json"
DEFAULT_LOCK_MAX_AGE_HOURS = 12
DAILY_TEMPLATE_NAME = "{{date}}.md"
FULL_NOTE_TEMPLATE_NAME = "Full Note.md"
INSIGHTS_MARKER = "> [!NOTE] Insights"
DEFAULT_STATUS = "[[done]]"
DEFAULT_BASE_TAG = "Insights"
TAG_EXCLUSIONS = {"done", "insights"}


def candidate_vault_roots() -> list[Path]:
    home = Path.home()
    candidates: list[Path] = []
    env_override = os.getenv("OBSIDIAN_VAULT_ROOT")
    if env_override:
        candidates.append(Path(env_override).expanduser())

    system = platform.system().lower()
    if system == "windows":
        candidates.extend(
            [
                home / "iCloudDrive" / "iCloud~md~obsidian" / "main",
                home / "Apple" / "CloudDocs" / "iCloud~md~obsidian" / "main",
            ]
        )
    elif system == "darwin":
        candidates.extend(
            [
                home / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "main",
                home / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "main",
            ]
        )
    else:
        candidates.append(home / "iCloudDrive" / "iCloud~md~obsidian" / "main")

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def resolve_vault_root(vault_root_arg: str | None) -> Path:
    if vault_root_arg:
        return Path(vault_root_arg).expanduser()

    for candidate in candidate_vault_roots():
        if candidate.exists():
            return candidate

    checked = "\n".join(f"- {candidate}" for candidate in candidate_vault_roots())
    raise FileNotFoundError(
        "Could not detect the Obsidian iCloud vault automatically. "
        "Set OBSIDIAN_VAULT_ROOT or pass --vault-root explicitly. "
        f"Checked:\n{checked}"
    )


@dataclass(frozen=True)
class VaultPaths:
    vault_root: Path
    clippings_dir: Path
    processed_dir: Path
    main_notes_dir: Path
    insights_dir: Path
    storage_dir: Path
    daily_dir: Path
    templates_dir: Path
    indexes_dir: Path
    lock_path: Path


def build_paths(vault_root: Path) -> VaultPaths:
    clippings_dir = vault_root / DEFAULT_CLIPPINGS_DIR
    lock_dir = clippings_dir / DEFAULT_LOCK_DIRNAME
    return VaultPaths(
        vault_root=vault_root,
        clippings_dir=clippings_dir,
        processed_dir=clippings_dir / DEFAULT_PROCESSED_DIRNAME,
        main_notes_dir=vault_root / DEFAULT_MAIN_NOTES_DIR,
        insights_dir=vault_root / DEFAULT_MAIN_NOTES_DIR / DEFAULT_INSIGHTS_DIR,
        storage_dir=vault_root / DEFAULT_STORAGE_DIR,
        daily_dir=vault_root / DEFAULT_DAILY_DIR,
        templates_dir=vault_root / DEFAULT_TEMPLATES_DIR,
        indexes_dir=vault_root / DEFAULT_INDEXES_DIR,
        lock_path=lock_dir / DEFAULT_LOCK_FILE,
    )


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text

    lines = text.splitlines()
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break

    if end_idx is None:
        return {}, text

    frontmatter_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")
    data: dict[str, Any] = {}
    current_key: str | None = None

    for raw_line in frontmatter_lines:
        if not raw_line.strip():
            continue
        if raw_line.startswith("  - ") and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(strip_wrapping_quotes(raw_line[4:].strip()))
            continue
        if ":" not in raw_line:
            current_key = None
            continue

        key, value = raw_line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if not value:
            data[key] = []
            continue
        data[key] = strip_wrapping_quotes(value)

    return data, body


def strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def slugify(value: str) -> str:
    ascii_value = value.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").lower()
    return slug or "summary"


def normalize_for_match(value: str) -> str:
    ascii_value = value.encode("ascii", errors="ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_markdown_references(text: str) -> dict[str, list[dict[str, str]]]:
    links: list[dict[str, str]] = []
    images: list[dict[str, str]] = []
    embeds: list[dict[str, str]] = []

    for alt, url in re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", text):
        images.append({"label": alt.strip(), "url": url.strip()})
    for label, url in re.findall(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)", text):
        links.append({"label": label.strip(), "url": url.strip()})
    for tag, url in re.findall(r"<(img|video|source)[^>]+src=\"([^\"]+)\"", text, flags=re.IGNORECASE):
        embeds.append({"type": tag.lower(), "url": url.strip()})
    for raw_url in re.findall(r"(?<!\()https?://[^\s>)\]]+", text):
        if not any(item["url"] == raw_url for item in links):
            links.append({"label": "", "url": raw_url})

    return {"links": links, "images": images, "embeds": embeds}


def discover_pending(vault_root: Path) -> list[dict[str, Any]]:
    paths = build_paths(vault_root)
    pending: list[dict[str, Any]] = []
    if not paths.clippings_dir.exists():
        return pending

    for clip_path in sorted(paths.clippings_dir.rglob("*.md")):
        if paths.processed_dir in clip_path.parents:
            continue
        if clip_path.name.startswith("."):
            continue

        text = read_text(clip_path)
        frontmatter, body = parse_frontmatter(text)
        pending.append(
            {
                "path": str(clip_path),
                "relative_path": str(clip_path.relative_to(vault_root)).replace("\\", "/"),
                "title": frontmatter.get("title") or clip_path.stem,
                "source": frontmatter.get("source") or "",
                "created": frontmatter.get("created") or "",
                "body_preview": body[:1500],
                "body_char_count": len(body),
                "modified_at": datetime.fromtimestamp(clip_path.stat().st_mtime).isoformat(),
                "references": extract_markdown_references(body),
            }
        )
    return pending


def summary_relative_path(summary_path: Path, vault_root: Path) -> str:
    return str(summary_path.relative_to(vault_root)).replace("\\", "/")


def ensure_unique_path(candidate: Path) -> Path:
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        maybe = candidate.with_name(f"{stem}-{counter}{suffix}")
        if not maybe.exists():
            return maybe
        counter += 1


def build_summary_path(paths: VaultPaths, title: str) -> Path:
    paths.insights_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{slugify(title)}.md"
    return ensure_unique_path(paths.insights_dir / filename)


def ensure_daily_note(paths: VaultPaths, target_date: date) -> Path:
    daily_path = paths.daily_dir / f"{target_date.strftime('%Y%m%d')}.md"
    if daily_path.exists():
        return daily_path

    template_path = paths.templates_dir / DAILY_TEMPLATE_NAME
    if template_path.exists():
        template = read_text(template_path)
        rendered = template.replace("{{date}}", target_date.isoformat())
    else:
        rendered = f"## **Daily note : {target_date.isoformat()}**\n\n#daily\n"

    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily_path.write_text(rendered, encoding="utf-8")
    return daily_path


def insert_links_into_insights(daily_text: str, links: list[str]) -> str:
    if not links:
        return daily_text

    lines = daily_text.splitlines()
    inserted = False
    output: list[str] = []
    for idx, line in enumerate(lines):
        output.append(line)
        if line.strip() != INSIGHTS_MARKER:
            continue

        inserted = True
        existing_block: list[str] = []
        look_ahead = idx + 1
        while look_ahead < len(lines) and lines[look_ahead].startswith(">"):
            existing_block.append(lines[look_ahead])
            look_ahead += 1

        existing_text = "\n".join(existing_block)
        new_lines = [f"> - {link}" for link in links if link not in existing_text]
        if new_lines:
            output.extend(new_lines)

    if inserted:
        return "\n".join(output) + ("\n" if daily_text.endswith("\n") else "")

    suffix = "\n" if daily_text.endswith("\n") else "\n\n"
    appended = [INSIGHTS_MARKER, *[f"> - {link}" for link in links]]
    return daily_text + suffix + "\n".join(appended) + "\n"


def update_daily_note(paths: VaultPaths, target_date: date, links: list[str]) -> Path:
    daily_path = ensure_daily_note(paths, target_date)
    updated = insert_links_into_insights(read_text(daily_path), links)
    daily_path.write_text(updated, encoding="utf-8")
    return daily_path


def update_daily_links(paths: VaultPaths, old_relative_path: str, new_relative_path: str, title: str) -> int:
    replacements = 0
    old_link = f"[[{old_relative_path}|{title}]]"
    new_link = f"[[{new_relative_path}|{title}]]"
    for daily_path in paths.daily_dir.glob("*.md"):
        text = read_text(daily_path)
        if old_link not in text:
            continue
        updated = text.replace(old_link, new_link)
        daily_path.write_text(updated, encoding="utf-8")
        replacements += 1
    return replacements


def load_index_names(paths: VaultPaths) -> list[str]:
    if not paths.indexes_dir.exists():
        return []
    return sorted(path.stem for path in paths.indexes_dir.glob("*.md"))


def infer_tags(paths: VaultPaths, content: str, max_tags: int = 2) -> list[str]:
    normalized_content = f" {normalize_for_match(content)} "
    scored: list[tuple[int, str]] = []
    for index_name in load_index_names(paths):
        normalized_name = normalize_for_match(index_name)
        if not normalized_name or normalized_name in TAG_EXCLUSIONS:
            continue
        pattern = rf"(?<!\w){re.escape(normalized_name)}(?!\w)"
        matches = len(re.findall(pattern, normalized_content))
        if matches > 0:
            scored.append((matches, index_name))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return [name for _score, name in scored[:max_tags]]


def build_tag_links(inferred_tags: list[str]) -> str:
    tags = [DEFAULT_BASE_TAG, *[tag for tag in inferred_tags if normalize_for_match(tag) != normalize_for_match(DEFAULT_BASE_TAG)]]
    return " ".join(f"[[{tag}]]" for tag in tags)


def apply_template_metadata(template_text: str, generated_at: datetime, tag_links: str) -> str:
    rendered = (
        template_text.replace("{{date}}", generated_at.strftime("%Y-%m-%d"))
        .replace("{{time}}", generated_at.strftime("%H:%M"))
        .rstrip()
    )
    rendered = re.sub(r"^Status:[^\n]*$", f"Status: {DEFAULT_STATUS}", rendered, count=1, flags=re.MULTILINE)
    rendered = re.sub(r"^Tags:[^\n]*$", f"Tags: {tag_links}", rendered, count=1, flags=re.MULTILINE)
    if "Status:" not in rendered:
        rendered = f"{rendered}\n\nStatus: {DEFAULT_STATUS}"
    if "Tags:" not in rendered:
        rendered = f"{rendered}\n\nTags: {tag_links}"
    return rendered


def render_summary_note(
    template_text: str,
    summary_title: str,
    source_url: str,
    clip_relative_path: str,
    generated_at: datetime,
    summary_markdown: str,
    tag_links: str,
) -> str:
    header = apply_template_metadata(template_text, generated_at, tag_links)
    reference_marker = "\n# References"
    if reference_marker in header:
        prefix, _marker, _suffix = header.partition(reference_marker)
        header_prefix = prefix.rstrip()
        references_heading = "# References"
    else:
        header_prefix = header
        references_heading = "# References"

    sections = [
        header_prefix,
        "",
        f"# {summary_title}",
        "",
        f"Source: {source_url or 'N/A'}",
        f"Original clipping: [[{clip_relative_path}]]",
        "",
        summary_markdown.rstrip(),
        "",
        references_heading,
        f"- [[{clip_relative_path}]]",
    ]
    return "\n".join(section for section in sections if section is not None).rstrip() + "\n"


def move_to_processed(paths: VaultPaths, source_path: Path) -> Path:
    relative = source_path.relative_to(paths.clippings_dir)
    destination = paths.processed_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_path), str(destination))
    return destination


def maybe_send_email(subject: str, body: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM")
    smtp_to = os.getenv("SMTP_TO")
    if not all([smtp_host, smtp_port, smtp_username, smtp_password, smtp_from, smtp_to]):
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = smtp_to
    message.set_content(body)

    with smtplib.SMTP(smtp_host, int(smtp_port), timeout=30) as client:
        client.starttls()
        client.login(smtp_username, smtp_password)
        client.send_message(message)
    return True


def load_template(paths: VaultPaths) -> str:
    template_path = paths.templates_dir / FULL_NOTE_TEMPLATE_NAME
    if template_path.exists():
        return read_text(template_path)
    return "{{date}} {{time}}\n\nStatus:\n\nTags:\n\n---\n"


@contextmanager
def processing_lock(paths: VaultPaths) -> Iterator[None]:
    now = datetime.now()
    paths.lock_path.parent.mkdir(parents=True, exist_ok=True)
    if paths.lock_path.exists():
        try:
            payload = json.loads(read_text(paths.lock_path))
            created_at = datetime.fromisoformat(payload["created_at"])
            if now - created_at < timedelta(hours=DEFAULT_LOCK_MAX_AGE_HOURS):
                raise RuntimeError(f"Vault is already being processed by {payload.get('hostname', 'another host')}.")
        except Exception:
            pass

    lock_payload = {
        "created_at": now.isoformat(),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
    }
    paths.lock_path.write_text(json.dumps(lock_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        yield
    finally:
        if paths.lock_path.exists():
            paths.lock_path.unlink()


def apply_summary_payload(vault_root: Path, payload: dict[str, Any], send_email: bool) -> dict[str, Any]:
    paths = build_paths(vault_root)
    target_date = date.fromisoformat(payload["target_date"])
    generated_at = datetime.now()
    template_text = load_template(paths)

    written_items: list[dict[str, Any]] = []
    links_for_daily: list[str] = []

    with processing_lock(paths):
        for item in payload.get("items", []):
            source_path = Path(item["source_path"])
            original_relative = source_path.relative_to(paths.clippings_dir)
            processed_relative_path = str((paths.processed_dir / original_relative).relative_to(vault_root)).replace("\\", "/")
            summary_path = build_summary_path(paths, item["summary_title"])
            inferred_tags = item.get("inferred_tags") or infer_tags(
                paths,
                " ".join(
                    [
                        item.get("summary_title", ""),
                        item.get("summary_markdown", ""),
                        item.get("source_url", ""),
                    ]
                ),
            )
            note_text = render_summary_note(
                template_text=template_text,
                summary_title=item["summary_title"],
                source_url=item.get("source_url", ""),
                clip_relative_path=processed_relative_path,
                generated_at=generated_at,
                summary_markdown=item["summary_markdown"],
                tag_links=build_tag_links(inferred_tags),
            )
            summary_path.write_text(note_text, encoding="utf-8")
            moved_path = move_to_processed(paths, source_path)
            relative_summary = summary_relative_path(summary_path, vault_root)
            links_for_daily.append(f"[[{relative_summary}|{item['summary_title']}]]")
            written_items.append(
                {
                    "summary_path": str(summary_path),
                    "summary_relative_path": relative_summary,
                    "processed_path": str(moved_path),
                    "summary_title": item["summary_title"],
                    "inferred_tags": inferred_tags,
                }
            )

        daily_path = update_daily_note(paths, target_date, links_for_daily)

    email_sent = False
    if send_email and written_items:
        subject = f"[Obsidian Summary] {len(written_items)} clipping(s) processed"
        lines = ["New summaries created:"]
        for item in written_items:
            lines.append(f"- {item['summary_title']} ({item['summary_relative_path']})")
        email_sent = maybe_send_email(subject, "\n".join(lines))

    return {
        "target_date": target_date.isoformat(),
        "daily_note_path": str(daily_path),
        "items": written_items,
        "email_sent": email_sent,
    }


def parse_legacy_summary(text: str) -> dict[str, str]:
    title_match = re.search(r"^# (.+)$", text, flags=re.MULTILINE)
    source_match = re.search(r"^Source:\s*(.+)$", text, flags=re.MULTILINE)
    clipping_match = re.search(r"^Original clipping:\s*\[\[([^\]]+)\]\]", text, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Migrated Summary"
    source_url = source_match.group(1).strip() if source_match else ""
    clipping_path = clipping_match.group(1).strip() if clipping_match else ""

    body = text
    if title_match:
        body = text[text.index(title_match.group(0)) :]
        body = body.split("\n", 1)[1] if "\n" in body else ""
    body = re.sub(r"^Source:\s*.+$", "", body, flags=re.MULTILINE)
    body = re.sub(r"^Original clipping:\s*\[\[[^\]]+\]\]\s*$", "", body, flags=re.MULTILINE)
    body = re.sub(r"(?ms)\n# References\s*\n.*$", "", body).strip()

    return {
        "title": title,
        "source_url": source_url,
        "clip_relative_path": clipping_path,
        "summary_markdown": body,
    }


def legacy_summary_paths(paths: VaultPaths) -> list[Path]:
    return sorted(paths.storage_dir.glob(f"*/{DEFAULT_LEGACY_SUMMARY_SUBDIR}/*.md"))


def migrate_legacy_summaries(vault_root: Path) -> dict[str, Any]:
    paths = build_paths(vault_root)
    template_text = load_template(paths)
    generated_at = datetime.now()
    migrated: list[dict[str, Any]] = []

    with processing_lock(paths):
        for old_path in legacy_summary_paths(paths):
            legacy = parse_legacy_summary(read_text(old_path))
            inferred_tags = infer_tags(paths, f"{legacy['title']} {legacy['summary_markdown']} {legacy['source_url']}")
            new_path = build_summary_path(paths, legacy["title"])
            new_text = render_summary_note(
                template_text=template_text,
                summary_title=legacy["title"],
                source_url=legacy["source_url"],
                clip_relative_path=legacy["clip_relative_path"],
                generated_at=generated_at,
                summary_markdown=legacy["summary_markdown"],
                tag_links=build_tag_links(inferred_tags),
            )
            new_path.write_text(new_text, encoding="utf-8")
            old_relative = summary_relative_path(old_path, vault_root)
            new_relative = summary_relative_path(new_path, vault_root)
            replacements = update_daily_links(paths, old_relative, new_relative, legacy["title"])
            old_path.unlink()
            migrated.append(
                {
                    "old_path": str(old_path),
                    "new_path": str(new_path),
                    "title": legacy["title"],
                    "daily_links_updated": replacements,
                    "inferred_tags": inferred_tags,
                }
            )

    return {"migrated": migrated}


def emit_json(data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def cmd_discover(args: argparse.Namespace) -> int:
    pending = discover_pending(Path(args.vault_root))
    emit_json({"pending": pending})
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    payload_path = Path(args.payload)
    payload = json.loads(read_text(payload_path))
    result = apply_summary_payload(Path(args.vault_root), payload, send_email=args.send_email)
    emit_json(result)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    result = migrate_legacy_summaries(Path(args.vault_root))
    emit_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic helpers for Obsidian clipping summaries.")
    parser.add_argument("--vault-root", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover", help="List pending clipping markdown files.")
    discover_parser.set_defaults(func=cmd_discover)

    apply_parser = subparsers.add_parser("apply", help="Apply AI-generated summaries into the vault.")
    apply_parser.add_argument("--payload", required=True, help="Path to a JSON payload with generated summaries.")
    apply_parser.add_argument("--send-email", action="store_true", help="Send an email notification if SMTP is configured.")
    apply_parser.set_defaults(func=cmd_apply)

    migrate_parser = subparsers.add_parser("migrate", help="Move legacy summaries into the new Insights pattern.")
    migrate_parser.set_defaults(func=cmd_migrate)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.vault_root = str(resolve_vault_root(args.vault_root))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
