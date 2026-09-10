# Индекс ТЗ для экономного чтения

Полный источник требований: `StructuraGuard_SDK_Technical_Specification.md`.

Не загружай документ целиком. Найди и прочитай только перечисленные разделы.

| Работа | Разделы ТЗ |
|---|---|
| Каркас SDK | `0. Паспорт проекта`, `1. Концепция проекта`, `2. Что является результатом`, `3. Цель и задачи`, `21. Нефункциональные требования`, `22. Выбранный стек`, `23. Публичный API`, `27. Структура репозитория`, `M1`, `33. Критерии приемки` |
| Contracts и DTO | `FR-012`, `9.4`, `14. Mapping Plan`, `16. Валидация`, `19.1`, `24. Результат работы`, `25. Статусы`, `M2` |
| Parser registry/plugins | 5, FR-001–FR-003, NFR-004, M3 |
| TXT/LOG/CSV/JSON | FR-004–FR-006, FR-012–FR-013, NFR-006, M4 |
| XML/HTML/YAML | FR-007–FR-008, 20.5–20.7, M4 |
| XLSX/PDF/DOCX | FR-009–FR-011, 20.13, M4 |
| Structural profiling и ParsePlan | FR-012.1–FR-012.2, FR-014, 20.9, 20.13, M5 |
| Database Inspector | 9, 10, 13, M7 |
| Source Profiler | FR-013, 11.4–11.6, 12, M6 |
| Deterministic Mapper | 10–14, M7 |
| LLM layer | 11.8–11.9, 12, 14, 19, 20.2, 20.10–20.11, M8 |
| MappingPlan validator | 14.1, 16, 18, 20.3–20.4, M9 |
| Validation engine | 15–16, M10 |
| Staging/loader | 17–18, 20.3–20.4, M11 |
| Security | 20, 30.4, 32.5, M12 |
| Demo application | 2, 23, 28, M13 |
| Evaluation | 30–33, M14 |

## Команды

```bash
# Список разделов
python scripts/extract_spec_sections.py --list

# Один или несколько разделов по подстроке заголовка
python scripts/extract_spec_sections.py "9.3. Что получает" "9.6. Граф"

# Поиск без вывода всего документа
rg -n '^#{1,3} .*Mapping Plan' StructuraGuard_SDK_Technical_Specification.md
```
