# ADR-0039: явное разрешение внешней LLM по HTTP

Статус: принят по требованию пользователя от 2026-09-28.

Host application может задать `OpenAICompatibleConfig.allow_insecure_http=True`
для внешнего сервера Chat Completions без TLS. Default `False` сохраняет прежнее
поведение: HTTP только на numeric loopback, остальные endpoints через HTTPS.
Ключ и данные при таком разрешении передаются открытым текстом; выбор должен
быть явно показан пользователю host-приложения.

Это не меняет locality, разрешённые классы данных, schema validation, запрет
redirects, credentials/query/fragment в URL и allowlist путей. Флаг — StrictBool,
перепроверяется provider и включён в fingerprint deployment. Secret fields
исключены из repr и safe serialization. Автоматическое ослабление политики
после сетевой ошибки не допускается.
