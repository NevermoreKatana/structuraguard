# Отчёт проверки Codex-набора

Дата проверки: 2 сентября 2026 года.

## Результат

```text
OK: skills=10, errors=0, warnings=0
```

- Skills: 10.
- Размер корневого `AGENTS.md`: 7187 байт при стандартном лимите 32 KiB.
- Стартовые `name + description`: 2117 символов при бюджете списка Skills до 8000 символов.
- Все 10 файлов `agents/openai.yaml` успешно разобраны YAML parser при сборке.
- Python scripts скомпилированы без синтаксических ошибок.
- Извлечение разделов не путает `FR-003` и `NFR-003`.

Проверенный результат выборки:

```text
## FR-003. Реестр парсеров
## M3. Parser Registry
```

## Команды повторной проверки

```bash
python scripts/validate_codex_pack.py
python -m py_compile scripts/*.py
python scripts/extract_spec_sections.py "FR-003" "M3. Parser Registry"
```
