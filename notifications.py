# -*- coding: utf-8 -*-
"""
notifications.py — модуль уведомлений для приложения-напоминалки.

Реализует класс `NotificationManager`:
  * фоновый daemon-поток, каждую секунду проверяющий БД на наступившие
    напоминания;
  * попытку показать системный тост Windows 11 (win10toast / plyer),
    с обязательной обработкой исключений (в Win 11 тосты могут быть
    заблокированы отсутствием ярлыка в меню Пуск);
  * 100% надёжный fallback — Tkinter popup-окно (Toplevel, -topmost,
    автозакрытие через 30 секунд).

КРИТИЧНО ВАЖНО: Tkinter не потокобезопасен. Фоновый поток НИКОГДА не
обращается к виджетам напрямую. Вместо этого сообщения кладутся в
queue.Queue, а главный поток (GUI) периодически забирает их из очереди
через root.after() и вызывает _show_popup уже в своём потоке.
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from typing import Optional

# Попытка импортировать библиотеки системных уведомлений.
# Любая из них может отсутствовать или не работать — это штатная ситуация,
# тогда используется Tkinter popup.
_TOASTER = None  # экземпляр DesktopNotifier из win10toast
try:
    from win10toast import ToastNotifier  # type: ignore
    _TOASTER = ToastNotifier()
except Exception:
    _TOASTER = None

_PLYER_AVAILABLE = False
try:
    from plyer import notification as _plyer_notification  # type: ignore
    _PLYER_AVAILABLE = True
except Exception:
    _PLYER_AVAILABLE = False


class NotificationManager:
    """Менеджер уведомлений: фоновый мониторинг БД + показ тостов/popup."""

    # Интервал опроса БД фоновым потоком, сек
    CHECK_INTERVAL = 1.0
    # Время автозакрытия Tkinter popup, мс (30 секунд)
    POPUP_TIMEOUT_MS = 30_000

    def __init__(self, db, root: Optional[tk.Tk] = None):
        """
        :param db: экземпляр ReminderDatabase (или совместимый объект).
        :param root: корневое окно Tkinter. Если передан None, popup будет
                     создаваться в отдельном минимальном Tk-контексте
                     (используется только для тестов без GUI).
        """
        self.db = db
        self.root = root

        # Очередь «запросов на popup»: фоновый поток пишет сюда,
        # главный поток читает. Это единственный безопасный канал
        # взаимодействия фонового потока с Tkinter.
        self._popup_queue: "queue.Queue[tuple]" = queue.Queue()

        # Событие остановки фонового монитора
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        # Флаг доступности системных тостов (определяется опытным путём)
        self._system_toasts_ok = _TOASTER is not None or _PLYER_AVAILABLE

        # Предотвращение дубликатов: id напоминаний, по которым уведомление
        # уже было показано (пока запись не сменит статус/время).
        self._notified_ids: set = set()
        self._notified_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Управление фоновым монитором
    # ------------------------------------------------------------------
    def start_monitoring(self):
        """Запускает фоновый daemon-поток мониторинга БД (раз в секунду)."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return  # уже запущен — не плодим второй поток
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="ReminderMonitor",
            daemon=True,  # поток умрёт вместе с процессом
        )
        self._monitor_thread.start()

        # Регистрируем «поллер» очереди popup в главном потоке Tkinter.
        # Вызов через after() гарантирует, что все Tk-операции происходят
        # именно в том потоке, где создан root (требование Tkinter).
        if self.root is not None:
            self.root.after(200, self._process_popup_queue)

    def stop_monitoring(self):
        """Останавливает фоновый поток (вызывается при закрытии приложения)."""
        self._stop_event.set()
        if self._monitor_thread is not None:
            # Ждём завершения монитора недолго — поток daemon'ный.
            self._monitor_thread.join(timeout=3.0)
            self._monitor_thread = None

    def _monitor_loop(self):
        """Тело фонового потока: проверка БД каждую секунду."""
        while not self._stop_event.is_set():
            try:
                # 1. Наступившие напоминания -> уведомляем
                due = self.db.get_due_reminders()
                for row in due:
                    rid, title, description, due_time, status, _created = row
                    with self._notified_lock:
                        if rid in self._notified_ids:
                            continue  # уже уведомляли — не спамим
                        self._notified_ids.add(rid)
                    text = description if description else f"Срок: {due_time}"
                    self._show_notification(title, text)
                    # Отмечаем, что «сработало»: статус оставляем "Ожидает",
                    # чтобы mark_overdue перевёл его в "Просрочено" через минуту,
                    # если пользователь не отреагирует.

                # 2. Помечаем просроченное (вышло > 1 минуты назад)
                self.db.mark_overdue()

            except Exception as exc:
                # Монитор не должен умирать из-за разовой ошибки БД —
                # пишем в консоль и продолжаем работу.
                print(f"[NotificationManager] Ошибка мониторинга: {exc}")

            # Пауза с возможностью досрочного выхода по stop_event
            self._stop_event.wait(self.CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Показ уведомлений
    # ------------------------------------------------------------------
    def _show_notification(self, title: str, text: str):
        """Основная точка входа показа: сначала системный тост, затем fallback.

        Логика:
          1. Пробуем win10toast / plyer (системный центр уведомлений Win 11).
          2. Если библиотека недоступна или бросила исключение (типично для
             Win 11 без ярлыка в меню Пуск) — падаем в Tkinter popup.
        """
        title = str(title)[:120]   # защита от экстремально длинных строк
        text = str(text)[:500]

        shown = False
        if self._system_toasts_ok:
            shown = self._show_system_toast(title, text)

        if not shown:
            # Fallback — Tkinter popup (кладём в очередь для главного потока)
            self._request_popup(title, text)

    def _show_system_toast(self, title: str, text: str) -> bool:
        """Пытается показать системное уведомление Windows. Возвращает успех."""
        # Вариант 1: win10toast
        if _TOASTER is not None:
            try:
                # show_toast блокирует вызывающий поток на время display_time,
                # поэтому запускаем его detached-режимом ( threaded=True ).
                _TOASTER.show_toast(
                    title=title,
                    msg=text,
                    duration=8,
                    threaded=True,
                )
                return True
            except Exception as exc:
                print(f"[NotificationManager] win10toast не сработал: {exc}")

        # Вариант 2: plyer
        if _PLYER_AVAILABLE:
            try:
                _plyer_notification.notify(
                    title=title,
                    message=text,
                    app_name="Напоминалка",
                    timeout=8,
                )
                return True
            except Exception as exc:
                print(f"[NotificationManager] plyer не сработал: {exc}")

        # Системные тосты недоступны — больше не пытаемся (экономия времени)
        self._system_toasts_ok = False
        return False

    # ------------------------------------------------------------------
    # Безопасный Tkinter popup (главный поток!)
    # ------------------------------------------------------------------
    def _request_popup(self, title: str, text: str):
        """Кладёт запрос popup в очередь (безопасно из любого потока)."""
        self._popup_queue.put((title, text))
        # Если GUI-цикла нет (например, test_notification вне mainloop),
        # показываем popup синхронно в отдельном мини-Tk контексте.
        if self.root is None:
            self._process_popup_queue_standalone()

    def _process_popup_queue(self):
        """Забирает сообщения из очереди в ГЛАВНОМ потоке Tkinter.

        Планирует себя повторно через root.after(), пока приложение живо.
        """
        try:
            while True:
                title, text = self._popup_queue.get_nowait()
                self._show_popup(title, text)
        except queue.Empty:
            pass
        finally:
            # Перепланирование: опрашиваем очередь каждые 250 мс
            if self.root is not None:
                try:
                    self.root.after(250, self._process_popup_queue)
                except tk.TclError:
                    pass  # окно уже закрыто — планировчик остановлен

    def _process_popup_queue_standalone(self):
        """Обработка очереди без основного GUI (для автономных тестов)."""
        try:
            title, text = self._popup_queue.get_nowait()
        except queue.Empty:
            return
        # Создаём временный Tk-корень, показываем popup, уничтожаем корень.
        tmp_root = tk.Tk()
        tmp_root.withdraw()  # сам корень невидим — только Toplevel
        try:
            self._show_popup(title, text, parent=tmp_root)
            tmp_root.mainloop()
        finally:
            try:
                tmp_root.destroy()
            except tk.TclError:
                pass

    def _show_popup(self, title: str, text: str, parent=None):
        """Создаёт Tkinter popup-окно поверх всех окон с автозакрытием.

        ВАЖНО: должен вызываться ТОЛЬКО из главного потока Tkinter.

        :param parent: родительский виджет (root). Если None — используется
                       self.root; если и он None — поведение не определено
                       (защита от вызова из чужого потока отсутствует здесь
                       намеренно: вызов фильтруется очередью выше).
        """
        master = parent if parent is not None else self.root
        if master is None:
            # Нет окна-родителя и мы не в standalone-режиме — теряем сообщение,
            # но не роняем приложение.
            print(f"[NotificationManager] Popup без root: '{title}' — {text}")
            return

        popup = tk.Toplevel(master)
        popup.title("⏰ Напоминание")
        popup.geometry("420x220")
        popup.resizable(False, False)
        # Поверх всех окон Windows
        popup.attributes("-topmost", True)
        popup.configure(bg="#ffffff")

        # Заголовок
        lbl_title = ttk.Label(
            popup, text=title, font=("Segoe UI", 12, "bold"),
            foreground="#c0392b", wraplength=390, justify="left",
        )
        lbl_title.pack(padx=15, pady=(15, 5), anchor="w")

        # Текст описания
        txt = tk.Text(
            popup, height=6, wrap="word", relief="flat",
            font=("Segoe UI", 10), bg="#f7f7f7",
        )
        txt.insert("1.0", text)
        txt.configure(state="disabled")  # read-only
        txt.pack(padx=15, pady=5, fill="both", expand=True)

        # Кнопка закрытия + счётчик обратного отсчёта
        frame_btns = ttk.Frame(popup)
        frame_btns.pack(fill="x", padx=15, pady=(5, 12))

        # Мутабельный контейнер для id задачи after(), чтобы корректно
        # отменять таймер при ручном закрытии.
        state = {"after_id": None, "remaining_ms": self.POPUP_TIMEOUT_MS}

        def close(event=None):
            """Явное уничтожение окна и отмена таймера (чистка памяти)."""
            if state["after_id"] is not None:
                try:
                    popup.after_cancel(state["after_id"])
                except tk.TclError:
                    pass
                state["after_id"] = None
            try:
                popup.destroy()
            except tk.TclError:
                pass  # уже уничтожено

        def auto_close_tick():
            """Тик обратного отсчёта: обновляет лейбл, закрывает по нулю."""
            state["remaining_ms"] -= 1000
            if state["remaining_ms"] <= 0:
                state["after_id"] = None
                close()
                return
            countdown_label.config(
                text=f"Автозакрытие через {state['remaining_ms'] // 1000} с."
            )
            state["after_id"] = popup.after(1000, auto_close_tick)

        countdown_label = ttk.Label(
            frame_btns,
            text=f"Автозакрытие через {self.POPUP_TIMEOUT_MS // 1000} с.",
            foreground="#7f8c8d",
        )
        countdown_label.pack(side="left")

        btn_close = ttk.Button(frame_btns, text="Закрыть", command=close, width=10)
        btn_close.pack(side="right")

        # Закрытие крестиком тоже должно корректно убирать таймер
        popup.protocol("WM_DELETE_WINDOW", close)
        popup.bind("<Escape>", close)

        # Фокус и старт таймера
        popup.lift()
        try:
            popup.focus_force()
        except tk.TclError:
            pass
        state["after_id"] = popup.after(1000, auto_close_tick)

    # ------------------------------------------------------------------
    # Публичные методы для GUI
    # ------------------------------------------------------------------
    def show_manual_notification(self, title: str, text: str):
        """Ручной вызов уведомления (например, кнопка в деталях напоминания).

        Вызывается из главного потока GUI — popup можно показывать сразу,
        минуя очередь.
        """
        self._reset_notified_if_needed()
        if not self._show_system_toast(title, text):
            self._show_popup(title, text)

    def test_notification(self):
        """Тестовое уведомление для проверки канала доставки (кнопка в GUI)."""
        now = datetime.now().strftime("%H:%M:%S")
        title = "Тестовое уведомление"
        text = (
            f"Это проверка системы оповещений.\n"
            f"Текущее время: {now}\n\n"
            f"Если вы видите это окно — fallback (Tkinter popup) работает. "
            f"Системные тосты Windows: "
            f"{'доступны' if self._system_toasts_ok else 'НЕДОСТУПНЫ'}."
        )
        self._show_notification(title, text)

    def forget(self, reminder_id: int):
        """Забывает, что по записи уже было уведомление (после смены статуса/времени)."""
        with self._notified_lock:
            self._notified_ids.discard(reminder_id)

    def _reset_notified_if_needed(self):
        """Периодическая чистка множества notified, чтобы оно не росло бесконечно."""
        with self._notified_lock:
            if len(self._notified_ids) > 1000:
                self._notified_ids.clear()
