"""Normalize text for native speech and prevent CLI command injection."""
import re

def speech_text(value: str, limit: int = 900) -> str:
    value = re.sub(r'<think>.*?</think>', '', value, flags=re.S | re.I)
    value = re.sub(r'```.*?```', '', value, flags=re.S)
    value = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', value)
    value = re.sub(r'[*#`_]', '', value)
    value = ' '.join(value.split()).strip()
    if len(value) > limit:
        prefix = value[:limit]
        stop = max(prefix.rfind('. '), prefix.rfind('! '), prefix.rfind('? '))
        value = prefix[:stop + 1] if stop > limit // 2 else prefix.rsplit(' ', 1)[0] + '.'
    return value

