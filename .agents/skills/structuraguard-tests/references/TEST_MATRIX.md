# Test matrix

| Компонент | Unit | Contract | Integration | Property/Security |
|---|---:|---:|---:|---:|
| Domain DTO/invariants | ✓ |  |  | property |
| Parser | ✓ | ✓ | optional external parser | malformed/limits |
| Database adapter | ✓ graph/query build | ✓ | PostgreSQL | injection/drift |
| LLM provider | ✓ parser/errors | ✓ | opt-in | prompt/malformed |
| Mapper/score | ✓ |  | fixture catalog | permutations |
| Validator/normalizer | ✓ |  | DB constraints | property/locales |
| Loader | ✓ plan | ✓ | PostgreSQL transaction | rollback/idempotency |

## Качество теста

- Имя описывает условие и ожидаемое поведение.
- Arrange содержит только необходимые данные.
- Assertion проверяет публичный эффект и важный metadata/error code.
- Нет зависимости от порядка тестов, сети, текущего времени и случайного UUID.
- Fixture мала и предметно названа.
