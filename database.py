# -*- coding: utf-8 -*-
"""
database.py — модуль работы с базой данных напоминаний (SQLite3).

Реализует класс `ReminderDatabase`:
  * создание/подключение к файлу БД;
  * потокобезопасную работу (каждый поток получает собственное соединение
    через threading.local());
  * CRUD-операции над напоминаниями и служебные методы (сортировка,
    подсчёт, перевод просроченных записей в статус "Просрочено").

Формат хранения времени в БД: ISO 8601 ("YYYY-MM-DD HH:MM:SS"),
что корректно парсится обратно в datetime и корректно сортируется
лексикографически.
"""

import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

# Имя по умолчанию для файла базы данных
DEFAULT_DB_NAME = "reminders.db"

# Разрешённые статусы записи (используются GUI и мониторингом)
STATUS_PENDING = "Ожидает"
STATUS_DONE = "Готово"
STATUS_OVERDUE = "Просрочено"
STATUS_CANCELED = "Отменено"

VALID_STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_OVERDUE, STATUS_CANCELED)

# Формат даты-времени, используемый при общении с пользователем и в БД
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class ReminderDatabase:
    """Инкапсуляция логики работы с SQLite-базой напоминаний.

    Потокобезопасность: sqlite3.Connection нельзя безопасно разделять
    между потоками, поэтому используется thread-local хранилище — каждый
    поток (GUI-поток и фоновый поток мониторинга) имеет своё соединение.
    """

    def __init__(self, db_path: str = DEFAULT_DB_NAME):
        """Инициализирует БД: определяет путь и создаёт таблицу при старте.

        :param db_path: путь к файлу базы данных (можно задать абсолютный).
        """
        # Резолвим путь относительно текущей директории скрипта,
        # чтобы файл БД не «гулял» вслед за cwd процесса.
        if not os.path.isabs(db_path):
            base_dir = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(base_dir, db_path)

        self.db_path = db_path
        # Хранилище соединений: по одному на каждый поток.
        self._local = threading.local()
        # Создаём схему при старте (проверяет наличие таблицы).
        self.init_database()

    # ------------------------------------------------------------------
    # Работа с соединениями (thread-local)
    # ------------------------------------------------------------------
    def _get_connection(self) -> sqlite3.Connection:
        """Возвращает соединение SQLite, привязанное к текущему потоку."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(
                    self.db_path,
                    timeout=10.0,          # ждать блокировки до 10 секунд
                    check_same_thread=False,  # соединение живёт только в своём потоке
                )
                # Возвращаем строки как str, а не bytes
                conn.text_factory = str
                self._local.conn = conn
            except sqlite3.Error as exc:
                raise RuntimeError(f"Не удалось подключиться к БД '{self.db_path}': {exc}") from exc
        return conn

    def close(self):
        """Закрывает соединение текущего потока (вызывается при завершении)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass  # соединение уже закрыто или недоступно — игнорируем
            finally:
                self._local.conn = None

    # ------------------------------------------------------------------
    # Схема БД
    # ------------------------------------------------------------------
    def init_database(self):
        """Создаёт таблицу reminders, если её ещё нет. Вызывается при старте."""
        sql = """
        CREATE TABLE IF NOT EXISTS reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            description TEXT    NOT NULL DEFAULT '',
            due_time    TEXT    NOT NULL,           -- ISO 8601: 'YYYY-MM-DD HH:MM:SS'
            status      TEXT    NOT NULL DEFAULT 'Ожидает',
            created_at  TEXT    NOT NULL            -- время создания записи
        );
        """
        # Индекс по due_time ускоряет выборку «что пора» в фоновом потоке.
        idx_sql = "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (due_time, status);"
        conn = self._get_connection()
        try:
            with conn:  # транзакция: commit при успехе, rollback при ошибке
                conn.execute(sql)
                conn.execute(idx_sql)
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка инициализации схемы БД: {exc}") from exc

    # ------------------------------------------------------------------
    # Внутренние помощники сериализации времени
    # ------------------------------------------------------------------
    @staticmethod
    def _dt_to_str(dt: datetime) -> str:
        """Конвертирует datetime в строку для хранения в БД."""
        return dt.strftime(DATETIME_FORMAT)

    @staticmethod
    def _str_to_dt(value) -> Optional[datetime]:
        """Парсит значение из БД обратно в datetime с обработкой ошибок."""
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            return datetime.strptime(str(value), DATETIME_FORMAT)
        except ValueError:
            # Попытка распарсить ISO с другим разделителем/микросекундами
            try:
                return datetime.fromisoformat(str(value))
            except ValueError:
                return None

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def add_reminder(self, title: str, description: str, due_time: datetime) -> int:
        """Добавляет напоминание.

        :param title: заголовок (обязателен, не пустой).
        :param description: описание (может быть пустым).
        :param due_time: срок исполнения (datetime).
        :return: id новой записи.
        :raises ValueError: если title пуст или due_time неверного типа.
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("Заголовок напоминания не может быть пустым.")
        if not isinstance(due_time, datetime):
            raise ValueError("due_time должен быть объектом datetime.")

        sql = """
        INSERT INTO reminders (title, description, due_time, status, created_at)
        VALUES (?, ?, ?, ?, ?);
        """
        params = (
            title,
            (description or "").strip(),
            self._dt_to_str(due_time),
            STATUS_PENDING,
            self._dt_to_str(datetime.now()),
        )
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(sql, params)
                return cur.lastrowid
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка добавления напоминания: {exc}") from exc

    def get_all_reminders(self) -> List[Tuple]:
        """Возвращает все записи, отсортированные по сроку исполнения."""
        sql = "SELECT id, title, description, due_time, status, created_at FROM reminders;"
        conn = self._get_connection()
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка чтения напоминаний: {exc}") from exc
        return self.sort_by_due_time(rows)

    def get_due_reminders(self) -> List[Tuple]:
        """Возвращает записи со статусом 'Ожидает', у которых срок наступил.

        Используется фоновым монитором: due_time <= сейчас.
        """
        now = self._dt_to_str(datetime.now())
        sql = """
        SELECT id, title, description, due_time, status, created_at
        FROM reminders
        WHERE status = ? AND due_time <= ?;
        """
        conn = self._get_connection()
        try:
            return conn.execute(sql, (STATUS_PENDING, now)).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка выборки наступивших напоминаний: {exc}") from exc

    def get_reminder_by_id(self, reminder_id: int) -> Optional[Tuple]:
        """Возвращает одну запись по id или None, если записи нет."""
        sql = "SELECT id, title, description, due_time, status, created_at FROM reminders WHERE id = ?;"
        conn = self._get_connection()
        try:
            return conn.execute(sql, (reminder_id,)).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка чтения напоминания #{reminder_id}: {exc}") from exc

    def update_status(self, reminder_id: int, status: str) -> bool:
        """Меняет статус записи. Возвращает True, если запись найдена и обновлена."""
        if status not in VALID_STATUSES:
            raise ValueError(f"Недопустимый статус: {status}. Ожидается один из: {VALID_STATUSES}")
        sql = "UPDATE reminders SET status = ? WHERE id = ?;"
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(sql, (status, reminder_id))
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка обновления статуса записи #{reminder_id}: {exc}") from exc

    def delete_reminder(self, reminder_id: int) -> bool:
        """Удаляет запись по id. Возвращает True, если запись существовала."""
        sql = "DELETE FROM reminders WHERE id = ?;"
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(sql, (reminder_id,))
                return cur.rowcount > 0
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка удаления записи #{reminder_id}: {exc}") from exc

    # ------------------------------------------------------------------
    # Служебные операции
    # ------------------------------------------------------------------
    def sort_by_due_time(self, rows: Optional[List[Tuple]] = None) -> List[Tuple]:
        """Сортирует список записей по возрастанию due_time.

        :param rows: готовые строки; если None — сортирует все записи из БД.
        """
        if rows is None:
            rows = self.get_all_reminders()

        def key(row):
            dt = self._str_to_dt(row[3])
            # Записи с некорректной датой уводим в конец списка
            return dt if dt is not None else datetime.max

        return sorted(rows, key=key)

    def mark_overdue(self) -> int:
        """Переводит в статус 'Просрочено' все 'Ожидает', у которых срок
        вышел более 1 минуты назад. Возвращает число обновлённых записей.

        Порог в 1 минуту нужен, чтобы фоновый монитор успел показать
        уведомление о «свежем» напоминании раньше, чем оно станет просроченным.
        """
        threshold = self._dt_to_str(datetime.now() - timedelta(minutes=1))
        sql = """
        UPDATE reminders
        SET status = ?
        WHERE status = ? AND due_time <= ?;
        """
        conn = self._get_connection()
        try:
            with conn:
                cur = conn.execute(sql, (STATUS_OVERDUE, STATUS_PENDING, threshold))
                return cur.rowcount
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка пометки просроченных напоминаний: {exc}") from exc

    def get_reminders_count(self) -> Tuple[int, int]:
        """Возвращает кортеж (всего_записей, активных_записей).

        Активными считаются записи в статусах 'Ожидает' и 'Просрочено'.
        """
        sql_total = "SELECT COUNT(*) FROM reminders;"
        sql_active = "SELECT COUNT(*) FROM reminders WHERE status IN (?, ?);"
        conn = self._get_connection()
        try:
            total = conn.execute(sql_total).fetchone()[0]
            active = conn.execute(sql_active, (STATUS_PENDING, STATUS_OVERDUE)).fetchone()[0]
            return total, active
        except sqlite3.Error as exc:
            raise RuntimeError(f"Ошибка подсчёта напоминаний: {exc}") from exc
