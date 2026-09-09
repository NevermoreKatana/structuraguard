# M4: security review текущего diff

Дата: 2026-09-09. Scope: tracked diff и новые untracked файлы текущего M4 A–F,
включая contract/registry changes, parser adapters, optional dependencies и tests.
Существующие рабочие изменения не удалялись. Это не review будущих DB/LLM modules
и не сертификация caller deployment. Основание: план M4, ADR 0004–0007,
parser security sections 20.5–20.7/20.13 и текущий запрос пользователя.

## Результат

Найдены и исправлены High SG-M4-SEC-01 и Medium SG-M4-SEC-02. Critical не найдено.
Подтверждение ограничено code review и приведёнными regression tests; отсутствие
других findings не означает отсутствие неизвестных native/backend vulnerabilities.

## SG-M4-SEC-01 — High: утечка response data через HTTP diagnostics

- Location исправления: `packages/structuraguard/src/structuraguard/parsers/_tika_http.py:33`
  (`_transport`; до исправления — обычный `AsyncHTTPTransport` на строке 28).
- Attacker-controlled input: status reason phrase, headers и malformed HTTP bytes
  Tika endpoint/proxy. Предусловие: включён optional Tika и INFO/DEBUG logs HTTP stack.
- Путь эксплуатации: server возвращает secret/PII в reason, `Set-Cookie` либо
  malformed header. HTTPcore сериализует response tuple/exception на DEBUG ещё
  до SDK error handling; HTTPX пишет reason на INFO. Typed exception redaction
  не удаляет уже отправленные LogRecords.
- Влияние: secret/PII или поддельный diagnostic text попадают в logs/collectors
  с другими readers и retention. Raw body не нужен для воспроизведения.
- Минимальное исправление: request-local trace callback удаляет `.complete` и
  `.failed` diagnostic payload; transport убирает reason extension до HTTPX logger.
  Фактические headers/body, TLS verification, timeout, errors и limits сохраняются.
  `.started` kwargs не трогаются: backend использует их для TCP/TLS вызовов.
  Глобальные logger levels/filters/handlers не изменяются.
- Regression: `tests/security/parsers/test_tika_transport_logging.py::test_http_diagnostics_do_not_log_response_secrets`
  — два real HTTP loopback cases: valid headers/reason и malformed header.
  До исправления `2 failed`, canary виден в HTTPcore и HTTPX logs; после `2 passed`.
  Тест также проверяет сохранение diagnostics без canary и logger configuration.
- Compatibility: `httpcore==1.0.9` закреплён в optional `tika`/`all`, поскольку
  используется проверенный порядок callback перед logging. Версия уже была в lock;
  новые packages не устанавливались. `test_tika_trace_redaction_backend_version_is_pinned`
  закрепляет constraint, старые exact extras/union/import checks не ослаблены.

