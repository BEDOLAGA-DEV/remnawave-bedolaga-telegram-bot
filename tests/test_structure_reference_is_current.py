"""docs/project_structure_reference.md обязан совпадать с кодом.

Документ вели руками, и он отстал незаметно: 191 модуль из 669, ни одного из
27 платёжных миксинов, «103 метода» у класса с 393 методами и пять записей про
файлы, которых давно нет. Справка, которой верят и которая врёт, вреднее её
отсутствия — а заметить расхождение глазами в трёх тысячах строк невозможно.

Теперь документ собирается генератором, и эта проверка следит, чтобы его не
забыли пересобрать.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from scripts.generate_structure_reference import OUTPUT, REPO_ROOT, build, render_entries, tracked_paths


REBUILD_HINT = 'выполните `make docs-structure` и закоммитьте результат'


def test_document_matches_the_code() -> None:
    assert OUTPUT.exists(), f'{OUTPUT.name} отсутствует — {REBUILD_HINT}'

    expected = build()
    actual = OUTPUT.read_text(encoding='utf-8')

    if actual == expected:
        return

    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    first_diff = next(
        (i for i, (a, b) in enumerate(zip(actual_lines, expected_lines, strict=False)) if a != b),
        min(len(actual_lines), len(expected_lines)),
    )
    pytest.fail(
        f'{OUTPUT.name} разошёлся с кодом — {REBUILD_HINT}.\n'
        f'Первое расхождение на строке {first_diff + 1}:\n'
        f'  в документе: {actual_lines[first_diff] if first_diff < len(actual_lines) else "(конец файла)"}\n'
        f'  ожидается:   {expected_lines[first_diff] if first_diff < len(expected_lines) else "(конец файла)"}'
    )


def test_only_tracked_files_are_listed() -> None:
    """Документ не должен зависеть от мусора в рабочей копии.

    Иначе `.env`, кеши и локальные заметки попадали бы в него по-разному у
    каждого, и проверка выше срабатывала бы случайным образом.
    """
    tracked = {path.as_posix() for path in tracked_paths()}
    document = OUTPUT.read_text(encoding='utf-8')

    for stray in ('.env`', 'venv/`', '__pycache__/`', '.ruff_cache/`'):
        assert stray not in document, f'в документе оказался неотслеживаемый путь: {stray}'

    assert 'app/config.py' in tracked
    assert '`app/config.py`' in document


def test_generator_is_deterministic() -> None:
    """Два запуска подряд дают один и тот же текст."""
    assert build() == build()


def test_payment_mixins_are_documented() -> None:
    """Ровно та дыра, из-за которой всё это затевалось."""
    document = OUTPUT.read_text(encoding='utf-8')
    mixins = sorted(Path('app/services/payment').glob('*.py'))
    missing = [m.name for m in mixins if m.name != '__init__.py' and f'`{m.as_posix()}`' not in document]
    assert not missing, f'платёжные миксины не попали в документ: {missing}'


def test_order_does_not_depend_on_platform_case_folding() -> None:
    """WindowsPath сравнивается без учёта регистра, PosixPath — байтово.

    Без явного ключа сортировки пересборка на Windows ставила `.github/ISSUE_TEMPLATE`
    после `dependabot.yml`, и `--check` в CI краснел. Порядок обязан быть один везде,
    поэтому на вход идут именно Windows-пути.
    """
    lines = render_entries([PureWindowsPath('Zeta.md'), PureWindowsPath('alpha.md')], REPO_ROOT)
    assert lines == ['- `Zeta.md` — файл', '- `alpha.md` — файл']


@pytest.mark.parametrize('tracked_instructions', [False, True])
@pytest.mark.parametrize('relative_path', ['AGENTS.md', 'app/AGENTS.md'])
def test_local_instructions_do_not_change_checkout_structure(
    tmp_path, monkeypatch, tracked_instructions, relative_path
):
    """Local instructions stay local; new source files and committed instructions remain visible."""
    import shutil
    import subprocess

    from scripts import generate_structure_reference as generator

    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', '-q', str(tmp_path)], check=True)  # noqa: S603 — isolated fixture
    instructions = tmp_path / relative_path
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_text('Local instructions\n')
    (tmp_path / 'new_module.py').write_text('def new_function(): pass\n')
    if tracked_instructions:
        subprocess.run([git, 'add', relative_path], cwd=tmp_path, check=True)  # noqa: S603 — fixed test paths
    monkeypatch.setattr(generator, 'ROOT', tmp_path)

    paths = {path.as_posix() for path in generator.tracked_paths()}

    assert 'new_module.py' in paths
    assert (relative_path in paths) == tracked_instructions
