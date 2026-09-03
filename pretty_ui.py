# -*- coding: utf-8 -*-
"""TDLib Media Uploader V1.6.3 的 Rich 终端界面。"""

from __future__ import annotations

import threading
from typing import Iterable

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.screen import Screen
from rich.table import Table
from rich.text import Text


def _format_size(value: float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def _format_eta(seconds) -> str:
    if seconds is None:
        return "--:--"

    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    return f"{minutes:02}:{seconds:02}"


class PrettyConsoleUI:
    """
    Rich 终端 UI。

    上传进度一旦进入 alternate screen，就由一次完整上传会话持有。
    Album 切换、普通日志以及 Album 完成事件都不能主动关闭该屏幕；
    只有最终 banner、warning、error 或显式 force stop 才退出。

    这样可以避免每完成一个 Album 时短暂恢复原终端缓冲区造成闪屏。
    """

    def __init__(
        self,
        *,
        refresh_hz=8,
        show_mbps=True,
        bar_width=32,
        transient=True,
        enabled=True,
    ):
        self.lock = threading.RLock()
        self.refresh_hz = max(1, int(refresh_hz))
        self.show_mbps = bool(show_mbps)
        self.bar_width = max(12, int(bar_width))
        self.transient = bool(transient)
        self.enabled = bool(enabled)

        self.console = Console(highlight=False)
        self.live: Live | None = None
        self._upload_session_active = False

    def _stop_live_unlocked(self, *, force: bool = False):
        if self.live is None:
            self._upload_session_active = False
            return

        if self._upload_session_active and not force:
            return

        live = self.live
        self.live = None
        self._upload_session_active = False

        try:
            live.stop()
        except Exception:
            pass

    def _start_progress_live_unlocked(self, renderable):
        if self.live is not None:
            return

        self._upload_session_active = True

        live = Live(
            renderable,
            console=self.console,
            screen=True,
            auto_refresh=False,
            refresh_per_second=self.refresh_hz,
            vertical_overflow="crop",
            redirect_stdout=False,
            redirect_stderr=False,
        )

        self.live = live

        try:
            live.start(refresh=True)
        except Exception:
            self.live = None
            self._upload_session_active = False
            raise

    def end_upload_session(self):
        with self.lock:
            self._stop_live_unlocked(force=True)

    def log(self, text=""):
        with self.lock:
            if self._upload_session_active:
                return

            self._stop_live_unlocked()
            self.console.print(
                str(text),
                markup=False,
                highlight=False,
            )

    def info(self, text):
        with self.lock:
            if self._upload_session_active:
                return

            self._stop_live_unlocked()
            self.console.print(f"[bold cyan]ℹ[/] {text}")

    def success(self, text):
        with self.lock:
            if self._upload_session_active:
                return

            self._stop_live_unlocked()
            self.console.print(f"[bold green]✓[/] {text}")

    def warning(self, text):
        with self.lock:
            self._stop_live_unlocked(force=True)
            self.console.print(f"[bold yellow]![/] {text}")

    def error(self, text):
        with self.lock:
            self._stop_live_unlocked(force=True)
            self.console.print(f"[bold red]✗[/] {text}")

    def banner(
        self,
        title: str,
        subtitle: str = "",
        *,
        accent="cyan",
    ):
        body = Text()
        body.append(
            title,
            style=f"bold bright_{accent}",
        )

        if subtitle:
            body.append("\n")
            body.append(
                subtitle,
                style="grey70",
            )

        with self.lock:
            self._stop_live_unlocked(force=True)

            self.console.print(
                Panel(
                    body,
                    border_style=f"bright_{accent}",
                    box=box.ROUNDED,
                    padding=(0, 2),
                    expand=True,
                )
            )

    def summary(
        self,
        title: str,
        rows: Iterable[tuple[str, object]],
        *,
        kind="VIDEO",
    ):
        accent = "cyan" if kind.upper() == "VIDEO" else "magenta"

        table = Table(
            box=None,
            show_header=False,
            pad_edge=False,
            expand=True,
        )
        table.add_column(
            "key",
            style="grey70",
            width=18,
            no_wrap=True,
        )
        table.add_column(
            "value",
            style="bold white",
            overflow="fold",
        )

        for key, value in rows:
            table.add_row(
                str(key),
                str(value),
            )

        with self.lock:
            if self._upload_session_active:
                return

            self._stop_live_unlocked()

            self.console.print(
                Panel(
                    table,
                    title=f"[bold] {title} [/]","+"
                    title_align="left",
                    border_style=f"bright_{accent}",
                    box=box.ROUNDED,
                    padding=(0, 1),
                    expand=True,
                )
            )

    def files(
        self,
        title: str,
        columns: list[tuple[str, dict]],
        rows: Iterable[Iterable[object]],
        *,
        kind="VIDEO",
        caption: str | None = None,
    ):
        accent = "cyan" if kind.upper() == "VIDEO" else "magenta"

        table = Table(
            title=title,
            title_style=f"bold bright_{accent}",
            header_style=f"bold bright_{accent}",
            box=box.SIMPLE_HEAVY,
            show_lines=False,
            expand=True,
            caption=caption,
            caption_style="grey58",
        )

        for name, kwargs in columns:
            table.add_column(name, **kwargs)

        for row in rows:
            table.add_row(*(str(value) for value in row))

        with self.lock:
            if self._upload_session_active:
                return

            self._stop_live_unlocked()
            self.console.print(table)

    def groups(
        self,
        title: str,
        rows: Iterable[Iterable[object]],
        *,
        kind="VIDEO",
    ):
        accent = "cyan" if kind.upper() == "VIDEO" else "magenta"

        table = Table(
            title=title,
            title_style=f"bold bright_{accent}",
            header_style=f"bold bright_{accent}",
            box=box.SIMPLE,
            expand=False,
        )
        table.add_column("月份", no_wrap=True)
        table.add_column("Caption", no_wrap=True)
        table.add_column("总数", justify="right")
        table.add_column("待上传", justify="right")
        table.add_column("Album", justify="right")

        for row in rows:
            table.add_row(*(str(value) for value in row))

        with self.lock:
            if self._upload_session_active:
                return

            self._stop_live_unlocked()
            self.console.print(table)

    def target(
        self,
        chat_title,
        topic_name,
        chat_id,
        topic_id,
    ):
        self.summary(
            "Telegram 目标已确认",
            [
                ("聊天", chat_title or "(未命名)"),
                ("CHAT_ID", chat_id),
                ("Topic", topic_name or "(未命名)"),
                ("FORUM_TOPIC_ID", topic_id),
            ],
            kind="VIDEO",
        )

    def album(
        self,
        *,
        kind,
        title,
        subtitle="",
        rows=None,
    ):
        accent = "cyan" if kind.upper() == "VIDEO" else "magenta"

        body = Text()
        body.append(
            title,
            style=f"bold bright_{accent}",
        )

        if subtitle:
            body.append("\n")
            body.append(
                subtitle,
                style="grey70",
            )

        if rows:
            for row in rows:
                body.append("\n")
                body.append(
                    str(row),
                    style="white",
                )

        with self.lock:
            if self._upload_session_active:
                return

            self._stop_live_unlocked()

            self.console.print(
                Panel(
                    body,
                    border_style=f"bright_{accent}",
                    box=box.ROUNDED,
                    padding=(0, 1),
                    expand=True,
                )
            )

    def confirm_upload(self) -> bool:
        with self.lock:
            self._stop_live_unlocked(force=True)

            answer = self.console.input(
                "\n[bold cyan]确认开始上传？[/] "
                "输入 [bold green]y[/] 继续："
            )

        return answer.strip().lower() == "y"

    def cancelled(self):
        self.warning("已取消，没有开始上传。")

    def _build_progress_panel(
        self,
        *,
        kind,
        ratio,
        speed,
        eta,
        detail,
        album_number,
        album_total,
        done_files,
        total_files,
        done_bytes,
        total_bytes,
    ):
        ratio = max(
            0.0,
            min(float(ratio), 1.0),
        )

        terminal_width = max(
            40,
            int(self.console.size.width),
        )

        responsive_bar_width = min(
            self.bar_width,
            max(
                12,
                terminal_width - 46,
            ),
        )

        filled = int(
            round(
                responsive_bar_width
                *
                ratio
            )
        )
        empty = responsive_bar_width - filled

        if kind.upper() == "VIDEO":
            title = " VIDEO · TDLib 上传 "
            accent = "bright_cyan"
        else:
            title = " IMAGE · TDLib 上传 "
            accent = "bright_magenta"

        line1 = Text()
        line1.append(
            "━" * filled,
            style=f"bold {accent}",
        )
        line1.append(
            "─" * empty,
            style="grey50",
        )
        line1.append(
            f"  {ratio * 100:6.2f}%",
            style="bold white",
        )

        line2 = Text()
        line2.append(
            "速度  ",
            style="grey70",
        )
        line2.append(
            f"{_format_size(speed)}/s",
            style=f"bold {accent}",
        )

        if self.show_mbps:
            mbps = float(speed) * 8 / 1_000_000
            line2.append(
                f"  ·  {mbps:,.1f} Mbps",
                style="bold white",
            )

        line2.append(
            "    ETA  ",
            style="grey70",
        )
        line2.append(
            _format_eta(eta),
            style="bold white",
        )

        line3 = Text()

        if detail:
            line3.append(
                f"{detail}  ·  ",
                style="bold white",
            )

        line3.append(
            "Album ",
            style="grey70",
        )
        line3.append(
            f"{album_number}/{album_total}",
            style="bold white",
        )
        line3.append(
            "  ·  文件 ",
            style="grey70",
        )
        line3.append(
            f"{done_files}/{total_files}",
            style="bold white",
        )
        line3.append(
            "  ·  已传 ",
            style="grey70",
        )
        line3.append(
            (
                f"{_format_size(done_bytes)}"
                f" / "
                f"{_format_size(total_bytes)}"
            ),
            style="bold white",
        )

        return Panel(
            Group(
                line1,
                line2,
                line3,
            ),
            title=title,
            title_align="left",
            border_style=accent,
            box=box.ROUNDED,
            padding=(0, 1),
            expand=True,
        )

    def _build_progress_screen(self, **kwargs):
        panel = self._build_progress_panel(**kwargs)

        return Screen(
            panel,
            application_mode=True,
        )

    def progress(
        self,
        *,
        kind,
        ratio,
        speed,
        eta,
        detail="",
        album_number=0,
        album_total=0,
        done_files=0,
        total_files=0,
        done_bytes=0,
        total_bytes=0,
    ):
        with self.lock:
            if not self.enabled:
                terminal_width = max(
                    40,
                    int(self.console.size.width),
                )
                bar_width = max(
                    10,
                    min(
                        20,
                        terminal_width - 42,
                    ),
                )
                filled = int(
                    bar_width
                    *
                    max(
                        0.0,
                        min(float(ratio), 1.0),
                    )
                )
                bar = (
                    "#" * filled
                    +
                    "-" * (bar_width - filled)
                )

                self.console.print(
                    (
                        f"\r[{bar}] "
                        f"{ratio * 100:6.2f}% "
                        f"{_format_size(speed)}/s "
                        f"ETA {_format_eta(eta)}"
                    ),
                    end="",
                    soft_wrap=False,
                )
                return

            renderable = self._build_progress_screen(
                kind=kind,
                ratio=ratio,
                speed=speed,
                eta=eta,
                detail=detail,
                album_number=album_number,
                album_total=album_total,
                done_files=done_files,
                total_files=total_files,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
            )

            if self.live is None:
                self._start_progress_live_unlocked(renderable)
            else:
                self.live.update(
                    renderable,
                    refresh=True,
                )

    def finish(self):
        with self.lock:
            if self.enabled and self._upload_session_active:
                return

            self._stop_live_unlocked()

            if not self.enabled:
                self.console.print()
