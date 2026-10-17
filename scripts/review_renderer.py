"""Render topic reading documents from already classified source records."""

from dataclasses import dataclass
import os
import re
from urllib.parse import quote, unquote, urlsplit

from atomic_io import atomic_write_text, filesystem_path
from document_utils import (
    AUTO_GENERATED_REVIEW, IMAGE_TOKEN_RE, _normalize_image_target,
    _resolve_source_image_path, cleanup_generated_root,
    escape_markdown_link_label, make_markdown_link, normalize_rel_path,
    resolve_document_language, source_date_key, strip_markdown_target_title,
)
from review_content import select_review_groups
from review_model import build_review_model


@dataclass(frozen=True)
class ReviewPolicy:
    mode: str
    document_limit: int
    record_chars: int
    image_limit: int
    excerpt_limit: int = 0


def review_policy(category):
    if category[0] == "Unclassified":
        return ReviewPolicy("triage", 8000, 0, 0, 0)
    if len(category) == 1:
        return ReviewPolicy("navigation", 8000, 0, 0, 0)
    if len(category) == 2:
        return ReviewPolicy("overview", 12000, 800, 1, 5)
    return ReviewPolicy("detail", 120000, 3000, 3)

LOCAL_LINK = re.compile(
    r"(?<!!)\[([^\]\n]+)\]\((<[^>\n]+>(?:\s+[\"'][^\"'\n]*[\"'])?|(?:[^()\n]|\([^()\n]*\))+)\)"
)
WIKILINK = re.compile(r"(?<!!)\[\[([^\]\n]+)\]\]")


def _relative_link(review_path, target_path, label):
    return make_markdown_link(review_path, target_path, label)


def _rewrite_local_links(text, record, review_path, vault_dir):
    vault_dir = os.path.realpath(vault_dir)
    source_path = os.path.join(vault_dir, record["source_path"].replace("/", os.sep))
    source_dir = os.path.dirname(source_path)
    review_dir = os.path.dirname(os.path.realpath(os.path.join(vault_dir, review_path.replace("/", os.sep))))

    def image(match):
        raw_target = match.group(1) or match.group(3)
        target = _normalize_image_target(raw_target)
        if not target or urlsplit(target).scheme:
            return match.group(0)
        resolved = _resolve_source_image_path(raw_target, source_path, vault_dir)
        if not resolved:
            raise RuntimeError(f"Review image was not found: {record['source_path']}: {raw_target}")
        relative = normalize_rel_path(os.path.relpath(resolved, review_dir))
        alt = escape_markdown_link_label(match.group(2) or "")
        return f"![{alt}]({quote(relative, safe='/-._~')})"

    def resolve_markdown(target):
        path = unquote(target).split("#", 1)[0]
        if not path or urlsplit(path).scheme or os.path.isabs(path):
            return None
        for base in (source_dir, vault_dir):
            candidate = os.path.realpath(os.path.join(base, path))
            try:
                within_vault = os.path.commonpath((candidate, os.path.realpath(vault_dir))) == os.path.realpath(vault_dir)
            except ValueError:
                within_vault = False
            if within_vault and os.path.isfile(candidate):
                return normalize_rel_path(os.path.relpath(candidate, vault_dir))
        return None

    def markdown(match):
        raw_target = unquote(strip_markdown_target_title(match.group(2)))
        destination = resolve_markdown(raw_target)
        if not destination:
            return match.group(0)
        fragment = raw_target.split("#", 1)[1] if "#" in raw_target else ""
        url = _relative_url(review_path, destination)
        if fragment:
            url += "#" + quote(fragment, safe="-._~")
        return f"[{escape_markdown_link_label(match.group(1))}]({url})"

    def wiki(match):
        raw, _, label = match.group(1).partition("|")
        target = raw.split("#", 1)[0].strip()
        destination = resolve_markdown(target if target.lower().endswith(".md") else target + ".md")
        if not destination:
            return match.group(0)
        fragment = raw.split("#", 1)[1] if "#" in raw else ""
        url = _relative_url(review_path, destination)
        if fragment:
            url += "#" + quote(fragment, safe="-._~")
        return f"[{escape_markdown_link_label(label or target)}]({url})"

    rewritten = []
    in_fence = False
    fence_marker = ""
    for line in text.split("\n"):
        visible = re.sub(r"^\s*>\s?", "", line).lstrip()
        opening = re.match(r"^(`{3,}|~{3,})", visible)
        if opening:
            marker = opening.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker[0] == fence_marker[0] and len(marker) >= len(fence_marker):
                in_fence = False
            rewritten.append(line)
            continue
        if in_fence:
            rewritten.append(line)
            continue
        line = IMAGE_TOKEN_RE.sub(image, line)
        line = LOCAL_LINK.sub(markdown, line)
        rewritten.append(WIKILINK.sub(wiki, line))
    return "\n".join(rewritten)


