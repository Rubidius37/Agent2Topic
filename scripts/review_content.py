"""Preserve source Markdown blocks for local topic reading documents."""

from dataclasses import dataclass
import re


HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)$")
LIST = re.compile(r"^([ \t]*)(?:[-*+]\s+|\d+[.)]\s+)")
TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
IMAGE = re.compile(
    r"!\[\[([^\]]+)\]\]|!\[([^\]\n]*)\]\((<[^>\n]+>(?:\s+[\"'][^\"'\n]*[\"'])?|(?:[^()\n]|\([^()\n]*\))+)\)"
)
SOURCE_ID = re.compile(r"^\s*<!--\s*(?:[\w-]+-)?source-id\s*:\s*[a-fA-F0-9]{8,64}\s*-->\s*$", re.I)
CATEGORY = re.compile(r"^\s*(?:Category|분류)\s*:", re.I)


@dataclass(frozen=True)
class ReviewBlock:
    kind: str
    text: str
    source_path: str
    source_id: str
    start_line: int
    end_line: int
    heading_path: tuple
    level: int = 0
    title: str = ""
    language: str = ""
    images: tuple = ()


@dataclass(frozen=True)
class ReviewGroup:
    heading_path: tuple
    blocks: tuple

    @property
    def text(self):
        return "\n\n".join(block.text for block in self.blocks)

    @property
    def images(self):
        return tuple(image for block in self.blocks for image in block.images)

    @property
    def start_line(self):
        return self.blocks[0].start_line


def _image_targets(text):
    targets = []
    fence = None
    for line in text.splitlines():
        visible = re.sub(r"^\s*>\s?", "", line).lstrip()
        marker = FENCE.match(visible)
        if marker:
            current = marker.group(1)
            if fence is None:
                fence = current
            elif current[0] == fence[0] and len(current) >= len(fence) and not marker.group(2).strip():
                fence = None
            continue
        if fence is None:
            targets.extend(match.group(1) or match.group(3) for match in IMAGE.finditer(line))
    return tuple(targets)


def extract_review_blocks(content, source_path, source_id, start_line=1, root_title=None):
    """Parse only review content; source-unit classification is left untouched."""
    lines = content.splitlines()
    blocks = []
    headings = []
    root_heading = None
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.strip():
            index += 1
            continue
        if SOURCE_ID.fullmatch(raw) or CATEGORY.match(raw):
            index += 1
            continue

        start = index
        heading = HEADING.match(raw)
        fence = FENCE.match(raw)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            if not blocks and root_title == title:
                root_heading = (level, title)
                index += 1
                continue
            kind = "heading"
            index += 1
        elif fence:
            kind = "code"
            marker = fence.group(1)
            index += 1
            while index < len(lines):
                closing = FENCE.match(lines[index])
                index += 1
                if closing and closing.group(1)[0] == marker[0] and len(closing.group(1)) >= len(marker) and not closing.group(2).strip():
                    break
        elif raw.lstrip().startswith(">"):
            kind = "quote"
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                index += 1
        elif LIST.match(raw):
            kind = "list"
            index += 1
            while index < len(lines):
                current = lines[index]
                if LIST.match(current):
                    index += 1
                elif current.strip() and current[:1].isspace() and not HEADING.match(current):
                    index += 1
                elif not current.strip() and index + 1 < len(lines) and (
                    LIST.match(lines[index + 1]) or
                    (lines[index + 1].strip() and lines[index + 1][:1].isspace())
                ):
                    index += 1
                else:
                    break
        elif (raw.lstrip().startswith("|") and index + 1 < len(lines)
              and TABLE_DIVIDER.match(lines[index + 1])):
            kind = "table"
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                index += 1
        elif IMAGE.fullmatch(raw.strip()):
            kind = "image"
            index += 1
        else:
            kind = "paragraph"
            index += 1
            while index < len(lines) and lines[index].strip():
                current = lines[index]
                if (HEADING.match(current) or FENCE.match(current)
                        or current.lstrip().startswith(">") or LIST.match(current)
                        or IMAGE.fullmatch(current.strip())
                        or SOURCE_ID.fullmatch(current) or CATEGORY.match(current)):
                    break
                index += 1

        text = "\n".join(lines[start:index])
        visible_headings = headings[1:] if headings and headings[0] == root_heading else headings
        path = tuple(title for _level, title in visible_headings)
        blocks.append(ReviewBlock(
            kind, text, source_path, source_id, start_line + start,
            start_line + index - 1, path,
            len(heading.group(1)) if heading else 0,
            heading.group(2).strip() if heading else "",
            fence.group(2).strip().split()[0] if fence and fence.group(2).strip() else "",
            _image_targets(text) if kind != "code" else (),
        ))
    return blocks


def group_review_blocks(blocks):
    """Join only adjacent structural relationships within one heading section."""
    grouped = []
    pending = []
    current_path = ()

    def flush():
        if pending:
            grouped.append(ReviewGroup(current_path, tuple(pending)))
            pending.clear()

    for block in blocks:
        if block.kind == "heading":
            flush()
            current_path = block.heading_path
            continue
        if pending:
            previous = pending[-1]
            same_section = block.heading_path == current_path
            adjacent = block.start_line - previous.end_line <= 2
            intro_with_structure = previous.kind == "paragraph" and block.kind in {"list", "table", "code"}
            image_with_caption = (
                (previous.kind == "image" and block.kind == "image")
                or (previous.kind == "image" and block.kind == "paragraph" and len(block.text) <= 120)
                or (previous.kind == "paragraph" and len(previous.text) <= 120 and block.kind == "image")
            )
            if not (same_section and adjacent and (intro_with_structure or image_with_caption)):
                flush()
        current_path = block.heading_path
        pending.append(block)
    flush()
    return grouped


def select_review_groups(blocks, target_chars, image_limit, code_limit=650, table_limit=900):
    """Round-robin over headings, then restore source order for display."""
    groups = group_review_blocks(blocks)
    sections = {}
    for group in groups:
        sections.setdefault(group.heading_path, []).append(group)
    selected = []
    skipped = False
    used_chars = 0
    used_images = 0
    while any(sections.values()):
        progressed = False
        for path in sections:
            while sections[path]:
                group = sections[path].pop(0)
                if any(block.kind == "code" and len(block.text) > code_limit for block in group.blocks):
                    skipped = True
                    continue
                if any(block.kind == "table" and len(block.text) > table_limit for block in group.blocks):
                    skipped = True
                    continue
                if len(group.text) > target_chars:
                    skipped = True
                    continue
                remaining_images = image_limit - used_images
                if len(group.images) > remaining_images:
                    if remaining_images <= 0:
                        skipped = True
                        continue
                    # Keep the image nearest a following short caption.
                    caption_after = group.blocks[-1].kind == "paragraph"
                    images = [block for block in group.blocks if block.images]
                    if any(len(block.images) > remaining_images for block in images):
                        skipped = True
                        continue
                    chosen = images[-remaining_images:] if caption_after else images[:remaining_images]
                    group = ReviewGroup(group.heading_path, tuple(
                        block for block in group.blocks if not block.images or block in chosen
                    ))
                    skipped = True
                    if len(group.images) > remaining_images or len(group.text) > target_chars:
                        continue
                if selected and used_chars + len(group.text) > target_chars:
                    skipped = True
                    continue
                selected.append(group)
                used_chars += len(group.text)
                used_images += len(group.images)
                progressed = True
                break
        if not progressed or used_chars >= target_chars:
            break
    return sorted(selected, key=lambda group: group.start_line), skipped or len(selected) < len(groups)
