# -*- coding: utf-8 -*-
"""
main.py — точка входа приложения-напоминалки для Windows 11.

Порядок запуска:
  1. Инициализация базы данных (создание файла и схемы при необходимости).
  2. Создание NotificationManager (пока без root — он будет привязан в GUI).
  3. Создание GUI, который привязывает корневое окно Tk к менеджеру.
  4. Запуск фонового мониторинга БД (daemon-поток).
  5. Запуск главного цикла событий Tkinter.

Корректное завершение:
  * GUI при закрытии спрашивает подтверждение, останавливает монитор и
    закрывает соединения БД (см. ReminderAppGUI.on_closing);
  * на случай нештатного завершения (Ctrl+C, исключение) предусмотрена
    страховка в блоке try/finally.

Запуск:  python main.py
"""

import sys
import traceback

from database import ReminderDatabase
from notifications import NotificationManager
from gui import ReminderAppGUI


def main():
    """Инициализирует компоненты приложения и запускает цикл событий."""
    db = None
    notifier = None
    app = None

    try:
        # --- 1. База данных ------------------------------------------------
        try:
            db = ReminderDatabase("reminders.db")
        except RuntimeError as exc:
            # Ошибка подключения/создания БД — фатальна для приложения.
            _fatal_error(
                "Не удалось инициализировать базу данных",
                f"{exc}\n\nПроверьте права доступа к папке с приложением."
            )
            return 1

        # --- 2. Менеджер уведомлений ---------------------------------------
        # Создаём до GUI: root будет передан позже (NotificationManager.root),
        # чтобы фоновый поток мог безопасно показывать popup через очередь.
        notifier = NotificationManager(db, root=None)

        # --- 3. Графический интерфейс --------------------------------------
        try:
            app = ReminderAppGUI(db, notifier)
        except Exception as exc:
            _fatal_error("Не удалось создать графический интерфейс", str(exc))
            return 1

        # --- 4. Фоновый мониторинг БД ---------------------------------------
        # Монитор запускаем ДО mainloop: поток daemon'ный, а все Tk-операции
        # выполняются им только через after()/queue — это безопасно.
        notifier.start_monitoring()

        # --- 5. Главный цикл событий ----------------------------------------
        app.run()
        return 0

    except KeyboardInterrupt:
        # Закрытие через консоль (Ctrl+C): выходим без паники.
        return 0
    except Exception:
        # Любая непредвиденная ошибка — показываем диагностический диалог/консоль.
        err = traceback.format_exc()
        print(err, file=sys.stderr)
        try:
            _fatal_error("В приложении произошла непредвиденная ошибка", err[-1500:])
        except Exception:
            pass
        return 1
    finally:
        # Страховка: если mainloop завершился не через on_closing
        # (например, исключение внутри цикла) — останавливаем монитор
        # и закрываем соединение текущего потока.
        if notifier is not None:
            try:
                notifier.stop_monitoring()
            except Exception:
                pass
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _fatal_error(title: str, message: str):
    """Показывает критическую ошибку максимально доступным способом.

    Сначала пробуем Tkinter messagebox; если Tk недоступен (например,
    ошибка до создания окна или нет дисплея) — пишем в stderr.
    """
    shown = False
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
        shown = True
    except Exception:
        pass
    if not shown:
        print(f"FATAL [{title}]: {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
