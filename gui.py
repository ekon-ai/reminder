# -*- coding: utf-8 -*-
"""
gui.py — графический интерфейс приложения-напоминалки (Tkinter).

Реализует класс `ReminderAppGUI`:
  * список напоминаний с фильтрацией по статусу;
  * форму добавления напоминания (включая кнопки быстрого выбора времени);
  * действия над выбранной записью ("Готово", "Отменить", "Удалить");
  * просмотр деталей по двойному клику;
  * строку состояния с количеством всех/активных записей;
  * корректное подтверждение выхода и остановку фонового мониторинга.

Все обращения к Tkinter происходят только из главного потока приложения.
Данные из фонового потока (уведомления) доставляются через
NotificationManager + queue.Queue (см. notifications.py).
"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta
from typing import Optional

from database import (
    ReminderDatabase,
    STATUS_PENDING,
    STATUS_DONE,
    STATUS_OVERDUE,
    STATUS_CANCELED,
    DATETIME_FORMAT,
)
from notifications import NotificationManager


class ReminderAppGUI:
    """Главное окно приложения «Напоминалка»."""

    # Варианты фильтра в комбобоксе
    FILTER_ALL = "Все"
    FILTER_OPTIONS = [
        FILTER_ALL,
        f"Только: {STATUS_PENDING}",
        f"Только: {STATUS_DONE}",
        f"Только: {STATUS_OVERDUE}",
        f"Только: {STATUS_CANCELED}",
    ]

    def __init__(self, db: ReminderDatabase, notifier: NotificationManager):
        """Создаёт корневое окно и подключает менеджера уведомлений.

        :param db: экземпляр ReminderDatabase.
        :param notifier: экземпляр NotificationManager (ещё не запущенный).
        """
        self.db = db
        self.notifier = notifier

        self.root = tk.Tk()
        self.root.title("⏰ Напоминалка — планировщик задач")
        self.root.geometry("900x620")
        self.root.minsize(760, 520)

        # Привязываем root к менеджеру, чтобы фоновый поток мог безопасно
        # показывать popup-окна через очередь + after().
        self.notifier.root = self.root

        self.setup_ui()
        self.refresh_reminders()
        self.update_status_bar()

        # Подтверждение выхода при закрытии окна крестиком
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ==================================================================
    # Построение интерфейса
    # ==================================================================
    def setup_ui(self):
        """Настраивает весь интерфейс: форма, фильтры, список, статус-бар."""
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ------------------------- Форма добавления ---------------------
        form = ttk.LabelFrame(main, text=" Новое напоминание ", padding=10)
        form.pack(fill="x", pady=(0, 10))

        ttk.Label(form, text="Заголовок:").grid(row=0, column=0, sticky="w")
        self.entry_title = ttk.Entry(form, width=45)
        self.entry_title.grid(row=0, column=1, columnspan=3, sticky="we", padx=5)

        ttk.Label(form, text="Описание:").grid(row=1, column=0, sticky="nw")
        self.text_description = tk.Text(form, height=3, width=60, wrap="word")
        self.text_description.grid(row=1, column=1, columnspan=3, sticky="we", padx=5, pady=4)

        ttk.Label(form, text="Дата и время:").grid(row=2, column=0, sticky="w")
        self.entry_due = ttk.Entry(form, width=22)
        self.entry_due.grid(row=2, column=1, sticky="w", padx=5)
        self.entry_due.insert(
            0, (datetime.now() + timedelta(minutes=15)).strftime(DATETIME_FORMAT)
        )
        ttk.Label(
            form, text="формат: ГГГГ-ММ-ДД ЧЧ:ММ:СС", foreground="#7f8c8d"
        ).grid(row=2, column=2, columnspan=2, sticky="w")

        # Кнопки быстрого выбора времени
        quick = ttk.Frame(form)
        quick.grid(row=3, column=1, columnspan=3, sticky="w", pady=(6, 0))
        for minutes in (1, 5, 15, 60):
            ttk.Button(
                quick,
                text=f"+{minutes} мин" if minutes < 60 else "+1 час",
                width=8,
                command=lambda m=minutes: self.set_quick_time(m),
            ).pack(side="left", padx=3)

        btn_add = ttk.Button(form, text="➕ Добавить напоминание", command=self.add_reminder)
        btn_add.grid(row=4, column=1, sticky="w", pady=(10, 0))

        form.columnconfigure(1, weight=1)

        # ---------------------- Панель действий -------------------------
        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(0, 8))

        ttk.Label(actions, text="Фильтр:").pack(side="left")
        self.filter_var = tk.StringVar(value=self.FILTER_ALL)
        self.combo_filter = ttk.Combobox(
            actions, textvariable=self.filter_var,
            values=self.FILTER_OPTIONS, state="readonly", width=18,
        )
        self.combo_filter.pack(side="left", padx=6)
        self.combo_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh_reminders())

        ttk.Button(actions, text="🔄 Обновить", command=self.refresh_reminders).pack(side="left", padx=3)
        ttk.Button(actions, text="✔ Отметить «Готово»", command=self.mark_as_done).pack(side="left", padx=3)
        ttk.Button(actions, text="✖ Отменить", command=self.cancel_reminder).pack(side="left", padx=3)
        ttk.Button(actions, text="🗑 Удалить", command=self.delete_reminder).pack(side="left", padx=3)
        ttk.Button(actions, text="🔔 Тест уведомления", command=self.notifier.test_notification).pack(side="right")

        # ------------------------ Список записей --------------------------
        list_frame = ttk.Frame(main)
        list_frame.pack(fill="both", expand=True)

        columns = ("id", "title", "due", "status")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("id", text="№")
        self.tree.heading("title", text="Задача")
        self.tree.heading("due", text="Срок исполнения")
        self.tree.heading("status", text="Статус")

        self.tree.column("id", width=50, anchor="center", stretch=False)
        self.tree.column("title", width=380, anchor="w")
        self.tree.column("due", width=180, anchor="center", stretch=False)
        self.tree.column("status", width=140, anchor="center", stretch=False)

        # Цветовая маркировка статуса
        self.tree.tag_configure(STATUS_PENDING, foreground="#2980b9")
        self.tree.tag_configure(STATUS_DONE, foreground="#27ae60")
        self.tree.tag_configure(STATUS_OVERDUE, foreground="#c0392b")
        self.tree.tag_configure(STATUS_CANCELED, foreground="#95a5a6")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Двойной клик — показать детали
        self.tree.bind("<Double-1>", self.on_double_click)

        # -------------------------- Статус-бар ----------------------------
        self.status_bar = ttk.Label(
            main, text="Подготовка…", relief="sunken", anchor="w", padding=(8, 4)
        )
        self.status_bar.pack(fill="x", pady=(8, 0))

    def set_quick_time(self, minutes: int):
        """Устанавливает поле «дата и время» на N минут от текущего момента."""
        target = datetime.now() + timedelta(minutes=minutes)
        try:
            self.entry_due.delete(0, "end")
            self.entry_due.insert(0, target.strftime(DATETIME_FORMAT))
        except tk.TclError:
            pass  # виджет уже уничтожен — молча игнорируем

    # ==================================================================
    # Операции с данными
    # ==================================================================
    def add_reminder(self):
        """Добавляет напоминание из формы; валидирует ввод и обновляет список."""
        title = self.entry_title.get().strip()
        description = self.text_description.get("1.0", "end").strip()
        due_raw = self.entry_due.get().strip()

        if not title:
            messagebox.showwarning("Проверка ввода", "Введите заголовок напоминания.")
            return
        try:
            due_dt = datetime.strptime(due_raw, DATETIME_FORMAT)
        except ValueError:
            messagebox.showerror(
                "Некорректная дата",
                f"Не удалось распознать дату/время:\n«{due_raw}»\n\n"
                f"Ожидается формат: {DATETIME_FORMAT.replace('%Y-%m-%d %H:%M:%S', 'ГГГГ-ММ-ДД ЧЧ:ММ:СС')}",
            )
            return

        try:
            new_id = self.db.add_reminder(title, description, due_dt)
        except (ValueError, RuntimeError) as exc:
            messagebox.showerror("Ошибка БД", str(exc))
            return

        # Сброс формы
        self.entry_title.delete(0, "end")
        self.text_description.delete("1.0", "end")
        self.entry_due.delete(0, "end")
        self.entry_due.insert(0, (datetime.now() + timedelta(minutes=15)).strftime(DATETIME_FORMAT))

        self.refresh_reminders()
        self.update_status_bar()

        # Если срок уже наступил/наступает менее чем через минуту — сразу
        # уведомим пользователя (монитор подхватит запись в течение секунды,
        # но мгновенная реакция выглядит лучше).
        if due_dt <= datetime.now():
            self.notifier.show_manual_notification(title, description or f"Срок: {due_raw}")
        else:
            self.status_bar.config(
                text=f"Добавлено напоминание №{new_id}: «{title}» на {due_raw}"
            )

    def _get_selected_id(self) -> Optional[int]:
        """Возвращает id выделенной в списке записи или None."""
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Не выбрано", "Сначала выберите напоминание в списке.")
            return None
        try:
            return int(self.tree.item(selection[0], "values")[0])
        except (ValueError, IndexError, KeyError):
            return None

    def mark_as_done(self):
        """Отмечает выбранную запись выполненной («Готово»)."""
        rid = self._get_selected_id()
        if rid is None:
            return
        try:
            updated = self.db.update_status(rid, STATUS_DONE)
        except (ValueError, RuntimeError) as exc:
            messagebox.showerror("Ошибка", str(exc))
            return
        if updated:
            # Запись больше не должна уведомлять — очищаем «память» монитора
            self.notifier.forget(rid)
        self.refresh_reminders()
        self.update_status_bar()

    def cancel_reminder(self):
        """Отменяет выбранную запись (статус «Отменено»)."""
        rid = self._get_selected_id()
        if rid is None:
            return
        try:
            updated = self.db.update_status(rid, STATUS_CANCELED)
        except (ValueError, RuntimeError) as exc:
            messagebox.showerror("Ошибка", str(exc))
            return
        if updated:
            self.notifier.forget(rid)
        self.refresh_reminders()
        self.update_status_bar()

    def delete_reminder(self):
        """Удаляет выбранную запись после подтверждения."""
        rid = self._get_selected_id()
        if rid is None:
            return
        if not messagebox.askyesno(
            "Удаление", f"Удалить напоминание №{rid} безвозвратно?"
        ):
            return
        try:
            removed = self.db.delete_reminder(rid)
        except RuntimeError as exc:
            messagebox.showerror("Ошибка", str(exc))
            return
        if removed:
            self.notifier.forget(rid)
        self.refresh_reminders()
        self.update_status_bar()

    # ==================================================================
    # Отображение списка
    # ==================================================================
    def refresh_reminders(self):
        """Перечитывает БД и перерисовывает список с учётом фильтра."""
        try:
            rows = self.db.get_all_reminders()
        except RuntimeError as exc:
            messagebox.showerror("Ошибка БД", str(exc))
            return

        wanted = self._status_from_filter()
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            rid, title, description, due_time, status, created_at = row
            if wanted is not None and status != wanted:
                continue
            display_due = self._format_due_display(due_time)
            self.tree.insert(
                "", "end",
                values=(rid, title, display_due, status),
                tags=(status,),
            )
        self.update_status_bar()

    def _status_from_filter(self) -> Optional[str]:
        """Извлекает нужный статус из текста фильтра (None = «Все»)."""
        value = self.filter_var.get()
        if value == self.FILTER_ALL:
            return None
        prefix = "Только: "
        if value.startswith(prefix):
            return value[len(prefix):]
        return None

    @staticmethod
    def _format_due_display(due_raw) -> str:
        """Форматирует срок для отображения; некорректные значения как есть."""
        try:
            dt = datetime.strptime(str(due_raw), DATETIME_FORMAT)
            return dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return str(due_raw)

    def update_status_bar(self):
        """Обновляет строку состояния: всего записей / активных."""
        try:
            total, active = self.db.get_reminders_count()
        except RuntimeError as exc:
            self.status_bar.config(text=f"Ошибка БД: {exc}")
            return
        self.status_bar.config(
            text=(
                f"Всего напоминаний: {total}   |   "
                f"Активных (ожидает/просрочено): {active}   |   "
                f"Монитор уведомлений: запущен   |   "
                f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
        )

    # ==================================================================
    # Детали записи
    # ==================================================================
    def on_double_click(self, event=None):
        """По двойному клику открывает окно с деталями выбранного напоминания."""
        rid = self._get_selected_id()
        if rid is None:
            return
        row = self.db.get_reminder_by_id(rid)
        if row is None:
            messagebox.showinfo("Не найдено", "Запись была удалена.")
            self.refresh_reminders()
            return

        _id, title, description, due_time, status, created_at = row
        details = tk.Toplevel(self.root)
        details.title(f"Напоминание №{_id}")
        details.geometry("480x340")
        details.transient(self.root)
        details.resizable(False, False)

        body = ttk.Frame(details, padding=15)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=title, font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 8))

        info = (
            f"Срок исполнения: {self._format_due_display(due_time)}\n"
            f"Статус: {status}\n"
            f"Создано: {created_at}"
        )
        ttk.Label(body, text=info, foreground="#34495e", justify="left").pack(anchor="w", pady=(0, 10))

        ttk.Label(body, text="Описание:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        txt = tk.Text(body, height=8, wrap="word", font=("Segoe UI", 10), bg="#f7f7f7")
        txt.insert("1.0", description if description else "(описание отсутствует)")
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, pady=(2, 10))

        btns = ttk.Frame(body)
        btns.pack(fill="x")
        ttk.Button(
            btns, text="🔔 Напомнить сейчас",
            command=lambda: self.notifier.show_manual_notification(
                title, description or f"Срок: {due_time}"
            ),
        ).pack(side="left")
        ttk.Button(btns, text="Закрыть", command=details.destroy).pack(side="right")

    # ==================================================================
    # Завершение работы
    # ==================================================================
    def on_closing(self):
        """Диалог подтверждения выхода; останавливает монитор и закрывает приложение."""
        if not messagebox.askokcancel(
            "Выход", "Закрыть приложение «Напоминалка»?\nФоновые уведомления будут остановлены."
        ):
            return
        try:
            self.notifier.stop_monitoring()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        """Запускает главный цикл событий Tkinter."""
        self.root.mainloop()
