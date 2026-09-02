# Parser checklist

## Общее

- Фактический тип важнее расширения.
- Source читается ограниченно; seekability не предполагается без проверки.
- Duplicate headers и missing values имеют детерминированную политику.
- Raw value и normalized candidate не смешиваются.

## Форматная безопасность

- XML: запрет DTD, external entities и network resolution.
- HTML: без JavaScript, iframe fetch и external resources.
- YAML: только safe loader, limits на aliases/depth.
- XLSX/DOCX: ZIP limits, path traversal, macros не исполняются.
- PDF: только text layer в MVP; malformed object limits и sandbox policy.
- LOG/TXT: limits на line length и multiline event size.

## Тесты

- Provenance указывает на исходное значение.
- Batch boundary не меняет результат.
- Ошибка содержит стабильный code и безопасный context.
- Большой источник не читается полностью в память.
