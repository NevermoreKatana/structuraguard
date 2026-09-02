# Диагностический чек-лист

- Дефект воспроизводится детерминированно либо отмечена нестабильность.
- Известен первый момент нарушения инварианта.
- Логи не содержат secrets или raw restricted data.
- Проверены input boundaries, locale/encoding, batch boundaries и schema drift.
- Для async проверены cancellation, timeout, shared state и blocking calls.
- Для БД проверены transaction scope, isolation, FK order и rollback.
- Для LLM проверены raw response, parser, capability mismatch и provider error normalization.
- Regression test проверяет поведение, а не внутреннюю строку реализации.