def _record_links(record, review_path):
    subject = record["subject_documents"][0][0]
    if resolve_document_language() == "en":
        return (
            f"[Detail Document]({_relative_url(review_path, subject)}) · "
            f"[Source Log]({_relative_url(review_path, record['source_path'])})"
        )
    return (
        f"[상세 문서]({_relative_url(review_path, subject)}) · "
        f"[원본 일지]({_relative_url(review_path, record['source_path'])})"
    )


def _relative_url(from_path, target_path):
    source_dir = os.path.dirname(from_path) or "."
    return quote(os.path.relpath(target_path, source_dir).replace("\\", "/"), safe="/-._~")


def _undated_label():
    return "No date" if resolve_document_language() == "en" else "날짜 없음"


def _record_date(record):
    date = source_date_key(record["source_path"])
    return _undated_label() if date == "0000-00-00" else date


def _select_record(record, policy):
    return select_review_groups(record.get("review_blocks", ()), policy.record_chars, policy.image_limit)


def _render_record(record, review_path, vault_dir, selected, omitted):
    date = _record_date(record)
    title = record["title"]
    lines = [f"#### {date} · {title}\n\n"]
    current_heading = None
    previous_end = None
    for group in selected:
        if group.heading_path != current_heading and group.heading_path:
            lines.append(f"##### {group.heading_path[-1]}\n\n")
            current_heading = group.heading_path
        if previous_end is not None and group.start_line > previous_end + 2:
            lines.append("…\n\n")
        for block in group.blocks:
            text = block.text if block.kind == "code" else _rewrite_local_links(block.text, record, review_path, vault_dir)
            lines.append(text + "\n\n")
        previous_end = group.blocks[-1].end_line
    if omitted:
        omitted_prefix = "… Omitted content: " if resolve_document_language() == "en" else "… 생략된 내용: "
        lines.append(omitted_prefix + _record_links(record, review_path) + "\n\n")
    else:
        lines.append(_record_links(record, review_path) + "\n\n")
    return "".join(lines)


class _Budget:
    def __init__(self, limit, footer):
        self.limit = limit
        self.footer = footer
        self.parts = []
        self.length = 0

    def add(self, chunk, section_limit=None):
        if section_limit is not None and len(chunk) > section_limit:
            return False
        if self.length + len(chunk) + len(self.footer) > self.limit:
            return False
        self.parts.append(chunk)
        self.length += len(chunk)
        return True

    def render(self, footer):
        result = "".join(self.parts) + footer
        assert len(result) <= self.limit
        return result


def _folder_reference(vault_dir, review_path, category, records):
    lang = resolve_document_language()
    if records:
        sample = records[0]
        subject = os.path.dirname(sample["generated_path"])
        for _ in range(max(0, len(sample["category_path"]) - len(category))):
            subject = os.path.dirname(subject)
        subject = subject.replace("\\", "/")
    else:
        subject = os.path.join("Subject", *category).replace("\\", "/")
    absolute = os.path.join(vault_dir, subject)
    try:
        exists = os.path.isdir(absolute)
    except OSError:
        exists = False
    label = "Subject Folder" if lang == "en" else "Subject 폴더"
    if exists:
        reference = f"[{label}]({_relative_url(review_path, subject)}/)"
    else:
        reference = f"{subject}/"
    fallback = "Subject Folder (link omitted)" if lang == "en" else "Subject 폴더 (경로가 길어 링크 생략)"
    return reference if len(reference) <= 2000 else fallback


