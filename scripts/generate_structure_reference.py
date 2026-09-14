"""Собирает docs/project_structure_reference.md из текущего состояния кода.

Раньше документ вели руками, и он отставал молча: 191 модуль из 669, ни одного
из 27 платёжных миксинов, пять несуществующих записей в корне и «103 метода» у
класса, где их 393. Навигационная справка, которой верят и которая врёт, хуже
её отсутствия.

Берутся только файлы под контролем версий — иначе документ зависел бы от того,
что валяется в рабочей копии (`.env`, кеши, локальные заметки), и проверка
свежести срабатывала бы у каждого по-своему.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path()
OUTPUT = ROOT / 'docs' / 'project_structure_reference.md'

HEADER = """# База по структуре проекта

> Документ собирается автоматически: `make docs-structure`.
> Правки руками затрутся — меняйте генератор `scripts/generate_structure_reference.py`.

Перечислены файлы под контролем версий. Для Python-модулей указаны классы
(с числом методов) и функции верхнего уровня; имена, начинающиеся с
подчёркивания, опущены как внутренние.
"""

MAX_HEADING_LEVEL = 6


def tracked_paths() -> list[Path]:
    """Файлы проекта: отслеживаемые плюс новые, которые git не игнорирует.

    Брать только индекс (`git ls-files`) нельзя: новый файл попадал бы в
    документ лишь ПОСЛЕ коммита, поэтому локально проверка была зелёной, а в CI
    сразу после того же коммита краснела. Ровно так и вышло. `--others
    --exclude-standard` добавляет ещё не добавленные в индекс файлы, не считая
    игнорируемых, — состояние индекса перестаёт влиять на результат, а мусор из
    рабочей копии по-прежнему отсекается через .gitignore.
    """
    git = shutil.which('git')
    if git is None:
        raise RuntimeError('git не найден в PATH — список файлов взять неоткуда')

    # Аргументы фиксированы, путь к git взят из PATH через which — внешнего
    # ввода здесь нет.
    result = subprocess.run(  # noqa: S603
        [git, 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return sorted({Path(name) for name in result.stdout.split('\0') if name})


def _first_docstring_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node)
    if not doc:
        return ''
    return doc.strip().splitlines()[0].strip()


def describe_module(path: Path) -> tuple[str, str]:
    """Строки «Классы:» и «Функции:» для модуля."""
    try:
        tree = ast.parse((ROOT / path).read_text(encoding='utf-8'), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as error:
        return f'не разобран ({error.__class__.__name__})', 'не разобраны'

    classes = []
    functions = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.name.startswith('_'):
                continue
            methods = sum(1 for item in node.body if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef))
            classes.append(f'`{node.name}` ({methods} методов)' if methods else f'`{node.name}`')
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith('_'):
                continue
            summary = _first_docstring_line(node)
            functions.append(f'`{node.name}` — {summary}' if summary else f'`{node.name}`')

    return ', '.join(classes) or 'нет', ', '.join(functions) or 'нет'


def _children(paths: list[Path], directory: Path) -> tuple[list[Path], list[Path]]:
    """Прямые потомки директории: (файлы, поддиректории)."""
    files: list[Path] = []
    subdirs: set[Path] = set()
    depth = len(directory.parts)

    for path in paths:
        if directory != REPO_ROOT and not path.is_relative_to(directory):
            continue
        parts = path.parts
        if len(parts) == depth + 1:
            files.append(path)
        elif len(parts) > depth + 1:
            subdirs.add(Path(*parts[: depth + 1]))

    return sorted(files), sorted(subdirs)


def render_entries(paths: list[Path], directory: Path) -> list[str]:
    files, subdirs = _children(paths, directory)
    lines: list[str] = []

    for entry in sorted(files + subdirs):
        if entry in subdirs:
            lines.append(f'- `{entry.as_posix()}/`')
            continue
        if entry.suffix == '.py':
            classes, functions = describe_module(entry)
            lines.append(f'- `{entry.as_posix()}` — Python-модуль')
            lines.append(f'  Классы: {classes}')
            lines.append(f'  Функции: {functions}')
        else:
            lines.append(f'- `{entry.as_posix()}` — файл')

    return lines


def render(paths: list[Path]) -> str:
    lines = [HEADER, '## Общая структура корня', '']
    lines.extend(render_entries(paths, REPO_ROOT))

    _, top_level = _children(paths, REPO_ROOT)
    queue = list(top_level)
    while queue:
        directory = queue.pop(0)
        level = min(MAX_HEADING_LEVEL, 1 + len(directory.parts))
        lines.extend(['', f'{"#" * level} {directory.as_posix()}', ''])
        lines.extend(render_entries(paths, directory))

        _, subdirs = _children(paths, directory)
        queue = subdirs + queue

    return '\n'.join(lines).rstrip('\n') + '\n'


def build() -> str:
    return render(tracked_paths())


def main() -> int:
    content = build()
    if '--check' in sys.argv:
        current = OUTPUT.read_text(encoding='utf-8') if OUTPUT.exists() else ''
        if current != content:
            print(f'{OUTPUT.relative_to(ROOT)} устарел — выполните `make docs-structure`')
            return 1
        print(f'{OUTPUT.relative_to(ROOT)} актуален')
        return 0

    OUTPUT.write_text(content, encoding='utf-8')
    print(f'{OUTPUT.relative_to(ROOT)}: {len(content.splitlines())} строк')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
