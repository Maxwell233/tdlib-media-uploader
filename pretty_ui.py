# -*- coding: utf-8 -*-
"""TDLib Media Uploader V1.6.3 Rich terminal UI."""
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


def _format_size(v):
    v = float(v)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < 1024 or u == "TiB":
            return f"{v:.0f} {u}" if u == "B" else f"{v:.2f} {u}"
        v /= 1024


def _format_eta(s):
    if s is None:
        return "--:--"
    s = max(0, int(s))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h:02}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


class PrettyConsoleUI:
    def __init__(self, *, refresh_hz=8, show_mbps=True, bar_width=32, transient=True, enabled=True):
        self.lock = threading.RLock()
        self.refresh_hz = max(1, int(refresh_hz))
        self.show_mbps = bool(show_mbps)
        self.bar_width = max(12, int(bar_width))
        self.transient = bool(transient)
        self.enabled = bool(enabled)
        self.console = Console(highlight=False)
        self.live = None
        self._upload_session_active = False

    def _stop_live_unlocked(self, force=False):
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

    def _start_live_unlocked(self, renderable):
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
            self.console.print(str(text), markup=False, highlight=False)

    def info(self, text):
        with self.lock:
            if self._upload_session_active:
                return
            self.console.print(f"[bold cyan]ℹ[/] {text}")

    def success(self, text):
        with self.lock:
            if self._upload_session_active:
                return
            self.console.print(f"[bold green]✓[/] {text}")

    def warning(self, text):
        with self.lock:
            self._stop_live_unlocked(force=True)
            self.console.print(f"[bold yellow]![/] {text}")

    def error(self, text):
        with self.lock:
            self._stop_live_unlocked(force=True)
            self.console.print(f"[bold red]✗[/] {text}")

    def banner(self, title, subtitle="", *, accent="cyan"):
        b = Text()
        b.append(title, style=f"bold bright_{accent}")
        if subtitle:
            b.append("\n")
            b.append(subtitle, style="grey70")
        with self.lock:
            self._stop_live_unlocked(force=True)
            self.console.print(
                Panel(
                    b,
                    border_style=f"bright_{accent}",
                    box=box.ROUNDED,
                    padding=(0, 2),
                    expand=True,
                )
            )

    def summary(self, title, rows: Iterable[tuple[str, object]], *, kind="VIDEO"):
        if self._upload_session_active:
            return
        a = "cyan" if kind.upper() == "VIDEO" else "magenta"
        t = Table(box=None, show_header=False, pad_edge=False, expand=True)
        t.add_column("key", style="grey70", width=18, no_wrap=True)
        t.add_column("value", style="bold white", overflow="fold")
        for k, v in rows:
            t.add_row(str(k), str(v))
        with self.lock:
            self.console.print(
                Panel(
                    t,
                    title=f"[bold] {title} [/]",
                    title_align="left",
                    border_style=f"bright_{a}",
                    box=box.ROUNDED,
                    padding=(0, 1),
                    expand=True,
                )
            )

    def files(self, title, columns, rows, *, kind="VIDEO", caption=None):
        if self._upload_session_active:
            return
        a = "cyan" if kind.upper() == "VIDEO" else "magenta"
        t = Table(
            title=title,
            title_style=f"bold bright_{a}",
            header_style=f"bold bright_{a}",
            box=box.SIMPLE_HEAVY,
            show_lines=False,
            expand=True,
            caption=caption,
            caption_style="grey58",
        )
        for n, kw in columns:
            t.add_column(n, **kw)
        for r in rows:
            t.add_row(*(str(v) for v in r))
        with self.lock:
            self.console.print(t)

    def groups(self, title, rows, *, kind="VIDEO"):
        if self._upload_session_active:
            return
        a = "cyan" if kind.upper() == "VIDEO" else "magenta"
        t = Table(
            title=title,
            title_style=f"bold bright_{a}",
            header_style=f"bold bright_{a}",
            box=box.SIMPLE,
            expand=False,
        )
        t.add_column("月份", no_wrap=True)
        t.add_column("Caption", no_wrap=True)
        t.add_column("总数", justify="right")
        t.add_column("待上传", justify="right")
        t.add_column("Album", justify="right")
        for r in rows:
            t.add_row(*(str(v) for v in r))
        with self.lock:
            self.console.print(t)

    def target(self, chat_title, topic_name, chat_id, topic_id):
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

    def album(self, *, kind, title, subtitle="", rows=None):
        if self._upload_session_active:
            return
        a = "cyan" if kind.upper() == "VIDEO" else "magenta"
        b = Text()
        b.append(title, style=f"bold bright_{a}")
        if subtitle:
            b.append("\n")
            b.append(subtitle, style="grey70")
        if rows:
            for r in rows:
                b.append("\n")
                b.append(str(r), style="white")
        with self.lock:
            self.console.print(
                Panel(
                    b,
                    border_style=f"bright_{a}",
                    box=box.ROUNDED,
                    padding=(0, 1),
                    expand=True,
                )
            )

    def confirm_upload(self):
        with self.lock:
            self._stop_live_unlocked(force=True)
            ans = self.console.input(
                "\n[bold cyan]确认开始上传？[/] 输入 [bold green]y[/] 继续："
            )
        return ans.strip().lower() == "y"

    def cancelled(self):
        self.warning("已取消，没有开始上传。")

    def _panel(
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
        ratio = max(0.0, min(float(ratio), 1.0))
        tw = max(40, int(self.console.size.width))
        bw = min(self.bar_width, max(12, tw - 46))
        f = int(round(bw * ratio))
        e = bw - f

        if kind.upper() == "VIDEO":
            title = " VIDEO · TDLib 上传 "
            a = "bright_cyan"
        else:
            title = " IMAGE · TDLib 上传 "
            a = "bright_magenta"

        l1 = Text()
        l1.append("━" * f, style=f"bold {a}")
        l1.append("─" * e, style="grey50")
        l1.append(f"  {ratio * 100:6.2f}%", style="bold white")

        l2 = Text()
        l2.append("速度  ", style="grey70")
        l2.append(f"{_format_size(speed)}/s", style=f"bold {a}")
        if self.show_mbps:
            l2.append(
                f"  ·  {float(speed) * 8 / 1_000_000:,.1f} Mbps",
                style="bold white",
            )
        l2.append("    ETA  ", style="grey70")
        l2.append(_format_eta(eta), style="bold white")

        l3 = Text()
        if detail:
            l3.append(f"{detail}  ·  ", style="bold white")
        l3.append("Album ", style="grey70")
        l3.append(f"{album_number}/{album_total}", style="bold white")
        l3.append("  ·  文件 ", style="grey70")
        l3.append(f"{done_files}/{total_files}", style="bold white")
        l3.append("  ·  已传 ", style="grey70")
        l3.append(
            f"{_format_size(done_bytes)} / {_format_size(total_bytes)}",
            style="bold white",
        )

        return Panel(
            Group(l1, l2, l3),
            title=title,
            title_align="left",
            border_style=a,
            box=box.ROUNDED,
            padding=(0, 1),
            expand=True,
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
                bw = 20
                f = int(bw * max(0, min(float(ratio), 1)))
                self.console.print(
                    f"\r[{'#' * f}{'-' * (bw - f)}] {ratio * 100:6.2f}% "
                    f"{_format_size(speed)}/s ETA {_format_eta(eta)}",
                    end="",
                    soft_wrap=False,
                )
                return

            screen = Screen(
                self._panel(
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
                ),
                application_mode=True,
            )

            if self.live is None:
                self._start_live_unlocked(screen)
            else:
                self.live.update(screen, refresh=True)

    def finish(self):
        with self.lock:
            if self.enabled and self._upload_session_active:
                return
            self._stop_live_unlocked()
            if not self.enabled:
                self.console.print()
