# LLM checklist

- Temperature и output limits заданы детерминированно для mapping.
- Prompt version/fingerprint сохраняется.
- Provider SDK types не выходят из adapter package.
- Model self-confidence не используется как единственный итоговый score.
- MappingPlan не содержит SQL, commands или неизвестные identifiers.
- Malformed JSON, extra fields и prompt injection payload покрыты тестами.
- Privacy-first route блокирует cloud fallback для confidential/restricted data.
- Raw response хранится только согласно retention/redaction policy.
- No-LLM mode остаётся работоспособным.
