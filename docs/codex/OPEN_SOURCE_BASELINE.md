# Основа Codex Skills и заимствованные практики

Дата проверки источников: 2 сентября 2026 года.

## Официальная основа

### OpenAI Codex — Build skills

Источник: https://developers.openai.com/codex/build-skills

Использовано:

- repository skills в `.agents/skills`;
- обязательные `name` и `description`;
- progressive disclosure;
- один skill — одна задача;
- imperative workflow с явными входом и результатом;
- instruction-first подход;
- локальные `references/` для подробностей;
- проверка trigger prompts;
- optional `agents/openai.yaml`.

### OpenAI Codex — AGENTS.md

Источник: https://developers.openai.com/codex/agent-configuration/agents-md

Использовано:

- компактные постоянные правила в корневом `AGENTS.md`;
- возможность вложенных override-файлов;
- сохранение размера существенно ниже стандартных 32 KiB;
- lint-правила оставлены инструментам, а не размножены в review-инструкциях.

### Agent Skills specification

Источник: https://agentskills.io/specification

Использовано:

- ограничения имени и описания;
- `SKILL.md` существенно короче 500 строк;
- дополнительные материалы вынесены в небольшие `references/`;
- ссылки на ресурсы идут от корня skill;
- scripts применяются только для детерминированной работы.

## Популярные открытые ориентиры

### obra/superpowers

Источник: https://github.com/obra/superpowers

Взяты методические идеи:

- test-first для нового поведения и дефектов;
- системная отладка через доказуемые гипотезы;
- минимальное исправление первопричины;
- отдельный review;
- проверка командами до заявления о готовности.

### addyosmani/agent-skills

Источник: https://github.com/addyosmani/agent-skills

Взяты оси review:

- correctness;
- readability;
- architecture;
- security;
- performance;
- compatibility и качество tests.

## Что намеренно не сделано

- Сторонние skill-файлы и scripts не скопированы целиком.
- Внешние репозитории не устанавливаются автоматически.
- Навыки не получают дополнительные сетевые или системные полномочия.
- Общие универсальные инструкции адаптированы к contracts, trust boundaries и pipeline StructuraGuard.

Это снижает supply-chain риск и не превращает проект в набор конфликтующих инструкций.