Использованы документированные [trace/reason extensions HTTPX](https://www.python-httpx.org/advanced/extensions/)
и проверен [порядок в httpcore 1.0.9](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_trace.py).

## SG-M4-SEC-02 — Medium: format allowlist проверял другой read

- Location: `packages/structuraguard/src/structuraguard/parsers/_tika_http.py:147`;
  передача expected signature — `parsers/tika.py:421`.
- Предусловие: mutable/некорректный SourceReader либо замена backing file между
  probe и spooling. PUBLIC approval для конечного hash уже выдан caller;
  подделка этого approval проблемой не допускается.
- Путь эксплуатации: при allowlist только RTF reader возвращает RTF signature
  для probe, затем PostScript/PDF bytes, соответствующие одобренному SHA-256.
  Раньше SHA preflight проходил, но формат upload оставался основанным на probe.
- Влияние: обход format restriction и отправка непредусмотренного формата в Tika;
  специализированные core format controls могли быть обойдены этим fallback.
  Это не обход PUBLIC/secret approval и не доказательство RCE в Tika.
- Исправление: сигнатура ожидаемого allowlisted media type проверяется на том же
  temporary snapshot, что SHA и upload, до создания client/transport. Отказ —
  `SECURITY_INPUT_REJECTED`, reason `tika_snapshot_format`, без HTTP запроса.
- Regression: `tests/security/parsers/test_tika_security.py::test_upload_rechecks_format_on_the_approved_snapshot`
  — PostScript/core PDF × chunk sizes 1/7/4096. До `6 failed`, после `6 passed`;
  fake server не получает requests. Existing SHA mismatch tests сохранены.

Все пути `tests/...` относятся к `packages/structuraguard/`.

## Проверенные угрозы и controls

| Угроза | Применимость / проверенное поведение |
|---|---|
| Attacker-controlled input | Все bytes, names, metadata и HTTP response недоверенные. Bounded SourceReader, strict DTO revalidation, content evidence, SG-M4-SEC-02. Tests: selection, malformed corpus, stream integrity. |
| Resource exhaustion | Byte/line/field/record/depth/node/batch caps; ограниченный spool, ZIP directory/member/ratio checks; document deadline/RSS/CPU controls. Tests: security suites A–F и document boundaries. Полный peak/performance corpus не закрыт. |
| Parser exploit / unsafe deserialization | YAML SafeLoader используется для events, без object construction; aliases не разворачиваются; worker protocol — JSON, не pickle. Native PDF не считается OS sandbox; strict mode fail-closed. |
| XXE / DTD / network | XML/OOXML/Tika XHTML используют defused parser с forbid DTD/entities/external; tests XXE/Billion Laughs. HTML не запускает JS и не загружает resources. Единственная разрешённая сеть — opt-in Tika endpoint. |
| Prompt injection / excessive agency | Source instructions остаются raw values. Нет LLM/DB/tools authority, ParsePlan или semantic naming в parsers. Проверяется static suite и inert payload fixtures. |
| PII / secrets leakage | Tika требует source-bound PUBLIC + external secret review; SHA/canary preflight до egress. Errors redacted; SG-M4-SEC-01 закрывает transport logs. Canary detector не заменяет caller DLP. |
| SQL / identifier injection | В текущем diff нет SQL execution path. Имена таблиц/листов/колонок — physical data, не identifiers для SQL. DB adapter не изменён. |
| DB allowlist / denylist bypass | DB inspection/load не входят в M4 и не вызываются. Отсутствие DB imports/ports проверено; это не validation будущего loader allowlist. |
| Schema drift | Проверены schema-version compatibility, строгие DTO, parser/source identity, continuation и canonical fingerprints. DB schema drift неприменим: catalog/mapping отсутствуют в этом execution path. |
| Logs / audit | Raw document values не логируются парсерами. HTTP diagnostics regression — SG-M4-SEC-01. Audit subsystem не изменён, raw extracted output не считается audit-safe. |
| Path traversal / temp files | ZIP members не извлекаются на диск; paths/symlinks/duplicates/external relationships отклоняются. Snapshot — TemporaryDirectory/TemporaryFile, известное имя внутри private directory; timeout/cancellation cleanup tests. |
| Supply chain | См. проверенные версии ниже. Core не получает HTTP/doc extras, нет JAR auto-download или browser engine. Import/package gates и exact metadata tests сохранены. |

## Supply-chain evidence

В установленном parser окружении проверены версии и license metadata:

| Package | Version | License |
|---|---|---|
| charset-normalizer | 3.5.1 | MIT |
| defusedxml | 0.7.1 | PSFL |
| PyYAML | 6.0.3 | MIT |
| HTTPX | 0.28.1 | BSD-3-Clause |
| httpcore | 1.0.9 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| PyMuPDF | 1.28.2 | AGPL/commercial |
| openpyxl | 3.1.5 | MIT |
| python-docx | 1.2.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |

На дату review запрос `POST https://api.osv.dev/v1/querybatch` для этих десяти
package/version пар вернул `{"results":[{},{},{},{},{},{},{},{},{},{}]}`:
известные advisories для выбранных версий не возвращены. В запросе были только
публичные package names/versions, без проекта, исходников и secrets.
Это не полный SBOM/native audit, не проверка всех разрешённых version ranges и
не гарантия отсутствия zero-days. Сверены upstream security pages
[charset-normalizer](https://github.com/jawah/charset_normalizer/security),
[defusedxml](https://github.com/tiran/defusedxml/security),
[PyYAML](https://github.com/yaml/pyyaml/security),
[HTTPX](https://github.com/encode/httpx/security),
[httpcore](https://github.com/encode/httpcore/security).

Обязательная новая core dependency M4 — charset-normalizer для encoding confidence.
Остальные parser backends optional; httpcore ранее был transitive dependency HTTPX,
теперь pin явно выражает security compatibility constraint. BSD license проверена
по [upstream файлу](https://github.com/encode/httpcore/blob/1.0.9/LICENSE.md).
PyMuPDF license compatibility остаётся обязанностью embedding project.
Известный [CVE-2026-3029](https://github.com/advisories/GHSA-cxqh-p2w9-fmr7)
относится к embedded-file extraction CLI старых версий: этот путь не вызывается,
attachments запрещены и установленная версия не входит в affected range.

## Команды и результаты

- `.venv/bin/pytest -q packages/structuraguard/tests/security/parsers/test_tika_security.py -k upload_rechecks_format`
  — сначала 6 failed, после исправления 6 passed.
- `.venv/bin/pytest -q packages/structuraguard/tests/security/parsers/test_tika_transport_logging.py --tb=short`
  — сначала 2 failed, после исправления 2 passed.
- `uv lock --offline`; `uv sync --offline --locked --all-packages --group dev --group docs`
  — успешно, resolved 100 packages, обновлена только editable metadata SDK.
- Узкий pytest для Tika unit/property/security/integration и packaging metadata/verifier:
  `92 passed in 2.82s`.
- `.venv/bin/mypy` — no issues in 130 source files.

`make check test-integration test-security` — exit code 0:

- Lock/Ruff format (132 files)/lint — успешно.
- Strict mypy — no issues in 130 source files.
- Полный pytest — успешно. Отдельный компактный повтор `.venv/bin/pytest -q`:
  `1559 passed, 5 warnings in 52.68s`.
- Strict MkDocs, offline wheel/sdist/rebuild и isolated base import — успешно,
  `distribution verification OK`.
- Integration — `11 passed, 1548 deselected, 5 warnings in 2.31s`.
- Security — `207 passed in 15.37s`.
- `git diff --check` — exit code 0.

Существующие пять PyMuPDF SWIG deprecation warnings не подавлены. Старые
результаты аудита приёмки не подменяются результатами этого review.

## Изменённые этим review файлы

- `packages/structuraguard/src/structuraguard/parsers/_tika_http.py`, `tika.py`:
  только snapshot signature и per-request diagnostic redaction.
- `packages/structuraguard/tests/security/parsers/test_tika_security.py`,
  новый `test_tika_transport_logging.py`: восемь exploit/regression cases.
- `packages/structuraguard/pyproject.toml`, `uv.lock`: optional httpcore pin,
  без нового core dependency или обновления версий lock.
- `packages/structuraguard/tests/packaging/test_metadata.py`,
  `scripts/verify_distribution.py`: строгий новый dependency contract и pin test.
- Этот отчёт, ADR 0007, `docs/public-api.md`, `docs/plans/M04_technical_parsers.md`,
  `docs/codex/PROJECT_STATE.md`, `mkdocs.yml`: findings, guarantees и evidence.

Иные рабочие изменения M4 относятся к предыдущим задачам и сохранены.

## Остаточные риски и непроверенные сценарии

- Полный native exploit/fuzz corpus, measured peak RSS/CPU и все N±1 сочетания
  limits не запускались; bounded fixtures не доказывают абсолютную memory safety.
- Document worker Linux/macOS не является sandbox M12; `strict_mode=True` отказывает.
  macOS RSS sampling допускает overshoot между samples.
- Реальный Tika/JVM, DLP, DNS rebinding и network/container isolation caller не
  проверены: доступен только fake deployment. Нет external Tika/paid LLM calls.
- HTTPcore pin требует сопровождения при security updates. Trace redaction
  проверена для текущего HTTP/1.1 transport; logging custom transport caller
  вне guarantees SDK. HTTPS/TLS инфраструктура caller не подвергалась live-аудиту.
- DB injection/allowlist/drift/transaction проверки будущих milestones не
  реализовывались; текущий diff не расширяет полномочия parsers.
- All-extras DB/LLM environment, remote CI, другие OS/Python и transitive native
  system libraries не прошли отдельный полный audit в этом review.