def _footer(vault_dir, review_path, category, records, missing, body=None):
    folder = _folder_reference(vault_dir, review_path, category, records)
    if resolve_document_language() == "en":
        counts = f"Shown excerpts {body} · Unshown {missing} · " if body is not None else f"Unshown entries {missing} · "
        return f"\n---\n{counts}See {folder} and child reviews for the rest.\n"
    counts = f"표시 본문 {body}건 · 본문 미표시 {missing}건 · " if body is not None else f"미표시 기록 {missing}건 · "
    return f"\n---\n{counts}나머지는 {folder} 및 하위 리뷰에서 확인하세요.\n"


def _header(category, title, period, source_count, child_count, entry_count, limit, footer):
    marker = AUTO_GENERATED_REVIEW + "\n"
    if resolve_document_language() == "en":
        candidates = (
            f"# {category[-1]} {title}\n\n**Category**: {' > '.join(category)}\n"
            f"**Period**: {period} · **Sources**: {source_count} · **Topics**: {child_count}\n"
            f"**Entries**: {entry_count}\n\n",
            f"# {category[-1][:80]} {title}\n\n**Entries**: {entry_count}\n\n",
            f"# {category[-1][:20]}\n\n",
            "# Review\n\n",
        )
    else:
        candidates = (
            f"# {category[-1]} {title}\n\n**Category**: {' > '.join(category)}\n"
            f"**기록 기간**: {period} · **원본**: {source_count}건 · **주제**: {child_count}개\n"
            f"**Entries**: {entry_count}\n\n",
            f"# {category[-1][:80]} {title}\n\n**Entries**: {entry_count}\n\n",
            f"# {category[-1][:20]}\n\n",
            "# 리뷰\n\n",
        )
    for candidate in candidates:
        if len(marker) + len(candidate) + len(footer) <= limit:
            return marker + candidate
    return marker


def _render_index(budget, child_paths, groups, review_paths, review_path, policy):
    if not child_paths:
        return
    lang = resolve_document_language()
    budget.add("## Topic Index\n\n" if lang == "en" else "## 주제 목차\n\n")
    index_length = 0
    listed_children = 0
    folder_link = "[All Reviews Folder](./)" if lang == "en" else "[전체 리뷰 폴더](./)"
    notice_template = (
        f"- Omitted child reviews: {{count}} · {folder_link}\n"
        if lang == "en"
        else f"- 목차에서 생략한 하위 리뷰 {{count}}개 · {folder_link}\n"
    )
    notice_reserve = len(notice_template.format(count=len(child_paths)))
    for child in child_paths[:30]:
        child_records = groups[child]
        if lang == "en":
            row = (f"- {_relative_link(review_path, review_paths[child], child[-1])} · "
                   f"{len(child_records)} entries · latest {_record_date(child_records[-1])}\n")
        else:
            row = (f"- {_relative_link(review_path, review_paths[child], child[-1])} · "
                   f"기록 {len(child_records)}건 · 최근 {_record_date(child_records[-1])}\n")
        section_cap = 3000 if policy.mode == "overview" else None
        if section_cap is not None and index_length + len(row) + notice_reserve > section_cap:
            break
        if budget.length + len(row) + notice_reserve + len(budget.footer) > budget.limit:
            break
        budget.add(row)
        index_length += len(row)
        listed_children += 1
    omitted_children = len(child_paths) - listed_children
    if omitted_children:
        budget.add(notice_template.format(count=omitted_children))
    budget.add("\n")


def _render_navigation(budget, records, category, review_path):
    shown = 0
    direct = [record for record in reversed(records) if category in record["matched_categories"]]
    if direct:
        budget.add("## Directly Classified Entries\n\n" if resolve_document_language() == "en" else "## 직접 분류된 기록\n\n")
        for record in direct[:5]:
            row = f"- {_record_date(record)} · {record['title']} · {_record_links(record, review_path)}\n"
            shown += budget.add(row)
    return shown


