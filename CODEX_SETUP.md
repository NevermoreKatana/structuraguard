# Настройка Codex для StructuraGuard

## Что находится в наборе

- `AGENTS.md` — короткие постоянные правила проекта.
- `.agents/skills/` — узкие повторяемые workflows.
- `.codex/config.toml` — краткий вывод при достаточном reasoning.
- `docs/codex/` — минимальный контекст и индекс полного ТЗ.
- `scripts/` — проверка Skills и точечное извлечение разделов ТЗ.
- `agents/openai.yaml` внутри каждого skill — короткие отображаемые названия для интерфейсов Codex/ChatGPT.
- `codex-user-config/` — необязательные глобальные языковые настройки и профили economy/quality.

## Установка в репозиторий

Скопируй содержимое архива в корень Git-репозитория. Codex обнаруживает project skills в `.agents/skills` автоматически. Репозиторный `.codex/config.toml` применяется после того, как проект отмечен доверенным.

Проверка:

```bash
python scripts/validate_codex_pack.py
codex "Перечисли загруженные инструкции и доступные StructuraGuard skills"
```

В Codex CLI/IDE можно вызвать skill явно:

```text
$structuraguard-plan спланируй milestone M5
$structuraguard-parser добавь потоковый JSONL parser
$structuraguard-review проверь текущий diff
```

Или описать задачу обычным языком — короткие descriptions позволяют Codex подобрать skill автоматически.

## Как уменьшен расход токенов

1. Корневой `AGENTS.md` содержит только постоянные правила.
2. Полное ТЗ не читается автоматически.
3. В стартовый контекст попадают только имена и короткие descriptions Skills.
4. Полный `SKILL.md` загружается только после активации навыка.
5. Детальные checklists находятся в `references/` и читаются только при необходимости.
6. `.codex/config.toml` задаёт `model_verbosity = "low"` и concise reasoning summary.
7. Точечные команды и узкие тесты используются во время итераций; полный suite запускается перед завершением.

## Язык

- Ответы пользователю, документация, docstring и полезные комментарии — на русском.
- Идентификаторы, API, error codes и названия protocols — на английском.
- Комментарии объясняют причину, а не очевидную операцию.

## Необязательная глобальная настройка

Проектный `AGENTS.md` уже задаёт русский язык для StructuraGuard. Чтобы те же краткие ответы действовали во всех репозиториях, скопируй шаблон:

```bash
mkdir -p ~/.codex
cp codex-user-config/AGENTS.md ~/.codex/AGENTS.md
```

Профили конфигурации хранятся на пользовательском уровне:

```bash
cp codex-user-config/economy.config.toml ~/.codex/economy.config.toml
cp codex-user-config/quality.config.toml ~/.codex/quality.config.toml

# Обычная простая задача с минимальным выводом
codex --profile economy

# Архитектура, ИБ, сложная отладка или финальный review
codex --profile quality
```

Репозиторный `.codex/config.toml` остаётся сбалансированным вариантом по умолчанию. Профили нельзя надёжно задавать на project scope, поэтому они поставляются как шаблоны для `$CODEX_HOME`.

## Проверка качества навыков

```bash
python scripts/validate_codex_pack.py
```

Скрипт проверяет:

- YAML frontmatter;
- соответствие `name` имени каталога;
- допустимые символы и длину;
- наличие и длину `description`;
- дубликаты;
- размер `SKILL.md`;
- общий объём стартовых metadata;
- существование локальных ссылок на `references/` и skill-local `scripts/`;
- опасные команды и шаблоны в skill files.

## Рекомендуемый режим работы

Для крупной задачи:

```text
$structuraguard-plan → профильный skill → $structuraguard-tests → $structuraguard-review
```

Для дефекта:

```text
$structuraguard-debug → regression test → минимальный fix → $structuraguard-review
```

Для изменения на границе доверия:

```text
профильный skill + $structuraguard-security + $structuraguard-tests
```

## Открытые источники, использованные как ориентир

- OpenAI Codex: Build skills — https://developers.openai.com/codex/build-skills
- OpenAI Codex: AGENTS.md — https://developers.openai.com/codex/agent-configuration/agents-md
- Agent Skills specification — https://agentskills.io/specification
- OpenAI plugin examples — https://github.com/openai/plugins
- Superpowers — https://github.com/obra/superpowers
- Production-grade Agent Skills — https://github.com/addyosmani/agent-skills

Файлы набора написаны специально для StructuraGuard. Сторонние scripts и инструкции не копировались и не требуют установки внешних skill repositories.
