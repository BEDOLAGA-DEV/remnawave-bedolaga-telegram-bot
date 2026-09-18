// Проверка описания PR в main перед выпуском.
//
// Выпуск собирает release-please из сообщения коммита, который получается при
// слиянии PR в main. При «Squash and merge» GitHub склеивает в это сообщение
// все коммиты ветки, и одна пара вложенных скобок в любом старом коммите роняет
// разбор целиком: выпуск молча не создаётся. Единственное, что release-please
// читает вместо сообщения, — блок BEGIN_COMMIT_OVERRIDE … END_COMMIT_OVERRIDE
// в описании PR. Здесь он проверяется тем же разборщиком и по тем же правилам
// разбиения, что у release-please: запись от записи отделяется пустой строкой.
//
// Запуск: PR_BODY="$(…)" node scripts/check_release_pr_override.mjs
// или node scripts/check_release_pr_override.mjs path/to/body.md

import { readFileSync } from 'node:fs';
import { parser } from '@conventional-commits/parser';

const BEGIN = 'BEGIN_COMMIT_OVERRIDE';
const END = 'END_COMMIT_OVERRIDE';
// Типы, по которым release-please создаёт выпуск (см. release-please-config.json:
// hidden-типы chore/ci/test/build/style выпуска не дают).
const RELEASE_TYPES = new Set(['feat', 'fix', 'perf', 'refactor', 'docs']);
// То же регулярное выражение, что в splitMessages у release-please.
const SPLIT = /\r?\n\r?\n(?=(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(?:\(.*?\))?: )/;

function readBody() {
  const path = process.argv[2];
  if (path) return readFileSync(path, 'utf8');
  return process.env.PR_BODY ?? '';
}

function extractOverride(body) {
  if (!body.includes(BEGIN)) return null;
  return (body.split(BEGIN)[1] || '').split(END)[0].trim();
}

function entryType(ast) {
  const summary = ast.children.find((node) => node.type === 'summary');
  return summary?.children.find((node) => node.type === 'type')?.value ?? '';
}

function checkEntries(override) {
  const entries = override.split(SPLIT).filter(Boolean);
  const problems = [];
  let releaseEntries = 0;
  for (const entry of entries) {
    const header = entry.split('\n')[0];
    try {
      const type = entryType(parser(entry));
      if (RELEASE_TYPES.has(type)) releaseEntries += 1;
      console.log(`ok   [${type}] ${header}`);
    } catch (error) {
      problems.push(`не разбирается: «${header}» — ${error.message}`);
    }
  }
  if (releaseEntries === 0) {
    problems.push('нет ни одной записи feat/fix/perf/refactor/docs — release-please не создаст выпуск');
  }
  return problems;
}

const body = readBody();
const override = extractOverride(body);
if (override === null || override === '') {
  console.error(
    `В описании PR нет блока ${BEGIN} … ${END}.\n` +
      'Без него release-please читает сообщение сжатого коммита, куда GitHub складывает все коммиты ветки,\n' +
      'и любая пара вложенных скобок в старом коммите молча срывает выпуск.\n' +
      'Добавьте в конец описания блок: по одной строке «feat: …» / «fix: …» на изменение, между строками — пустая строка.',
  );
  process.exit(1);
}

const problems = checkEntries(override);
if (problems.length > 0) {
  for (const problem of problems) console.error(`ошибка: ${problem}`);
  process.exit(1);
}
console.log('Блок переопределения для release-please в порядке.');
