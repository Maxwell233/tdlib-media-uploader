# -*- coding: utf-8 -*-
"""Rich 动态上传进度 UI。正常使用时无需修改本文件。"""

from __future__ import annotations

import threading

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


def _format_size(value: float) -> str:
    value = float(value)

    for unit in (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
    ):
        if (
            value < 1024
            or
            unit == "TiB"
        ):
            if unit == "B":
                return f"{value:.0f} {unit}"

            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} TiB"


def _format_eta(seconds) -> str:
    if seconds is None:
        return "--:--"

    seconds = max(
        0,
        int(seconds)
    )

    hours, remainder = divmod(
        seconds,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    if hours:
        return (
            f"{hours:02}:"
            f"{minutes:02}:"
            f"{seconds:02}"
        )

    return (
        f"{minutes:02}:"
        f"{seconds:02}"
    )


class PrettyConsoleUI:

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

        self.refresh_hz = max(
            1,
            int(refresh_hz)
        )

        self.show_mbps = bool(
            show_mbps
        )

        self.bar_width = max(
            12,
            int(bar_width)
        )

        self.transient = bool(
            transient
        )

        self.enabled = bool(
            enabled
        )

        self.console = Console(
            highlight=False
        )

        self.live = None

    def log(
        self,
        text=""
    ):
        with self.lock:
            self.console.print(
                str(text),
                markup=False,
                highlight=False,
            )

    def _render_progress(
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
            min(
                float(ratio),
                1.0
            )
        )

        filled = int(
            round(
                self.bar_width
                * ratio
            )
        )

        empty = (
            self.bar_width
            - filled
        )

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
            mbps = (
                float(speed)
                * 8
                /
                1_000_000
            )

            line2.append(
                f"  ·  {mbps:,.1f} Mbps",
                style="bold white",
            )

        line2.append(
            "    ETA  ",
            style="grey70",
        )

        line2.append(
            _format_eta(
                eta
            ),
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
            f"{_format_size(done_bytes)}"
            f" / "
            f"{_format_size(total_bytes)}",
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
            padding=(
                0,
                1
            ),
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
                # 简单 ASCII 回退模式
                filled = int(
                    20
                    *
                    max(
                        0.0,
                        min(
                            float(ratio),
                            1.0
                        )
                    )
                )

                bar = (
                    "#"
                    * filled
                    +
                    "-"
                    * (
                        20
                        - filled
                    )
                )

                self.console.print(
                    f"\r[{bar}] "
                    f"{ratio * 100:6.2f}% "
                    f"{_format_size(speed)}/s "
                    f"ETA {_format_eta(eta)}",
                    end="",
                    soft_wrap=False,
                )

                return

            renderable = (
                self._render_progress(
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
            )

            if self.live is None:
                self.live = Live(
                    renderable,
                    console=self.console,
                    refresh_per_second=
                        self.refresh_hz,
                    transient=
                        self.transient,
                    auto_refresh=True,
                )

                self.live.start()

            else:
                self.live.update(
                    renderable,
                    refresh=True
                )

    def finish(
        self
    ):
        with self.lock:
            if self.live is not None:
                self.live.stop()
                self.live = None
            elif not self.enabled:
                self.console.print()
