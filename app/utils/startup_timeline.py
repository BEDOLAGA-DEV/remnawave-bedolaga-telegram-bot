import platform
import time
import unicodedata
from collections.abc import Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


def _char_width(ch: str) -> int:
    """Return terminal display width of a single character."""
    cp = ord(ch)
    # Variation selector U+FE0F / U+FE0E — zero width (handled by caller)
    if cp in (0xFE0E, 0xFE0F, 0x200D):
        return 0
    # Combining marks — zero width
    if unicodedata.category(ch).startswith('M'):
        return 0
    # East Asian Wide / Fullwidth
    if unicodedata.east_asian_width(ch) in ('W', 'F'):
        return 2
    return 1


def _display_width(text: str) -> int:
    """Calculate terminal display width accounting for wide chars and emoji."""
    width = 0
    prev_base = 0
    for ch in text:
        cp = ord(ch)
        # U+FE0F emoji presentation selector — upgrades previous char to 2 cells
        if cp == 0xFE0F:
            if prev_base == 1:
                width += 1  # upgrade 1 → 2
                prev_base = 2
            continue
        cw = _char_width(ch)
        if cw > 0:
            prev_base = cw
        width += cw
    return width


def _ljust(text: str, width: int) -> str:
    """Left-justify text to given display width."""
    return text + ' ' * max(0, width - _display_width(text))


def _center(text: str, width: int) -> str:
    """Center text to given display width."""
    pad = max(0, width - _display_width(text))
    left = pad // 2
    return ' ' * left + text + ' ' * (pad - left)


@dataclass
class StepRecord:
    title: str
    icon: str
    status_label: str
    message: str
    duration: float


class StageHandle:
    def __init__(
        self,
        timeline: 'StartupTimeline',
        title: str,
        icon: str,
        success_message: str | None,
    ) -> None:
        self.timeline = timeline
        self.title = title
        self.icon = icon
        self.message = success_message or ''
        self.status_icon = '✅'
        self.status_label = 'Готово'
        self._explicit_status = False

    def success(self, message: str | None = None) -> None:
        if message is not None:
            self.message = message
        self.status_icon = '✅'
        self.status_label = 'Готово'
        self._explicit_status = True

    def warning(self, message: str) -> None:
        self.status_icon = '⚠️'
        self.status_label = 'Предупреждение'
        self.message = message
        self._explicit_status = True

    def skip(self, message: str) -> None:
        self.status_icon = '⏭️'
        self.status_label = 'Пропущено'
        self.message = message
        self._explicit_status = True

    def failure(self, message: str) -> None:
        self.status_icon = '❌'
        self.status_label = 'Ошибка'
        self.message = message
        self._explicit_status = True

    def log(self, message: str, icon: str = '•') -> None:
        self.timeline.logger.info('┃', icon=icon, message=message)


class StartupTimeline:
    def __init__(self, logger: Any, app_name: str) -> None:
        self.logger = logger
        self.app_name = app_name
        self.steps: list[StepRecord] = []
        self.metadata_records: list[tuple[str, Any]] = []

    def _record_step(self, title: str, icon: str, status_label: str, message: str, duration: float) -> None:
        self.steps.append(
            StepRecord(
                title=title,
                icon=icon,
                status_label=status_label,
                message=message,
                duration=duration,
            )
        )

    def log_banner(self, metadata: Sequence[tuple[str, Any]] | None = None) -> None:
        if metadata:
            self.metadata_records.extend(metadata)

        title_text = f'🚀 {self.app_name}'
        subtitle_parts = [f'Python {platform.python_version()}']
        if metadata:
            for key, value in metadata:
                subtitle_parts.append(f'{key}: {value}')
        subtitle_text = ' | '.join(subtitle_parts)

        width = max(_display_width(title_text), _display_width(subtitle_text))
        border = '╔' + '═' * (width + 2) + '╗'
        self.logger.info(border)
        self.logger.info('║ ' + _ljust(title_text, width) + ' ║')
        self.logger.info('║ ' + _ljust(subtitle_text, width) + ' ║')
        self.logger.info('╚' + '═' * (width + 2) + '╝')

    def log_section(self, title: str, lines: Iterable[str], icon: str = '📄') -> None:
        items = [f'{icon} {title}'] + [f'• {line}' for line in lines]
        width = max(_display_width(item) for item in items)
        top = '┌ ' + '─' * width + ' ┐'
        middle = '├ ' + '─' * width + ' ┤'
        bottom = '└ ' + '─' * width + ' ┘'

        self.logger.info(top)
        self.logger.info('│ ' + _ljust(items[0], width) + ' │')
        self.logger.info(middle)
        for item in items[1:]:
            self.logger.info('│ ' + _ljust(item, width) + ' │')
        self.logger.info(bottom)

    def add_manual_step(
        self,
        title: str,
        icon: str,
        status_label: str,
        message: str,
    ) -> None:
        self.logger.info('┏', icon=icon, title=title)
        self.logger.info('┗ —', icon=icon, title=title, status_label=status_label, message=message)
        self._record_step(title, icon, status_label, message, 0.0)

    @asynccontextmanager
    async def stage(
        self,
        title: str,
        icon: str = '⚙️',
        description: str | None = None,
        success_message: str | None = 'Готово',
    ):
        if description:
            self.logger.info('┏ —', icon=icon, title=title, description=description)
        else:
            self.logger.info('┏', icon=icon, title=title)

        handle = StageHandle(self, title, icon, success_message)
        start_time = time.perf_counter()
        try:
            yield handle
        except Exception as exc:
            message = str(exc)
            handle.failure(message)
            self.logger.exception('┣ ❌ — ошибка', title=title, message=message)
            raise
        finally:
            duration = time.perf_counter() - start_time
            if not handle._explicit_status:
                handle.success(handle.message or 'Готово')
            self.logger.info(
                '┗ — [s]',
                status_icon=handle.status_icon,
                title=title,
                message=handle.message,
                duration=round(duration, 2),
            )
            self._record_step(
                title=title,
                icon=handle.status_icon,
                status_label=handle.status_label,
                message=handle.message,
                duration=duration,
            )

    def log_summary(self) -> None:
        if not self.steps:
            return

        lines = []
        if self.metadata_records:
            for key, value in self.metadata_records:
                lines.append(f'🔹 {key}: {value}')
            lines.append('─' * 10)  # Разделитель

        for step in self.steps:
            base = f'{step.icon} {step.title} — {step.status_label} [{step.duration:.2f}s]'
            if step.message:
                base += f' :: {step.message}'
            lines.append(base)

        width = max(_display_width(line) for line in lines)
        border_top = '┏' + '━' * (width + 2) + '┓'
        border_mid = '┣' + '━' * (width + 2) + '┫'
        border_bottom = '┗' + '━' * (width + 2) + '┛'
        title = 'РЕЗЮМЕ ЗАПУСКА'

        self.logger.info(border_top)
        self.logger.info('┃ ' + _center(title, width) + ' ┃')
        self.logger.info(border_mid)
        for line in lines:
            self.logger.info('┃ ' + _ljust(line, width) + ' ┃')
        self.logger.info(border_bottom)
