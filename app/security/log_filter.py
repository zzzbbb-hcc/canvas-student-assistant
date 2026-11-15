import logging
import re

class CredentialLogFilter(logging.Filter):
    _pattern = re.compile(
        r"(?i)(authorization|cookie|password|session|(?:canvas[_-]?)?pat|"
        r"(?:access_|refresh_)?token|api[_-]?key|secret)(\s*[:=]\s*)([^\s,;]+)"
    )

    @classmethod
    def _redact(cls, value: object) -> object:
        return cls._pattern.sub(r"\1\2[REDACTED]", value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact after logging has combined ``msg`` and ``args``. Redacting the
        # pieces separately misses calls such as logger.info("token=%s", value)
        # because neither individual piece contains the full key/value pair.
        try:
            formatted = record.getMessage()
        except (TypeError, ValueError):
            formatted = str(record.msg)
        record.msg = self._redact(formatted)
        record.args = ()
        return True

