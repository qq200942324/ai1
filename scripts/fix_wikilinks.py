"""Fix short-name wikilinks in concept/ecology/synthesis/topic pages."""
import re, sys
from pathlib import Path

# Build comprehensive file map: short_name -> full_relative_path
file_map = {}

# Course summaries
course_dir = Path('2-wiki/summaries/课程专题')
for f in course_dir.glob('*.md'):
    name = f.stem
    path = f'2-wiki/summaries/课程专题/{name}'
    file_map[name] = path
    if name.endswith(' - 摘要'):
        file_map[name[:-5].rstrip()] = path  # ' - 摘要' = 5 chars
    if name.endswith('. - 摘要'):
        file_map[name[:-6].rstrip()] = path  # '. - 摘要' = 6 chars

# Monthly reviews & jiege
for d in ['月度复盘', '杰哥复盘']:
    dd = Path(f'2-wiki/summaries/{d}')
    if dd.exists():
        for f in dd.glob('*.md'):
            name = f.stem
            file_map[name] = f'2-wiki/summaries/{d}/{name}'

# Syntheses, topics, ecology, data
for base in ['2-wiki/syntheses', '2-wiki/topics', '2-wiki/ecology', '2-wiki/data']:
    for f in Path(base).rglob('*.md'):
        name = f.stem
        rel = str(f).replace('\\', '/')
        file_map[name] = rel

# Known concept names (don't touch these short names)
concept_names = {f.stem for f in Path('2-wiki/concepts').glob('*.md')}

print(f'File map: {len(file_map)} entries')

# Scan and fix all relevant wiki pages
dirs_to_fix = [
    Path('2-wiki/concepts'),
    Path('2-wiki/ecology'),
    Path('2-wiki/syntheses'),
    Path('2-wiki/topics'),
]
fixed_count = 0
fixes_detail = []

for d in dirs_to_fix:
    if not d.exists():
        continue
    for md_file in d.rglob('*.md'):
        content = md_file.read_text(encoding='utf-8')
        orig = content

        def replace_link(m):
            full = m.group(0)
            inner = m.group(1)
            target = inner.split('|')[0].split('#')[0].strip()

            if '/' in target:
                return full
            if target in concept_names:
                return full
            if target in file_map:
                new_path = file_map[target]
                if '|' in inner:
                    display = inner.split('|', 1)[1]
                    return f'[[{new_path}|{display}]]'
                else:
                    return f'[[{new_path}|{target}]]'
            return full

        content = re.sub(r'\[\[([^\]]+)\]\]', replace_link, content)

        if content != orig:
            fixed_count += 1
            fixes_detail.append(str(md_file))
            md_file.write_text(content, encoding='utf-8')

print(f'\nFixed {fixed_count} files:')
for f in fixes_detail:
    print(f'  {f}')
