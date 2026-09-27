"""Синтетические syslog events с непрозрачным JSON сообщением."""

SYSLOG_JSON = (
    b"2026-01-01T10:00:01.123+03:00 node-a sample-worker[101]: "
    b'{"requestID":"request-a","level":"INFO","message":"Started",'
    b'"context":{"postal_code":"00123"}}\n'
    b"2026-01-01T10:00:02.456+03:00 node-b sample-worker[202]: "
    b'{"requestID":"request-b","level":"ERROR","message":"Stopped",'
    b'"context":{"postal_code":"420000"}}\n'
)

SYSLOG_CSV = b"event,result\n" + b"".join(
    b'"' + line.replace(b'"', b'""') + b'",ok\n' for line in SYSLOG_JSON.splitlines()
)
