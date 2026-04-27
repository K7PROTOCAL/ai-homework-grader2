from __future__ import annotations

import json
import os
import random
import sqlite3
import string
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generator, Optional

from cryptography.fernet import Fernet, InvalidToken


class DatabaseError(Exception):
    """数据库操作异常。"""


@dataclass
class UserRecord:
    id: int
    username: str
    password: str
    role: str
    contact: Optional[str]
    status: str


@dataclass
class ClassRecord:
    class_id: int
    class_code: str
    teacher_id: int
    class_name: str


@dataclass
class AssignmentRecord:
    id: int
    title: str
    content: str
    standard_answer: str
    deadline: Optional[str]
    target_classes: list[int]
    creator_id: int


class DatabaseManager:
    """SQLite 数据访问层，封装核心表结构与增删改查逻辑。"""

    def __init__(self, db_path: str = "ai_grader.db") -> None:
        self.db_path = db_path
        self.cipher = self._build_cipher()
        self.initialize_database()

    def _build_cipher(self) -> Fernet:
        """从环境变量构建加密器，不在代码中硬编码密钥。"""
        key = os.getenv("PASSWORD_ENCRYPTION_KEY")
        if not key:
            raise DatabaseError(
                "缺少环境变量 PASSWORD_ENCRYPTION_KEY，无法加密存储用户密码。"
            )
        try:
            return Fernet(key.encode("utf-8"))
        except Exception as exc:  # pragma: no cover - 防御性分支
            raise DatabaseError("无效的 PASSWORD_ENCRYPTION_KEY。") from exc

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """获取并自动提交/回滚数据库连接。"""
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise DatabaseError(f"数据库错误: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def initialize_database(self) -> None:
        """初始化所有业务表。"""
        create_users_sql = """
        CREATE TABLE IF NOT EXISTS Users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('teacher', 'student', 'admin')),
            contact TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
        create_classes_sql = """
        CREATE TABLE IF NOT EXISTS Classes (
            class_id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_code TEXT NOT NULL UNIQUE,
            teacher_id INTEGER NOT NULL,
            class_name TEXT NOT NULL,
            FOREIGN KEY (teacher_id) REFERENCES Users(id)
        )
        """
        create_user_class_sql = """
        CREATE TABLE IF NOT EXISTS User_Class (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            class_id INTEGER NOT NULL,
            UNIQUE(user_id, class_id),
            FOREIGN KEY (user_id) REFERENCES Users(id),
            FOREIGN KEY (class_id) REFERENCES Classes(class_id)
        )
        """
        create_assignments_sql = """
        CREATE TABLE IF NOT EXISTS Assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            standard_answer TEXT,
            deadline TEXT,
            target_classes TEXT NOT NULL,
            creator_id INTEGER NOT NULL,
            FOREIGN KEY (creator_id) REFERENCES Users(id)
        )
        """
        create_submissions_sql = """
        CREATE TABLE IF NOT EXISTS Submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            assignment_id INTEGER NOT NULL,
            student_answer TEXT NOT NULL,
            score REAL,
            feedback TEXT,
            status TEXT NOT NULL DEFAULT 'submitted',
            FOREIGN KEY (student_id) REFERENCES Users(id),
            FOREIGN KEY (assignment_id) REFERENCES Assignments(id)
        )
        """
        create_messages_sql = """
        CREATE TABLE IF NOT EXISTS Messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            is_group INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (sender_id) REFERENCES Users(id),
            FOREIGN KEY (receiver_id) REFERENCES Users(id)
        )
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(create_users_sql)
            cursor.execute(create_classes_sql)
            cursor.execute(create_user_class_sql)
            cursor.execute(create_assignments_sql)
            cursor.execute(create_submissions_sql)
            cursor.execute(create_messages_sql)

    def _encrypt_password(self, raw_password: str) -> str:
        try:
            encrypted = self.cipher.encrypt(raw_password.encode("utf-8"))
            return encrypted.decode("utf-8")
        except Exception as exc:
            raise DatabaseError("密码加密失败。") from exc

    def _decrypt_password(self, encrypted_password: str) -> str:
        try:
            raw_password = self.cipher.decrypt(encrypted_password.encode("utf-8"))
            return raw_password.decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise DatabaseError("密码解密失败。") from exc

    def create_user(
        self,
        username: str,
        password: str,
        role: str,
        contact: Optional[str] = None,
        status: str = "active",
    ) -> int:
        """创建用户并返回用户 id。"""
        encrypted_password = self._encrypt_password(password)
        sql = """
        INSERT INTO Users (username, password, role, contact, status)
        VALUES (?, ?, ?, ?, ?)
        """
        try:
            with self._get_connection() as connection:
                cursor = connection.cursor()
                cursor.execute(sql, (username, encrypted_password, role, contact, status))
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("用户名已存在或角色非法。") from exc

    def get_user_by_username(self, username: str) -> Optional[UserRecord]:
        sql = "SELECT * FROM Users WHERE username = ?"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            row = cursor.execute(sql, (username,)).fetchone()
            if row is None:
                return None
            return UserRecord(
                id=int(row["id"]),
                username=str(row["username"]),
                password=str(row["password"]),
                role=str(row["role"]),
                contact=row["contact"],
                status=str(row["status"]),
            )

    def verify_user_password(self, username: str, password: str) -> bool:
        """校验用户名和密码。"""
        user = self.get_user_by_username(username)
        if user is None:
            return False
        stored_password = self._decrypt_password(user.password)
        return stored_password == password

    def verify_user_contact(self, username: str, contact: str) -> bool:
        """校验用户名与绑定联系方式是否匹配。"""
        user = self.get_user_by_username(username)
        if user is None or user.contact is None:
            return False
        return user.contact.strip() == contact.strip()

    def reset_user_password(self, username: str, new_password: str) -> None:
        """重置用户密码。"""
        encrypted_password = self._encrypt_password(new_password)
        sql = "UPDATE Users SET password = ? WHERE username = ?"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, (encrypted_password, username))
            if cursor.rowcount == 0:
                raise DatabaseError("用户不存在，无法重置密码。")

    def _generate_unique_class_code(self) -> str:
        """生成 6 位随机班级码。"""
        candidates = string.ascii_uppercase + string.digits
        max_retry = 20
        sql = "SELECT 1 FROM Classes WHERE class_code = ? LIMIT 1"
        for _ in range(max_retry):
            class_code = "".join(random.choices(candidates, k=6))
            with self._get_connection() as connection:
                cursor = connection.cursor()
                exists = cursor.execute(sql, (class_code,)).fetchone()
                if exists is None:
                    return class_code
        raise DatabaseError("生成班级码失败，请重试。")

    def create_class(self, teacher_id: int, class_name: str) -> dict[str, Any]:
        """创建班级并返回班级信息。"""
        class_code = self._generate_unique_class_code()
        sql = """
        INSERT INTO Classes (class_code, teacher_id, class_name)
        VALUES (?, ?, ?)
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, (class_code, teacher_id, class_name))
            class_id = int(cursor.lastrowid)
        return {"class_id": class_id, "class_code": class_code, "class_name": class_name}

    def get_class_by_code(self, class_code: str) -> Optional[ClassRecord]:
        sql = "SELECT * FROM Classes WHERE class_code = ?"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            row = cursor.execute(sql, (class_code.strip().upper(),)).fetchone()
            if row is None:
                return None
            return ClassRecord(
                class_id=int(row["class_id"]),
                class_code=str(row["class_code"]),
                teacher_id=int(row["teacher_id"]),
                class_name=str(row["class_name"]),
            )

    def list_classes_by_teacher(self, teacher_id: int) -> list[ClassRecord]:
        sql = "SELECT * FROM Classes WHERE teacher_id = ? ORDER BY class_id DESC"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, (teacher_id,)).fetchall()
            return [
                ClassRecord(
                    class_id=int(row["class_id"]),
                    class_code=str(row["class_code"]),
                    teacher_id=int(row["teacher_id"]),
                    class_name=str(row["class_name"]),
                )
                for row in rows
            ]

    def list_classes_by_student(self, student_id: int) -> list[ClassRecord]:
        sql = """
        SELECT c.*
        FROM Classes c
        JOIN User_Class uc ON c.class_id = uc.class_id
        WHERE uc.user_id = ?
        ORDER BY c.class_id DESC
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, (student_id,)).fetchall()
            return [
                ClassRecord(
                    class_id=int(row["class_id"]),
                    class_code=str(row["class_code"]),
                    teacher_id=int(row["teacher_id"]),
                    class_name=str(row["class_name"]),
                )
                for row in rows
            ]

    def add_student_to_class(self, user_id: int, class_id: int) -> None:
        sql = "INSERT INTO User_Class (user_id, class_id) VALUES (?, ?)"
        try:
            with self._get_connection() as connection:
                cursor = connection.cursor()
                cursor.execute(sql, (user_id, class_id))
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("学生加入班级失败，可能是重复加入或班级不存在。") from exc

    def add_student_to_class_by_code(self, user_id: int, class_code: str) -> int:
        class_record = self.get_class_by_code(class_code)
        if class_record is None:
            raise DatabaseError("班级码不存在。")
        self.add_student_to_class(user_id=user_id, class_id=class_record.class_id)
        return class_record.class_id

    def create_assignment(
        self,
        title: str,
        content: str,
        standard_answer: str,
        deadline: Optional[datetime],
        target_classes: list[int],
        creator_id: int,
    ) -> int:
        """创建作业并返回作业 id。"""
        sql = """
        INSERT INTO Assignments (
            title, content, standard_answer, deadline, target_classes, creator_id
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """
        deadline_str = deadline.isoformat() if deadline else None
        target_classes_json = json.dumps(target_classes, ensure_ascii=False)
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                sql,
                (
                    title,
                    content,
                    standard_answer,
                    deadline_str,
                    target_classes_json,
                    creator_id,
                ),
            )
            return int(cursor.lastrowid)

    def get_assignment_by_id(self, assignment_id: int) -> Optional[AssignmentRecord]:
        sql = "SELECT * FROM Assignments WHERE id = ?"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            row = cursor.execute(sql, (assignment_id,)).fetchone()
            if row is None:
                return None
            return AssignmentRecord(
                id=int(row["id"]),
                title=str(row["title"]),
                content=str(row["content"]),
                standard_answer=str(row["standard_answer"] or ""),
                deadline=row["deadline"],
                target_classes=json.loads(row["target_classes"]) if row["target_classes"] else [],
                creator_id=int(row["creator_id"]),
            )

    def list_assignments_by_creator(self, creator_id: int) -> list[AssignmentRecord]:
        sql = "SELECT * FROM Assignments WHERE creator_id = ? ORDER BY id DESC"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, (creator_id,)).fetchall()
            return [
                AssignmentRecord(
                    id=int(row["id"]),
                    title=str(row["title"]),
                    content=str(row["content"]),
                    standard_answer=str(row["standard_answer"] or ""),
                    deadline=row["deadline"],
                    target_classes=json.loads(row["target_classes"]) if row["target_classes"] else [],
                    creator_id=int(row["creator_id"]),
                )
                for row in rows
            ]

    def list_assignments_for_student(self, student_id: int) -> list[AssignmentRecord]:
        classes = self.list_classes_by_student(student_id)
        class_ids = {class_item.class_id for class_item in classes}
        sql = "SELECT * FROM Assignments ORDER BY id DESC"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql).fetchall()
        assignments: list[AssignmentRecord] = []
        for row in rows:
            target_classes = json.loads(row["target_classes"]) if row["target_classes"] else []
            if class_ids.intersection(set(target_classes)):
                assignments.append(
                    AssignmentRecord(
                        id=int(row["id"]),
                        title=str(row["title"]),
                        content=str(row["content"]),
                        standard_answer=str(row["standard_answer"] or ""),
                        deadline=row["deadline"],
                        target_classes=target_classes,
                        creator_id=int(row["creator_id"]),
                    )
                )
        return assignments

    def create_submission(
        self,
        student_id: int,
        assignment_id: int,
        student_answer: str,
        status: str = "submitted",
    ) -> int:
        sql = """
        INSERT INTO Submissions (
            student_id, assignment_id, student_answer, status
        )
        VALUES (?, ?, ?, ?)
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, (student_id, assignment_id, student_answer, status))
            return int(cursor.lastrowid)

    def list_submissions_by_student(self, student_id: int) -> list[dict[str, Any]]:
        sql = """
        SELECT s.id, s.assignment_id, a.title, s.student_answer, s.score, s.feedback, s.status
        FROM Submissions s
        JOIN Assignments a ON s.assignment_id = a.id
        WHERE s.student_id = ?
        ORDER BY s.id DESC
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, (student_id,)).fetchall()
            return [dict(row) for row in rows]

    def list_submissions_for_teacher(self, teacher_id: int) -> list[dict[str, Any]]:
        sql = """
        SELECT
            s.id,
            s.assignment_id,
            a.title AS assignment_title,
            u.username AS student_username,
            s.student_answer,
            s.score,
            s.feedback,
            s.status
        FROM Submissions s
        JOIN Assignments a ON s.assignment_id = a.id
        JOIN Users u ON s.student_id = u.id
        WHERE a.creator_id = ?
        ORDER BY s.id DESC
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, (teacher_id,)).fetchall()
            return [dict(row) for row in rows]

    def get_submission_by_id(self, submission_id: int) -> Optional[dict[str, Any]]:
        sql = """
        SELECT
            s.*,
            a.title AS assignment_title,
            a.standard_answer
        FROM Submissions s
        JOIN Assignments a ON s.assignment_id = a.id
        WHERE s.id = ?
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            row = cursor.execute(sql, (submission_id,)).fetchone()
            return dict(row) if row else None

    def grade_submission(
        self,
        submission_id: int,
        score: float,
        feedback: str,
        status: str = "graded",
    ) -> None:
        sql = """
        UPDATE Submissions
        SET score = ?, feedback = ?, status = ?
        WHERE id = ?
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, (score, feedback, status, submission_id))
            if cursor.rowcount == 0:
                raise DatabaseError("提交记录不存在，无法评分。")

    def list_users(self) -> list[dict[str, Any]]:
        sql = """
        SELECT id, username, role, contact, status
        FROM Users
        ORDER BY id DESC
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql).fetchall()
            return [dict(row) for row in rows]

    def update_user_status(self, user_id: int, status: str) -> None:
        sql = "UPDATE Users SET status = ? WHERE id = ?"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, (status, user_id))
            if cursor.rowcount == 0:
                raise DatabaseError("用户不存在。")

    def delete_user(self, user_id: int) -> None:
        with self._get_connection() as connection:
            cursor = connection.cursor()
            teacher_class_rows = cursor.execute(
                "SELECT class_id FROM Classes WHERE teacher_id = ?",
                (user_id,),
            ).fetchall()
            teacher_class_ids = [int(row["class_id"]) for row in teacher_class_rows]
            if teacher_class_ids:
                class_placeholders = ",".join(["?"] * len(teacher_class_ids))
                cursor.execute(
                    f"DELETE FROM User_Class WHERE class_id IN ({class_placeholders})",
                    tuple(teacher_class_ids),
                )
                cursor.execute(
                    f"DELETE FROM Messages WHERE is_group = 1 AND receiver_id IN ({class_placeholders})",
                    tuple(teacher_class_ids),
                )
                cursor.execute(
                    f"DELETE FROM Classes WHERE class_id IN ({class_placeholders})",
                    tuple(teacher_class_ids),
                )

            assignment_rows = cursor.execute(
                "SELECT id FROM Assignments WHERE creator_id = ?",
                (user_id,),
            ).fetchall()
            assignment_ids = [int(row["id"]) for row in assignment_rows]
            if assignment_ids:
                assignment_placeholders = ",".join(["?"] * len(assignment_ids))
                cursor.execute(
                    f"DELETE FROM Submissions WHERE assignment_id IN ({assignment_placeholders})",
                    tuple(assignment_ids),
                )
                cursor.execute(
                    f"DELETE FROM Assignments WHERE id IN ({assignment_placeholders})",
                    tuple(assignment_ids),
                )

            cursor.execute(
                "DELETE FROM Friend_Requests WHERE sender_id = ? OR receiver_id = ?",
                (user_id, user_id),
            )
            cursor.execute(
                "DELETE FROM Friendships WHERE user_id = ? OR friend_id = ?",
                (user_id, user_id),
            )
            cursor.execute("DELETE FROM User_Class WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM Submissions WHERE student_id = ?", (user_id,))
            cursor.execute("DELETE FROM Messages WHERE sender_id = ? OR receiver_id = ?", (user_id, user_id))
            cursor.execute("DELETE FROM Users WHERE id = ?", (user_id,))
            if cursor.rowcount == 0:
                raise DatabaseError("用户不存在或已删除。")

    def get_user_by_id(self, user_id: int) -> Optional[UserRecord]:
        sql = "SELECT * FROM Users WHERE id = ?"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            row = cursor.execute(sql, (user_id,)).fetchone()
            if row is None:
                return None
            return UserRecord(
                id=int(row["id"]),
                username=str(row["username"]),
                password=str(row["password"]),
                role=str(row["role"]),
                contact=row["contact"],
                status=str(row["status"]),
            )

    def send_message(
        self,
        sender_id: int,
        receiver_id: int,
        content: str,
        is_group: bool = False,
        timestamp: Optional[datetime] = None,
    ) -> int:
        sql = """
        INSERT INTO Messages (sender_id, receiver_id, content, timestamp, is_group)
        VALUES (?, ?, ?, ?, ?)
        """
        actual_time = (timestamp or datetime.now()).isoformat()
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                sql,
                (sender_id, receiver_id, content, actual_time, int(is_group)),
            )
            return int(cursor.lastrowid)

    def list_private_messages(self, user_a: int, user_b: int) -> list[dict[str, Any]]:
        sql = """
        SELECT m.*, su.username AS sender_name, ru.username AS receiver_name
        FROM Messages m
        JOIN Users su ON m.sender_id = su.id
        JOIN Users ru ON m.receiver_id = ru.id
        WHERE m.is_group = 0
          AND (
            (m.sender_id = ? AND m.receiver_id = ?)
            OR
            (m.sender_id = ? AND m.receiver_id = ?)
          )
        ORDER BY m.id ASC
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, (user_a, user_b, user_b, user_a)).fetchall()
            return [dict(row) for row in rows]

    def list_group_messages_for_student(self, student_id: int) -> list[dict[str, Any]]:
        classes = self.list_classes_by_student(student_id)
        class_ids = [class_item.class_id for class_item in classes]
        if not class_ids:
            return []
        placeholders = ",".join(["?"] * len(class_ids))
        sql = f"""
        SELECT m.*, su.username AS sender_name, c.class_name
        FROM Messages m
        JOIN Users su ON m.sender_id = su.id
        JOIN Classes c ON m.receiver_id = c.class_id
        WHERE m.is_group = 1
          AND m.receiver_id IN ({placeholders})
        ORDER BY m.id DESC
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, tuple(class_ids)).fetchall()
            return [dict(row) for row in rows]

    def list_group_messages_for_teacher(self, teacher_id: int) -> list[dict[str, Any]]:
        sql = """
        SELECT m.*, c.class_name
        FROM Messages m
        JOIN Classes c ON m.receiver_id = c.class_id
        WHERE m.is_group = 1 AND m.sender_id = ?
        ORDER BY m.id DESC
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, (teacher_id,)).fetchall()
            return [dict(row) for row in rows]

    def list_chat_users(self, exclude_user_id: int) -> list[dict[str, Any]]:
        sql = """
        SELECT id, username, role, status
        FROM Users
        WHERE id != ? AND status != 'deleted'
        ORDER BY username ASC
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            rows = cursor.execute(sql, (exclude_user_id,)).fetchall()
            return [dict(row) for row in rows]