def _render_triage(budget, records, category, review_path):
    shown = 0
    lang = resolve_document_language()
    if len(category) > 1 and category[1] == "Missing Level 1":
        budget.add("Entries requiring Level 1 classification.\n\n" if lang == "en" else "Level 1 분류가 필요한 기록입니다.\n\n")
    budget.add("## Recent Entries\n\n" if lang == "en" else "## 최근 기록\n\n")
    source_label = "Source Log" if lang == "en" else "원본 일지"
    for record in reversed(records[:]):
        if shown >= 30:
            break
        row = f"- {_record_date(record)} · {record['title']} · {_relative_link(review_path, record['source_path'], source_label)}\n"
        shown += budget.add(row)
    return shown


def _render_excerpt_cards(budget, records, review_path, vault_dir, policy):
    shown = 0
    body_shown = 0
    for record in reversed(records):
        if policy.excerpt_limit and body_shown >= policy.excerpt_limit:
            break
        selected, omitted = _select_record(record, policy)
        if not selected and policy.mode == "overview":
            continue
        card = _render_record(record, review_path, vault_dir, selected, omitted)
        if budget.add(card):
            shown += 1
            body_shown += bool(selected)
    return shown, body_shown


def _render_overview(budget, records, review_path, vault_dir, policy):
    lang = resolve_document_language()
    budget.add("## Recent Entries\n\n" if lang == "en" else "## 최근 기록\n\n")
    shown, body_shown = _render_excerpt_cards(budget, records, review_path, vault_dir, policy)
    if body_shown == 0:
        budget.add("No displayable excerpts.\n\n" if lang == "en" else "표시 가능한 발췌가 없습니다.\n\n")
        for record in reversed(records[-5:]):
            row = f"- {_record_date(record)} · {_relative_link(review_path, record['source_path'], record['title'])}\n"
            budget.add(row)
    return shown, body_shown


def _render_detail(budget, records, review_path, vault_dir, policy):
    budget.add("## Entries\n\n" if resolve_document_language() == "en" else "## 기록\n\n")
    return _render_excerpt_cards(budget, records, review_path, vault_dir, policy)


def _render_category(vault_dir, category, model):
    review_path = model.review_paths[category]
    records = model.groups[category]
    child_paths = model.children[category]
    policy = review_policy(category)
    lang = resolve_document_language()
    if lang == "en":
        title = "Summary Review" if child_paths else "Review"
    else:
        title = "종합 리뷰" if child_paths else "리뷰"
    undated = _undated_label()
    dated = [_record_date(record) for record in records if _record_date(record) != undated]
    period = f"{min(dated)} ~ {max(dated)}" if dated else undated
    source_logs = {record["source_path"] for record in records}
    worst_footer = _footer(vault_dir, review_path, category, records, len(records),
                           len(records) if policy.mode == "overview" else None)
    budget = _Budget(policy.document_limit, worst_footer)
    budget.add(_header(category, title, period, len(source_logs),
                       len(child_paths), len(records), policy.document_limit, worst_footer))
    _render_index(budget, child_paths, model.groups, model.review_paths, review_path, policy)

    body_shown = None
    if policy.mode == "navigation":
        shown = _render_navigation(budget, records, category, review_path)
    elif policy.mode == "triage":
        shown = _render_triage(budget, records, category, review_path)
    elif policy.mode == "overview":
        shown, body_shown = _render_overview(budget, records, review_path, vault_dir, policy)
    else:
        shown, _body_shown = _render_detail(budget, records, review_path, vault_dir, policy)

    actual_footer = _footer(vault_dir, review_path, category, records, len(records) - shown,
                            body_shown)
    return budget.render(actual_footer)


def _save_reviews(vault_dir, review_dir_name, desired):
    for relative, content in desired.items():
        destination = os.path.join(vault_dir, relative)
        atomic_write_text(destination, content)
    cleanup_generated_root(vault_dir, review_dir_name, set(desired), AUTO_GENERATED_REVIEW)


def write_reviews(vault_dir, review_dir_name, generated_records, dry_run=False):
    model = build_review_model(generated_records, review_dir_name)
    desired = {model.review_paths[category]: _render_category(vault_dir, category, model)
               for category in sorted(model.groups)}
    if not dry_run:
        _save_reviews(vault_dir, review_dir_name, desired)
    return sorted(desired)
