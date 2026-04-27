from __future__ import annotations

import json
import os
import random
import sqlite3
import string
import html
import mimetypes
from pathlib import Path
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional, Set

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from cryptography.fernet import Fernet, InvalidToken
from streamlit.errors import StreamlitSecretNotFoundError

from ai_service import MissingDeepSeekAPIKeyError, grade_answer


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
    target_classes: List[int]
    creator_id: int
    attachment_type: Optional[str] = None
    attachment_name: Optional[str] = None
    attachment_path: Optional[str] = None
    attachment_mime: Optional[str] = None
    attachment_size: Optional[int] = None


class DatabaseManager:
    """SQLite 数据访问层。"""

    def __init__(self, db_path: str = "ai_grader.db") -> None:
        self.db_path = db_path
        self.cipher = self._build_cipher()
        self.initialize_database()
        self.ensure_default_admin()

    def _build_cipher(self) -> Fernet:
        key = os.getenv("PASSWORD_ENCRYPTION_KEY", "").strip()
        if not key:
            key = self._read_streamlit_secret("PASSWORD_ENCRYPTION_KEY")
        if not key:
            key = self._ensure_local_password_key()
        if not key:
            raise DatabaseError(
                "缺少 PASSWORD_ENCRYPTION_KEY。"
                "请在系统环境变量或 .streamlit/secrets.toml 中配置该值。"
            )
        try:
            return Fernet(key.encode("utf-8"))
        except Exception as exc:
            raise DatabaseError(
                "无效的 PASSWORD_ENCRYPTION_KEY。"
                "请使用 Fernet.generate_key() 生成 44 位 Base64 密钥。"
            ) from exc

    @staticmethod
    def _read_streamlit_secret(secret_name: str) -> str:
        """安全读取 Streamlit secrets，不因 secrets 文件缺失而崩溃。"""
        try:
            value = st.secrets[secret_name]
        except (StreamlitSecretNotFoundError, KeyError):
            return ""
        return str(value).strip()

    @staticmethod
    def _ensure_local_password_key() -> str:
        """
        本地开发兜底：若缺少配置，自动写入项目级 .streamlit/secrets.toml。
        该文件已被 .gitignore 忽略，不会上传到 GitHub。
        """
        secrets_path = Path(".streamlit") / "secrets.toml"
        try:
            secrets_path.parent.mkdir(parents=True, exist_ok=True)
            if secrets_path.exists():
                return ""
            generated_key = Fernet.generate_key().decode("utf-8")
            secrets_path.write_text(
                (
                    "# Auto-generated for local development.\n"
                    "# Replace with your own key for production deployments.\n"
                    f'PASSWORD_ENCRYPTION_KEY = "{generated_key}"\n'
                ),
                encoding="utf-8",
            )
            return generated_key
        except OSError:
            return ""

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
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
        sql_list = [
            """
            CREATE TABLE IF NOT EXISTS Users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('teacher', 'student', 'admin')),
                contact TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Classes (
                class_id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_code TEXT NOT NULL UNIQUE,
                teacher_id INTEGER NOT NULL,
                class_name TEXT NOT NULL,
                FOREIGN KEY (teacher_id) REFERENCES Users(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS User_Class (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                class_id INTEGER NOT NULL,
                UNIQUE(user_id, class_id),
                FOREIGN KEY (user_id) REFERENCES Users(id),
                FOREIGN KEY (class_id) REFERENCES Classes(class_id)
            )
            """,
            """
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
            """,
            """
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
            """,
            """
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
            """,
            """
            CREATE TABLE IF NOT EXISTS Friend_Requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                UNIQUE(sender_id, receiver_id),
                FOREIGN KEY (sender_id) REFERENCES Users(id),
                FOREIGN KEY (receiver_id) REFERENCES Users(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Friendships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                friend_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, friend_id),
                FOREIGN KEY (user_id) REFERENCES Users(id),
                FOREIGN KEY (friend_id) REFERENCES Users(id)
            )
            """,
        ]
        with self._get_connection() as connection:
            cursor = connection.cursor()
            for sql in sql_list:
                cursor.execute(sql)
            assignment_columns = {
                str(col["name"]) for col in cursor.execute("PRAGMA table_info(Assignments)").fetchall()
            }
            if "attachment_type" not in assignment_columns:
                cursor.execute("ALTER TABLE Assignments ADD COLUMN attachment_type TEXT")
            if "attachment_name" not in assignment_columns:
                cursor.execute("ALTER TABLE Assignments ADD COLUMN attachment_name TEXT")
            if "attachment_path" not in assignment_columns:
                cursor.execute("ALTER TABLE Assignments ADD COLUMN attachment_path TEXT")
            if "attachment_mime" not in assignment_columns:
                cursor.execute("ALTER TABLE Assignments ADD COLUMN attachment_mime TEXT")
            if "attachment_size" not in assignment_columns:
                cursor.execute("ALTER TABLE Assignments ADD COLUMN attachment_size INTEGER")

    def ensure_default_admin(self) -> None:
        """
        仅在数据库没有任何用户时创建默认管理员。

        避免用户手动删除“皇帝”后又被自动重建，导致 ID 持续变化。
        """
        admin_username = "皇帝"
        admin_password = "123456"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            user_count_row = cursor.execute("SELECT COUNT(1) AS cnt FROM Users").fetchone()
            user_count = int(user_count_row["cnt"]) if user_count_row is not None else 0
        if user_count == 0:
            self.create_user(
                username=admin_username,
                password=admin_password,
                role="admin",
                contact=None,
                status="active",
            )

    def _encrypt_password(self, raw_password: str) -> str:
        try:
            return self.cipher.encrypt(raw_password.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise DatabaseError("密码加密失败。") from exc

    def _decrypt_password(self, encrypted_password: str) -> str:
        try:
            return self.cipher.decrypt(encrypted_password.encode("utf-8")).decode("utf-8")
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
        # 先做业务层校验，避免直接抛出底层 UNIQUE 约束错误给前端
        existing_user = self.get_user_by_username(username)
        if existing_user is not None:
            raise DatabaseError("用户名已存在，请更换后重试。")
        encrypted_password = self._encrypt_password(password)
        sql = "INSERT INTO Users (username, password, role, contact, status) VALUES (?, ?, ?, ?, ?)"
        try:
            with self._get_connection() as connection:
                cursor = connection.cursor()
                cursor.execute(sql, (username, encrypted_password, role, contact, status))
                return int(cursor.lastrowid)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError("注册失败，请稍后重试。") from exc

    def get_user_by_username(self, username: str) -> Optional[UserRecord]:
        sql = "SELECT * FROM Users WHERE username = ?"
        with self._get_connection() as connection:
            row = connection.cursor().execute(sql, (username,)).fetchone()
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
        user = self.get_user_by_username(username)
        if user is None:
            return False
        return self._decrypt_password(user.password) == password

    def verify_user_contact(self, username: str, contact: str) -> bool:
        user = self.get_user_by_username(username)
        if user is None or user.contact is None:
            return False
        return user.contact.strip() == contact.strip()

    def reset_user_password(self, username: str, new_password: str) -> None:
        sql = "UPDATE Users SET password = ? WHERE username = ?"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, (self._encrypt_password(new_password), username))
            if cursor.rowcount == 0:
                raise DatabaseError("用户不存在，无法重置密码。")

    def _generate_unique_class_code(self) -> str:
        chars = string.ascii_uppercase + string.digits
        for _ in range(20):
            class_code = "".join(random.choices(chars, k=6))
            with self._get_connection() as connection:
                row = connection.cursor().execute(
                    "SELECT 1 FROM Classes WHERE class_code = ? LIMIT 1", (class_code,)
                ).fetchone()
                if row is None:
                    return class_code
        raise DatabaseError("生成班级码失败，请重试。")

    def create_class(self, teacher_id: int, class_name: str) -> Dict[str, Any]:
        class_code = self._generate_unique_class_code()
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO Classes (class_code, teacher_id, class_name) VALUES (?, ?, ?)",
                (class_code, teacher_id, class_name),
            )
            class_id = int(cursor.lastrowid)
        return {"class_id": class_id, "class_code": class_code, "class_name": class_name}

    def list_classes_by_teacher(self, teacher_id: int) -> List[ClassRecord]:
        sql = "SELECT * FROM Classes WHERE teacher_id = ? ORDER BY class_id DESC"
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (teacher_id,)).fetchall()
        return [
            ClassRecord(
                class_id=int(r["class_id"]),
                class_code=str(r["class_code"]),
                teacher_id=int(r["teacher_id"]),
                class_name=str(r["class_name"]),
            )
            for r in rows
        ]

    def list_classes_by_student(self, student_id: int) -> List[ClassRecord]:
        sql = """
        SELECT c.* FROM Classes c
        JOIN User_Class uc ON c.class_id = uc.class_id
        WHERE uc.user_id = ?
        ORDER BY c.class_id DESC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (student_id,)).fetchall()
        return [
            ClassRecord(
                class_id=int(r["class_id"]),
                class_code=str(r["class_code"]),
                teacher_id=int(r["teacher_id"]),
                class_name=str(r["class_name"]),
            )
            for r in rows
        ]

    def add_student_to_class_by_code(self, user_id: int, class_code: str) -> int:
        with self._get_connection() as connection:
            cursor = connection.cursor()
            row = cursor.execute(
                "SELECT class_id FROM Classes WHERE class_code = ?",
                (class_code.strip().upper(),),
            ).fetchone()
            if row is None:
                raise DatabaseError("班级码不存在。")
            class_id = int(row["class_id"])
            try:
                cursor.execute(
                    "INSERT INTO User_Class (user_id, class_id) VALUES (?, ?)",
                    (user_id, class_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError("加入失败，可能已在班级中。") from exc
        return class_id

    def create_assignment(
        self,
        title: str,
        content: str,
        standard_answer: str,
        deadline: Optional[datetime],
        target_classes: List[int],
        creator_id: int,
        attachment_type: Optional[str] = None,
        attachment_name: Optional[str] = None,
        attachment_path: Optional[str] = None,
        attachment_mime: Optional[str] = None,
        attachment_size: Optional[int] = None,
    ) -> int:
        sql = """
        INSERT INTO Assignments (
            title, content, standard_answer, deadline, target_classes, creator_id,
            attachment_type, attachment_name, attachment_path, attachment_mime, attachment_size
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                sql,
                (
                    title,
                    content,
                    standard_answer,
                    deadline.isoformat() if deadline else None,
                    json.dumps(target_classes, ensure_ascii=False),
                    creator_id,
                    attachment_type,
                    attachment_name,
                    attachment_path,
                    attachment_mime,
                    attachment_size,
                ),
            )
            return int(cursor.lastrowid)

    def list_assignments_by_creator(self, creator_id: int) -> List[Dict[str, Any]]:
        sql = "SELECT id, title, deadline, target_classes FROM Assignments WHERE creator_id = ? ORDER BY id DESC"
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (creator_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_class_name_map(self, teacher_id: int) -> Dict[int, str]:
        classes = self.list_classes_by_teacher(teacher_id)
        return {item.class_id: item.class_name for item in classes}

    def list_assignments_for_student(self, student_id: int) -> List[AssignmentRecord]:
        class_ids = {item.class_id for item in self.list_classes_by_student(student_id)}
        with self._get_connection() as connection:
            rows = connection.cursor().execute("SELECT * FROM Assignments ORDER BY id DESC").fetchall()
        results: List[AssignmentRecord] = []
        for row in rows:
            target_classes = json.loads(row["target_classes"]) if row["target_classes"] else []
            if class_ids.intersection(set(target_classes)):
                results.append(
                    AssignmentRecord(
                        id=int(row["id"]),
                        title=str(row["title"]),
                        content=str(row["content"]),
                        standard_answer=str(row["standard_answer"] or ""),
                        deadline=row["deadline"],
                        target_classes=target_classes,
                        creator_id=int(row["creator_id"]),
                        attachment_type=str(row["attachment_type"] or "") or None,
                        attachment_name=str(row["attachment_name"] or "") or None,
                        attachment_path=str(row["attachment_path"] or "") or None,
                        attachment_mime=str(row["attachment_mime"] or "") or None,
                        attachment_size=int(row["attachment_size"]) if row["attachment_size"] is not None else None,
                    )
                )
        return results

    def create_submission(self, student_id: int, assignment_id: int, student_answer: str) -> int:
        sql = """
        INSERT INTO Submissions (student_id, assignment_id, student_answer, status)
        VALUES (?, ?, ?, 'submitted')
        """
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, (student_id, assignment_id, student_answer))
            return int(cursor.lastrowid)

    def grade_submission(self, submission_id: int, score: float, feedback: str, status: str = "graded") -> None:
        sql = "UPDATE Submissions SET score = ?, feedback = ?, status = ? WHERE id = ?"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, (score, feedback, status, submission_id))
            if cursor.rowcount == 0:
                raise DatabaseError("提交记录不存在，无法评分。")

    def get_submission_detail(self, submission_id: int) -> Optional[Dict[str, Any]]:
        sql = """
        SELECT s.*, a.standard_answer, a.title AS assignment_title
        FROM Submissions s JOIN Assignments a ON s.assignment_id = a.id
        WHERE s.id = ?
        """
        with self._get_connection() as connection:
            row = connection.cursor().execute(sql, (submission_id,)).fetchone()
            return dict(row) if row else None

    def list_submissions_by_student(self, student_id: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT s.id, s.assignment_id, a.title, a.content, a.standard_answer,
               s.student_answer, s.score, s.feedback, s.status
        FROM Submissions s JOIN Assignments a ON s.assignment_id = a.id
        WHERE s.student_id = ? ORDER BY s.id DESC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (student_id,)).fetchall()
            return [dict(r) for r in rows]

    def list_submissions_for_teacher(self, teacher_id: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT s.id, s.assignment_id, s.student_id,
               a.title AS assignment_title, a.content, a.standard_answer,
               u.username AS student_username,
               s.student_answer, s.score, s.feedback, s.status
        FROM Submissions s
        JOIN Assignments a ON s.assignment_id = a.id
        JOIN Users u ON s.student_id = u.id
        WHERE a.creator_id = ?
        ORDER BY s.id DESC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (teacher_id,)).fetchall()
            return [dict(r) for r in rows]

    def list_users(self) -> List[Dict[str, Any]]:
        with self._get_connection() as connection:
            rows = connection.cursor().execute(
                "SELECT id, username, role, contact, status FROM Users ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_user_status(self, user_id: int, status: str) -> None:
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute("UPDATE Users SET status = ? WHERE id = ?", (status, user_id))
            if cursor.rowcount == 0:
                raise DatabaseError("用户不存在。")

    def delete_user(self, user_id: int) -> None:
        with self._get_connection() as connection:
            cursor = connection.cursor()
            # 删除教师关联数据（班级、作业、群聊消息），避免外键约束失败
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

            # 删除好友体系和用户自身提交/消息数据
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

    def list_chat_users(self, exclude_user_id: int) -> List[Dict[str, Any]]:
        with self._get_connection() as connection:
            rows = connection.cursor().execute(
                "SELECT id, username, role, status FROM Users WHERE id != ? ORDER BY username ASC",
                (exclude_user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def search_users(self, keyword: str, current_user_id: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT id, username, role, status
        FROM Users
        WHERE id != ? AND username LIKE ? AND status = 'active'
        ORDER BY username ASC
        LIMIT 20
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (current_user_id, f"%{keyword}%")).fetchall()
            return [dict(r) for r in rows]

    def list_friends(self, user_id: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT u.id, u.username, u.role, u.status, u.contact
        FROM Friendships f
        JOIN Users u ON u.id = f.friend_id
        WHERE f.user_id = ?
        ORDER BY u.username ASC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (user_id,)).fetchall()
            return [dict(r) for r in rows]

    def send_friend_request(self, sender_id: int, receiver_id: int) -> None:
        if sender_id == receiver_id:
            raise DatabaseError("不能添加自己为好友。")
        # already friends
        with self._get_connection() as connection:
            cursor = connection.cursor()
            exists = cursor.execute(
                "SELECT 1 FROM Friendships WHERE user_id = ? AND friend_id = ? LIMIT 1",
                (sender_id, receiver_id),
            ).fetchone()
            if exists:
                raise DatabaseError("你们已经是好友。")
            # prevent opposite pending duplicates
            pending_reverse = cursor.execute(
                "SELECT 1 FROM Friend_Requests WHERE sender_id = ? AND receiver_id = ? AND status = 'pending' LIMIT 1",
                (receiver_id, sender_id),
            ).fetchone()
            if pending_reverse:
                raise DatabaseError("对方已向你发起请求，请到请求列表处理。")
            try:
                cursor.execute(
                    "INSERT INTO Friend_Requests (sender_id, receiver_id, status, created_at) VALUES (?, ?, 'pending', ?)",
                    (sender_id, receiver_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError("好友请求已发送，请勿重复提交。") from exc

    def list_received_friend_requests(self, user_id: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT fr.id, fr.sender_id, u.username AS sender_name, u.role AS sender_role, fr.created_at
        FROM Friend_Requests fr
        JOIN Users u ON u.id = fr.sender_id
        WHERE fr.receiver_id = ? AND fr.status = 'pending'
        ORDER BY fr.id DESC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (user_id,)).fetchall()
            return [dict(r) for r in rows]

    def respond_friend_request(self, request_id: int, receiver_id: int, accept: bool) -> None:
        with self._get_connection() as connection:
            cursor = connection.cursor()
            request = cursor.execute(
                "SELECT sender_id, receiver_id, status FROM Friend_Requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if request is None or int(request["receiver_id"]) != receiver_id:
                raise DatabaseError("好友请求不存在。")
            if request["status"] != "pending":
                raise DatabaseError("该请求已处理。")
            new_status = "accepted" if accept else "rejected"
            cursor.execute("UPDATE Friend_Requests SET status = ? WHERE id = ?", (new_status, request_id))
            if accept:
                now_str = datetime.now().isoformat()
                for uid, fid in [(int(request["sender_id"]), receiver_id), (receiver_id, int(request["sender_id"]))]:
                    try:
                        cursor.execute(
                            "INSERT INTO Friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
                            (uid, fid, now_str),
                        )
                    except sqlite3.IntegrityError:
                        pass

    def send_message(
        self,
        sender_id: int,
        receiver_id: int,
        content: str,
        is_group: bool = False,
        timestamp: Optional[datetime] = None,
    ) -> int:
        sql = "INSERT INTO Messages (sender_id, receiver_id, content, timestamp, is_group) VALUES (?, ?, ?, ?, ?)"
        with self._get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                sql,
                (sender_id, receiver_id, content, (timestamp or datetime.now()).isoformat(), int(is_group)),
            )
            return int(cursor.lastrowid)

    def list_private_messages(self, user_a: int, user_b: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT * FROM Messages
        WHERE is_group = 0 AND ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))
        ORDER BY id ASC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (user_a, user_b, user_b, user_a)).fetchall()
            return [dict(r) for r in rows]

    def list_group_messages_for_student(self, student_id: int) -> List[Dict[str, Any]]:
        class_ids = [item.class_id for item in self.list_classes_by_student(student_id)]
        if not class_ids:
            return []
        placeholders = ",".join(["?"] * len(class_ids))
        sql = f"""
        SELECT m.*, c.class_name FROM Messages m
        JOIN Classes c ON m.receiver_id = c.class_id
        WHERE m.is_group = 1 AND m.receiver_id IN ({placeholders})
        ORDER BY m.id DESC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, tuple(class_ids)).fetchall()
            return [dict(r) for r in rows]

    def list_group_messages_for_teacher(self, teacher_id: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT m.*, c.class_name FROM Messages m
        JOIN Classes c ON m.receiver_id = c.class_id
        WHERE m.is_group = 1 AND m.sender_id = ?
        ORDER BY m.id DESC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (teacher_id,)).fetchall()
            return [dict(r) for r in rows]

    def list_group_messages_by_class(self, class_id: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT m.id, m.sender_id, m.receiver_id, m.content, m.timestamp,
               u.username AS sender_name
        FROM Messages m
        JOIN Users u ON m.sender_id = u.id
        WHERE m.is_group = 1 AND m.receiver_id = ?
        ORDER BY m.id ASC
        """
        with self._get_connection() as connection:
            rows = connection.cursor().execute(sql, (class_id,)).fetchall()
            return [dict(r) for r in rows]


ROLE_LABEL_TO_VALUE: Dict[str, str] = {"老师": "teacher", "学生": "student", "管理员": "admin"}
ROLE_VALUE_TO_LABEL: Dict[str, str] = {v: k for k, v in ROLE_LABEL_TO_VALUE.items()}
ROLE_PAGES: Dict[str, List[str]] = {
    "teacher": ["班级管理", "作业发布", "批改中心", "消息中心"],
    "student": ["班级加入", "作业提交", "提交记录", "消息中心"],
    "admin": ["用户管理"],
}


def inject_custom_css(is_logged_in: bool = False) -> None:
    # ── 基础 token + 全局字体（始终注入）────────────────────────────────────
    base_css = """
    <style>
    /* ── Q 弹微交互关键帧 (Elastic Interaction · 60FPS on transform only) ── */
    @keyframes elasticJiggle {
        0%   { transform: translateY(0)      scale(1);      }
        18%  { transform: translateY(-3px)   scale(0.979);  }
        38%  { transform: translateY(1.4px)  scale(1.010);  }
        58%  { transform: translateY(-1px)   scale(1.003);  }
        78%  { transform: translateY(0.4px)  scale(0.999);  }
        100% { transform: translateY(0)      scale(1);      }
    }
    @keyframes elasticPress {
        0%   { transform: scale(1);     }
        30%  { transform: scale(0.962); }
        62%  { transform: scale(1.018); }
        84%  { transform: scale(0.997); }
        100% { transform: scale(1);     }
    }

    /* ── 社交管理 · 选项卡专属 Q 弹（Apple 质感，更柔和） ──
       仅作用于 transform: translateY/scale，60FPS 稳定，无 layout/paint 抖动 */
    @keyframes mcNavTabBounce {
        0%   { transform: translateY(0)      scale(1);     }
        20%  { transform: translateY(-2.6px) scale(0.992); }
        44%  { transform: translateY(1.2px)  scale(1.006); }
        66%  { transform: translateY(-0.5px) scale(1.002); }
        100% { transform: translateY(0)      scale(1);     }
    }
    /* 点击瞬间：scale(0.98) → 轻微 overshoot → 弹回 */
    @keyframes mcNavTabPress {
        0%   { transform: scale(1)    translateY(0);    }
        36%  { transform: scale(0.98) translateY(0.4px); }
        66%  { transform: scale(1.012) translateY(-0.3px); }
        100% { transform: scale(1)    translateY(0);    }
    }

    :root {
        --bg:           #F8FAFC;
        --surface:      #FFFFFF;
        --border:       #E2E8F0;
        --border-light: #F1F5F9;
        --text:         #0F172A;
        --text-2:       #475569;
        --text-muted:   #94A3B8;
        --primary:      #4F86F7;
        --primary-50:   #EFF6FF;
        --primary-600:  #3B6FDC;
        --radius:       16px;
        --radius-sm:    10px;
        --ease:   cubic-bezier(0.4,0,0.2,1);
        --spring: cubic-bezier(0.16,1,0.3,1);
        /* 全站 BaseWeb 输入：清晨天蓝光晕（冷色、无粉/无暖色描边） */
        --input-surface:            #ffffff;
        --input-sky-border:         rgba(125, 211, 252, 0.44);
        --input-sky-border-hover:   rgba(56, 189, 248, 0.5);
        --input-sky-border-focus:   rgba(14, 165, 233, 0.55);
        --input-halo-rest:   0 0 0 1px rgba(224, 242, 254, 0.95),
            0 1px 2px rgba(14, 165, 233, 0.04),
            0 2px 10px -2px rgba(125, 211, 252, 0.22);
        --input-halo-hover:  0 0 0 1px rgba(191, 219, 254, 0.95),
            0 1px 3px rgba(14, 165, 233, 0.07),
            0 4px 16px -4px rgba(125, 211, 252, 0.3);
        --input-halo-focus:  0 0 0 2px rgba(125, 211, 252, 0.4),
            0 2px 8px rgba(14, 165, 233, 0.09),
            0 8px 24px -6px rgba(56, 189, 248, 0.25);
    }

    html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Helvetica Neue", "Arial", sans-serif !important;
        -webkit-font-smoothing: antialiased !important;
        -moz-osx-font-smoothing: grayscale !important;
        text-rendering: optimizeLegibility !important;
    }
    /* Force form widgets and BaseWeb inner inputs to inherit the Apple stack
       (BaseWeb sometimes resets font-family on inner native inputs). */
    button, input, textarea, select,
    [data-baseweb] input,
    [data-baseweb] textarea,
    [data-baseweb] select,
    [data-baseweb="input"] input,
    [data-baseweb="textarea"] textarea,
    [data-baseweb="select"] *,
    [data-testid="stMarkdownContainer"],
    [data-testid="stMarkdownContainer"] * {
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Helvetica Neue", "Arial", sans-serif !important;
        -webkit-font-smoothing: antialiased !important;
        -moz-osx-font-smoothing: grayscale !important;
    }

    h1,h2,h3,h4 { color:var(--text) !important; font-weight:700 !important; letter-spacing:-0.025em !important; }
    p,label,span,li,.stMarkdown,.stText { color:var(--text-2) !important; }
    .stCaption,[data-testid="stCaptionContainer"] * { color:var(--text-muted) !important; font-size:0.8rem !important; }

    /* ── 全站 BaseWeb 输入 / 文本域 / 选择器：清晨天蓝光晕，描边只在外层 ── */
    [data-baseweb="input"] input,
    [data-baseweb="textarea"] textarea {
        color:var(--text) !important;
        background:var(--input-surface) !important;
        font-size:0.9375rem !important;
        line-height:1.5 !important;
        caret-color:#0ea5e9 !important;
        border:none !important;
        outline:none !important;
        box-shadow:none !important;
        -webkit-appearance:none !important;
        appearance:none !important;
        transition:background 0.3s var(--ease), color 0.3s var(--ease) !important;
    }
    [data-baseweb="textarea"] textarea {
        /* 去掉浏览器默认的右下角 textarea 缩放手柄（易呈现为“黑线”/斜纹） */
        resize:none !important;
    }
    [data-baseweb="textarea"] textarea::-webkit-resizer {
        display:none !important;
    }
    [data-baseweb="input"] input::placeholder,
    [data-baseweb="textarea"] textarea::placeholder {
        color:#94A3B8 !important;
    }
    [data-baseweb="select"] > div {
        color:var(--text) !important;
        background:var(--input-surface) !important;
        border:none !important;
        outline:none !important;
        box-shadow:none !important;
    }
    /* 去掉 BaseWeb 文本域内层容器的错层/底边硬线（勿动普通 input 内部 flex，以免日期等控件异常） */
    [data-baseweb="textarea"] > div,
    [data-baseweb="textarea"] > div > div {
        background:transparent !important;
        border:none !important;
        box-shadow:none !important;
    }
    [data-baseweb="input"],
    [data-baseweb="textarea"],
    [data-baseweb="select"] {
        background:var(--input-surface) !important;
        border:1px solid var(--input-sky-border) !important;
        border-radius:12px !important;
        box-shadow:var(--input-halo-rest) !important;
        transition:box-shadow 0.3s var(--ease), border-color 0.3s var(--ease), background 0.3s var(--ease) !important;
    }
    [data-baseweb="input"]:focus-within,
    [data-baseweb="textarea"]:focus-within,
    [data-baseweb="select"]:focus-within {
        border-color:var(--input-sky-border-focus) !important;
        background:var(--input-surface) !important;
        box-shadow:var(--input-halo-focus) !important;
        outline:none !important;
    }
    [data-baseweb="input"]:focus-within input,
    [data-baseweb="textarea"]:focus-within textarea,
    [data-baseweb="select"]:focus-within > div {
        background:var(--input-surface) !important;
    }
    [data-baseweb="input"]:hover:not(:focus-within),
    [data-baseweb="textarea"]:hover:not(:focus-within),
    [data-baseweb="select"]:hover:not(:focus-within) {
        border-color:var(--input-sky-border-hover) !important;
        box-shadow:var(--input-halo-hover) !important;
    }
    /* 隐藏输入框右下角的默认英文提交提示文案 */
    [data-testid="InputInstructions"] {
        display:none !important;
    }
    /* ── 密码显示开关：透明融入输入框 + 细线黑色图标 ── */
    /* 1) 让按钮及其所有容器/伪元素都透明，融入输入框背景 */
    html body [data-baseweb="input"] > div > div:last-child,
    html body [data-baseweb="input"] > div > div:last-child > div,
    html body [data-baseweb="input"] [data-baseweb="button"],
    html body [data-baseweb="input"] [data-baseweb="button"] > div,
    html body [data-baseweb="input"] button,
    html body [data-baseweb="input"] button > div,
    html body [data-baseweb="input"] button > span,
    html body button[kind="iconButton"],
    html body button[data-testid="stPasswordVisibilityButton"],
    html body button[aria-label="Show password text"],
    html body button[aria-label="Hide password text"] {
        background:transparent !important;
        background-color:transparent !important;
        background-image:none !important;
        border:none !important;
        box-shadow:none !important;
        outline:none !important;
        filter:none !important;
        clip-path:none !important;
    }
    /* 1.1) 关键：去掉 Streamlit/BaseWeb iconButton 的 ::before / ::after 深色覆盖层 */
    html body [data-baseweb="input"] button::before,
    html body [data-baseweb="input"] button::after,
    html body button[kind="iconButton"]::before,
    html body button[kind="iconButton"]::after,
    html body button[data-testid="stPasswordVisibilityButton"]::before,
    html body button[data-testid="stPasswordVisibilityButton"]::after {
        background:transparent !important;
        background-color:transparent !important;
        background-image:none !important;
        box-shadow:none !important;
        content:none !important;
        display:none !important;
    }
    html body [data-baseweb="input"] button {
        padding:0 0.6rem !important;
        cursor:pointer !important;
        border-radius:0 !important;
    }
    /* 2) hover / focus / active 全部保持透明，无深色 ripple */
    html body [data-baseweb="input"] button:hover,
    html body [data-baseweb="input"] button:focus,
    html body [data-baseweb="input"] button:focus-visible,
    html body [data-baseweb="input"] button:active,
    html body button[kind="iconButton"]:hover,
    html body button[kind="iconButton"]:focus,
    html body button[kind="iconButton"]:active,
    html body button[data-testid="stPasswordVisibilityButton"]:hover,
    html body button[data-testid="stPasswordVisibilityButton"]:focus,
    html body button[data-testid="stPasswordVisibilityButton"]:active {
        background:transparent !important;
        background-color:transparent !important;
        background-image:none !important;
        border:none !important;
        box-shadow:none !important;
        outline:none !important;
        transform:none !important;
    }
    /* 3) 图标：细线黑色（fill 走黑、stroke 设极细，模拟纤细线条） */
    html body [data-baseweb="input"] button svg,
    html body button[kind="iconButton"] svg,
    html body button[data-testid="stPasswordVisibilityButton"] svg,
    html body button[aria-label="Show password text"] svg,
    html body button[aria-label="Hide password text"] svg {
        width:16px !important;
        height:16px !important;
        color:#0F172A !important;
        fill:#0F172A !important;
        stroke:none !important;
        opacity:0.9 !important;
    }
    html body [data-baseweb="input"] button svg *,
    html body button[kind="iconButton"] svg *,
    html body button[data-testid="stPasswordVisibilityButton"] svg *,
    html body button[aria-label="Show password text"] svg *,
    html body button[aria-label="Hide password text"] svg * {
        fill:#0F172A !important;
        stroke:none !important;
        stroke-width:0 !important;
    }

    /* ── 按钮（基础态） ── */
    .stButton > button {
        border-radius:var(--radius-sm) !important;
        border:1px solid var(--border) !important;
        color:var(--text-2) !important;
        background:var(--surface) !important;
        font-size:0.9rem !important;
        font-weight:500 !important;
        padding:0.5rem 1.25rem !important;
        transition:box-shadow 0.22s var(--ease), border-color 0.22s var(--ease) !important;
        cursor:pointer !important;
        will-change:transform !important;
    }
    /* hover：只加阴影+边框微调，触发一次 Q 弹抖动，不改颜色 */
    .stButton > button:hover {
        border-color:#BFDBFE !important;
        box-shadow:0 5px 16px rgba(79,134,247,0.15) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    /* active：按下手感——缩放弹回 */
    .stButton > button:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    .stButton > button[kind="primary"] {
        background:var(--primary) !important;
        color:#FFFFFF !important;
        border-color:var(--primary) !important;
        height:2.75rem !important;
        font-weight:600 !important;
        letter-spacing:-0.01em !important;
        font-size:0.9375rem !important;
    }
    .stButton > button[kind="primary"]:hover {
        background:var(--primary-600) !important;
        border-color:var(--primary-600) !important;
        box-shadow:0 4px 16px rgba(79,134,247,0.30) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    .stButton > button[kind="primary"]:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    /* 教师端 — 创建班级：天蓝渐变 + 白字（与功能卡片协调） */
    .st-key-teacher_create_class_btn button {
        background:linear-gradient(135deg, #38BDF8 0%, #0EA5E9 100%) !important;
        color:#FFFFFF !important;
        border:none !important;
        box-shadow:0 4px 14px rgba(14,165,233,0.28) !important;
    }
    .st-key-teacher_create_class_btn button:hover {
        background:linear-gradient(135deg, #0EA5E9 0%, #0284C7 100%) !important;
        color:#FFFFFF !important;
        border:none !important;
        box-shadow:0 6px 20px rgba(14,165,233,0.38) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    .st-key-teacher_create_class_btn button:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    /* 教师端 — 发布作业：红底黑字 */
    .st-key-teacher_publish_assignment_btn button {
        background:#FF4D4F !important;
        color:#111827 !important;
        border-color:#FF4D4F !important;
    }
    .st-key-teacher_publish_assignment_btn button:hover {
        background:#FF4D4F !important;
        color:#111827 !important;
        border-color:#FF4D4F !important;
        box-shadow:0 5px 18px rgba(244,63,94,0.24) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    .st-key-teacher_publish_assignment_btn button:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    /* 学生端加入班级按钮：红底黑字 */
    .st-key-student_join_class_btn button {
        background:#FF4D4F !important;
        color:#111827 !important;
        border-color:#FF4D4F !important;
    }
    .st-key-student_join_class_btn button:hover {
        background:#FF4D4F !important;
        color:#111827 !important;
        border-color:#FF4D4F !important;
        box-shadow:0 5px 18px rgba(244,63,94,0.24) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    .st-key-student_join_class_btn button:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }

    /* ── Tabs 全局交互（注册/登录等标签）── */
    [data-baseweb="tab-list"] [data-baseweb="tab"] {
        border-radius:10px !important;
        background:#FFFFFF !important;
        color:#0F172A !important;
        border:1px solid #E2E8F0 !important;
        transition:box-shadow 0.22s var(--ease), border-color 0.22s var(--ease) !important;
        will-change:transform !important;
    }
    /* hover：不改背景/文字色，仅加阴影+边框 + Q 弹 */
    [data-baseweb="tab-list"] [data-baseweb="tab"]:hover {
        border-color:#BFDBFE !important;
        box-shadow:0 4px 14px rgba(79,134,247,0.17) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    [data-baseweb="tab-list"] [data-baseweb="tab"]:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"] {
        background:#FFFFFF !important;
        color:#0F172A !important;
        border-color:#BFDBFE !important;
        box-shadow:0 8px 20px rgba(79,134,247,0.18) !important;
    }
    [data-baseweb="tab-list"] [data-baseweb="tab"][aria-selected="true"]:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }

    /* ── 提示框 ── */
    [data-testid="stAlert"] { border-radius:var(--radius-sm) !important; border-width:1px !important; font-size:0.9rem !important; }
    [data-testid="stAlert"][kind="success"] { background:#F0FDF4 !important; border-color:#BBF7D0 !important; color:#166534 !important; }
    [data-testid="stAlert"][kind="warning"] { background:#FFFBEB !important; border-color:#FDE68A !important; color:#92400E !important; }
    [data-testid="stAlert"][kind="error"]   { background:#FFF1F2 !important; border-color:#FECDD3 !important; color:#9F1239 !important; }
    [data-testid="stAlert"][kind="info"]    { background:#EFF6FF !important; border-color:#BFDBFE !important; color:#1E40AF !important; }

    /* ── 表格 ── */
    [data-testid="stDataFrame"] { border:1px solid var(--border-light) !important; border-radius:var(--radius) !important; overflow:hidden !important; }

    /* ── 分割线 ── */
    hr { border-color:var(--border-light) !important; margin:1.25rem 0 !important; }

    /* ── 表单提交按钮（form_submit_button）Q 弹补全 ── */
    [data-testid="stFormSubmitButton"] > button {
        will-change:transform !important;
        transition:box-shadow 0.22s var(--ease), border-color 0.22s var(--ease),
                   background 0.22s var(--ease) !important;
    }
    [data-testid="stFormSubmitButton"] > button:hover {
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    [data-testid="stFormSubmitButton"] > button:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }

    /* ── 滚动条 ── */
    ::-webkit-scrollbar { width:5px; height:5px; }
    ::-webkit-scrollbar-track { background:transparent; }
    ::-webkit-scrollbar-thumb { background:var(--border); border-radius:100px; }
    ::-webkit-scrollbar-thumb:hover { background:var(--text-muted); }
    </style>
    """

    # ── 认证页专属 CSS ────────────────────────────────────────────────────
    auth_css = """
    <style>
    @keyframes authFadeUp {
        from { opacity:0; transform:translateY(20px) scale(0.99); }
        to   { opacity:1; transform:translateY(0)    scale(1);    }
    }

    .stApp,
    [data-testid="stAppViewContainer"] {
        background:
            radial-gradient(ellipse 80% 50% at 50% -10%, rgba(219,234,254,0.55) 0%, transparent 60%),
            #F8FAFC !important;
        min-height:100vh !important;
    }

    [data-testid="stHeader"] { display:none !important; }

    [data-testid="stMain"] {
        display:flex !important;
        align-items:center !important;
        justify-content:center !important;
        min-height:100vh !important;
        padding:2rem 1rem !important;
        background:transparent !important;
    }

    [data-testid="stMainBlockContainer"] {
        max-width:420px !important;
        width:100% !important;
        background:#FFFFFF !important;
        border-radius:24px !important;
        border:1px solid #E2E8F0 !important;
        box-shadow:0 1px 3px rgba(15,23,42,0.06),0 8px 32px rgba(15,23,42,0.09) !important;
        padding:2.5rem 2.5rem 2rem !important;
        margin:0 !important;
        animation:authFadeUp 0.5s cubic-bezier(0.16,1,0.3,1) both !important;
    }

    .auth-logo {
        width:56px; height:56px;
        margin:0 auto 1rem auto;
        border-radius:14px;
        background:linear-gradient(135deg,#EFF6FF,#EEF2FF);
        display:flex; align-items:center; justify-content:center;
        font-size:1.625rem;
        box-shadow:0 2px 8px rgba(59,130,246,0.12);
    }
    .auth-brand {
        text-align:center;
        font-size:1.5rem !important;
        font-weight:700 !important;
        color:#0F172A !important;
        letter-spacing:-0.025em;
        margin-bottom:0.2rem;
    }
    .auth-sub {
        text-align:center;
        font-size:0.8125rem !important;
        color:#94A3B8 !important;
        margin-bottom:1.75rem;
    }
    .auth-section-title {
        font-size:1rem !important;
        font-weight:600 !important;
        color:#0F172A !important;
        margin-bottom:1rem !important;
    }
    [data-testid="stCheckbox"] label p { color:#475569 !important; font-size:0.875rem !important; }
    [data-baseweb="tab-list"] {
        justify-content:flex-start !important;
        border-bottom:1px solid #E2E8F0 !important;
        margin-bottom:1.25rem !important;
        gap:0.35rem !important;
    }
    [data-baseweb="tab"] {
        padding:0.5rem 0.75rem 0.625rem !important;
        font-weight:600 !important;
        font-size:0.9375rem !important;
        color:#0F172A !important;
        background:#FFFFFF !important;
        border-radius:10px !important;
        border:1px solid #E2E8F0 !important;
        transition:box-shadow 0.22s cubic-bezier(0.4,0,0.2,1), border-color 0.22s cubic-bezier(0.4,0,0.2,1) !important;
        will-change:transform !important;
    }
    /* hover：清晨天空冷色，无粉/无暖描边 */
    [data-baseweb="tab"]:hover {
        border-color:var(--input-sky-border-hover) !important;
        box-shadow:var(--input-halo-hover) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    [data-baseweb="tab"]:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    /* 选中态：天青深色底 + 浅色字，与全站输入语言一致 */
    [aria-selected="true"][data-baseweb="tab"] {
        background:linear-gradient(135deg, #0284c7 0%, #0e7490 100%) !important;
        color:#f8fafc !important;
        border-color:rgba(14, 165, 233, 0.55) !important;
        box-shadow:0 6px 20px -4px rgba(14, 165, 233, 0.35) !important;
    }
    [aria-selected="true"][data-baseweb="tab"]:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    /* 忘记密码页：返回登录按钮单行小字 */
    .st-key-back_to_login button {
        white-space:nowrap !important;
        font-size:0.82rem !important;
        line-height:1.1 !important;
        min-height:2.35rem !important;
        padding:0.35rem 0.6rem !important;
    }
    [data-testid="stForm"] > div { gap:0.75rem !important; }
    @media (max-width:480px) {
        [data-testid="stMainBlockContainer"] {
            border-radius:16px !important;
            padding:1.75rem 1.5rem 1.5rem !important;
        }
    }
    </style>
    """

    # ── 工作台专属 CSS ───────────────────────────────────────────────────
    dashboard_css = """
    <style>
    @keyframes contentFadeUp {
        from { opacity:0; transform:translateY(12px); }
        to   { opacity:1; transform:translateY(0); }
    }

    /* ── 页面背景 ── */
    .stApp,
    [data-testid="stAppViewContainer"] { background:#F8FAFC !important; }

    /* ── 顶部栏 ── */
    [data-testid="stHeader"] {
        display:block !important;
        background:rgba(248,250,252,0.92) !important;
        border-bottom:1px solid #E2E8F0 !important;
        backdrop-filter:blur(12px) !important;
    }

    /* ── 侧边栏 ── */
    [data-testid="stSidebar"] {
        background:rgba(255,255,255,0.78) !important;
        backdrop-filter:blur(10px) saturate(140%) !important;
        border-right:1px solid #E2E8F0 !important;
        padding-top:0.5rem !important;
    }
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span,
    [data-testid="stSidebar"] .stMarkdown { color:#0F172A !important; }
    [data-testid="stSidebar"] code {
        background:#0F172A !important;
        color:#F8FAFC !important;
        border-radius:6px !important;
        padding:0.15rem 0.45rem !important;
        font-weight:600 !important;
        font-size:0.8rem !important;
    }
    [data-testid="stSidebar"] [data-testid="stRadio"] label {
        border-radius:8px !important;
        padding:0.5rem 0.75rem !important;
        margin:0.1rem 0 !important;
        background:rgba(241,245,249,0.50) !important;
        color:#475569 !important;
        border:1px solid transparent !important;
        transition:box-shadow 0.22s cubic-bezier(0.4,0,0.2,1), border-color 0.22s cubic-bezier(0.4,0,0.2,1) !important;
        font-size:0.9rem !important;
        will-change:transform !important;
    }
    /* hover：不改颜色，仅加边框+阴影 + Q 弹 */
    [data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
        border-color:rgba(79,134,247,0.32) !important;
        box-shadow:0 4px 12px rgba(79,134,247,0.14) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    /* 选中项高对比：深蓝底 + 白字 */
    [data-testid="stSidebar"] [data-testid="stRadio"] input:checked + div,
    [data-testid="stSidebar"] [data-testid="stRadio"] input:checked + div p,
    [data-testid="stSidebar"] [data-testid="stRadio"] input:checked + div span {
        color:#0F172A !important;
    }

    /* ── 主内容区 ── */
    [data-testid="stMainBlockContainer"] {
        max-width:1320px !important;
        background:transparent !important;
        box-shadow:none !important;
        border:none !important;
        border-radius:0 !important;
        padding-top:1.5rem !important;
        padding-bottom:2rem !important;
        animation:contentFadeUp 0.4s cubic-bezier(0.16,1,0.3,1) both !important;
    }

    /* ── 内容卡片 ── */
    .dashboard-card {
        background:#FFFFFF;
        border:1px solid #F1F5F9;
        border-radius:18px;
        padding:1.75rem;
        box-shadow:0 1px 3px rgba(15,23,42,0.05),0 1px 2px rgba(15,23,42,0.03);
        transition:transform 0.25s cubic-bezier(0.4,0,0.2,1),box-shadow 0.25s cubic-bezier(0.4,0,0.2,1);
        margin-bottom:0.5rem;
    }
    .dashboard-card:hover {
        transform:translateY(-2px);
        box-shadow:0 8px 24px rgba(15,23,42,0.08),0 2px 6px rgba(15,23,42,0.04);
    }

    /* Student class join: keep forms inside keyed containers; style the shell only */
    .st-key-student_join_class_card,
    .st-key-student_joined_list_card {
        background: var(--card-bg, #ffffff);
        border: 1px solid var(--card-border, rgba(226, 232, 240, 0.95));
        border-radius: 24px;
        box-shadow: var(
            --card-shadow,
            0 4px 22px rgba(14, 165, 233, 0.09),
            0 1px 3px rgba(15, 23, 42, 0.05)
        );
        margin-bottom: 0.5rem;
        transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1), box-shadow 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .st-key-student_join_class_card:hover,
    .st-key-student_joined_list_card:hover {
        transform: translateY(-2px);
        box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08), 0 2px 8px rgba(14, 165, 233, 0.08);
    }
    .st-key-student_join_class_card > [data-testid="stVerticalBlock"] {
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: stretch;
        min-height: var(--card-min-height, 300px);
        padding: 0;
        width: 100%;
    }
    .st-key-student_joined_list_card > [data-testid="stVerticalBlock"] {
        display: flex;
        flex-direction: column;
        justify-content: flex-start;
        align-items: stretch;
        min-height: var(--card-min-height, 300px);
        padding: 0;
        width: 100%;
    }
    .student-card-header {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        gap: 0.35rem;
        padding: 1.4rem 1.5rem 0.5rem;
    }
    .student-card-header .pd-card-icon {
        font-size: 2rem;
        line-height: 1;
    }
    .student-card-header .pd-card-title {
        font-size: 1.12rem;
        font-weight: 700;
        color: #0c4a6e;
        margin: 0;
    }
    .student-card-header .pd-card-subtitle {
        max-width: 96%;
        margin: 0 auto;
        font-size: 0.86rem;
        line-height: 1.5;
        color: #64748b;
    }
    .student-join-panel,
    .student-list-panel {
        border-top: 1px solid rgba(125, 211, 252, 0.34);
        padding: 0 1.5rem 1.5rem;
    }
    .st-key-student_join_class_card [data-baseweb="input"] {
        border-radius: 12px;
        border-color: rgba(186, 230, 253, 0.95) !important;
    }
    .student-class-row {
        display: flex;
        flex-direction: row;
        align-items: center;
        justify-content: space-between;
        gap: 0.75rem;
        width: 100%;
        min-height: 3.5rem;
        margin-bottom: 0.7rem;
        padding: 0.75rem 0.9rem;
        box-sizing: border-box;
        border-radius: 14px;
        border: 1px solid rgba(186, 230, 253, 0.65);
        background: #ffffff;
        color: #0f172a;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 12px rgba(14, 116, 144, 0.06);
    }
    .student-class-row:last-child {
        margin-bottom: 0;
    }
    .student-class-row__lead {
        display: flex;
        flex-direction: row;
        align-items: center;
        gap: 0.75rem;
        flex: 1 1 0;
        min-width: 0;
    }
    .student-class-avatar {
        width: 2.5rem;
        height: 2.5rem;
        border-radius: 50%;
        background: #e0f2fe;
        color: #075985;
        font-weight: 700;
        font-size: 1.05rem;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
    }
    .student-class-body {
        flex: 1 1 0;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 0.2rem;
    }
    .student-class-name-line {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.35rem;
        line-height: 1.3;
    }
    .student-class-name {
        font-weight: 700;
        font-size: 0.95rem;
        color: #0f172a;
        word-break: break-word;
    }
    .student-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.2rem;
        font-size: 0.7rem;
        font-weight: 600;
        padding: 0.1rem 0.45rem;
        border-radius: 999px;
        line-height: 1.2;
    }
    .student-pill--active {
        background: #ecfdf5;
        color: #047857;
        border: 1px solid rgba(16, 185, 129, 0.22);
    }
    .student-pill-dot {
        width: 0.4rem;
        height: 0.4rem;
        border-radius: 50%;
        background: #10b981;
    }
    .student-class-sub {
        font-size: 0.78rem;
        color: #94a3b8;
        word-break: break-word;
    }
    .student-class-code-chip {
        flex: 0 0 auto;
        align-self: center;
        max-width: 7.5rem;
        padding: 0.35rem 0.5rem;
        border-radius: 8px;
        background: #1e293b;
        color: #10b981;
        font-size: 0.78rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        line-height: 1.2;
        text-align: center;
        word-break: break-all;
    }
    .student-list-panel [data-testid="stAlert"] {
        border-radius: 0.75rem;
        margin-top: 0.35rem;
    }

    /* ── 统计卡片 ── */
    .stat-card {
        display:flex;
        flex-direction:column;
        align-items:flex-start;
        justify-content:center;
        text-align:left;
        background:#FFFFFF;
        border:1px solid #F1F5F9;
        border-radius:1.125rem;
        padding:1.5rem 1.75rem;
        min-height:5.5rem;
        box-shadow:0 2px 8px rgba(15,23,42,0.06),0 1px 2px rgba(15,23,42,0.04);
        transition:transform 0.25s cubic-bezier(0.4,0,0.2,1),box-shadow 0.25s cubic-bezier(0.4,0,0.2,1);
    }
    .stat-card:hover {
        transform:translateY(-2px);
        box-shadow:0 8px 24px rgba(15,23,42,0.1),0 2px 6px rgba(15,23,42,0.05);
    }
    .stat-label {
        font-size:0.8125rem;
        font-weight:600;
        color:#64748B;
        text-transform:none;
        letter-spacing:0.02em;
        margin-bottom:0.55rem;
    }
    .stat-value {
        font-size:2rem;
        font-weight:700;
        color:#0F172A;
        letter-spacing:-0.03em;
        line-height:1;
    }
    .st-key-assignment_kpi_row,
    .st-key-class_kpi_row {
        width:100% !important;
        margin:0 0 1.1rem 0;
        border-radius:1.5rem;
        border:1px solid rgba(186,230,253,0.95);
        background:linear-gradient(180deg,#f0f9ff 0%,#e0f2fe 55%,#dbeafe 100%);
        box-shadow:
            0 10px 24px rgba(14,165,233,0.1),
            0 2px 8px rgba(15,23,42,0.04),
            inset 0 1px 0 rgba(255,255,255,0.88);
        display:flex !important;
        flex-direction:column !important;
        align-items:stretch !important;
        justify-content:center !important;
        /* Equal vertical room; default Streamlit row margins are offset below */
        min-height:7.25rem;
        padding:1.35rem 1.5rem 1.35rem;
        box-sizing:border-box;
    }
    .st-key-assignment_kpi_row [data-testid="stVerticalBlock"],
    .st-key-class_kpi_row [data-testid="stVerticalBlock"] {
        gap:0 !important;
        display:flex !important;
        flex-direction:column !important;
        flex:1 1 auto;
        width:100% !important;
        max-width:100%;
        align-items:center !important;
        justify-content:center !important;
        margin:0 !important;
        padding:0 !important;
    }
    .st-key-assignment_kpi_row [data-testid="stHorizontalBlock"],
    .st-key-class_kpi_row [data-testid="stHorizontalBlock"] {
        width:100% !important;
        max-width:60rem;
        margin:0 !important;
        margin-left:auto !important;
        margin-right:auto !important;
        align-items:stretch !important;
        align-self:center !important;
        justify-content:center !important;
        gap:0.9rem !important;
        /* Nudge whole row up to match true optical center in this layout */
        transform:translateY(-0.3rem);
    }
    .st-key-assignment_kpi_row [data-testid="column"],
    .st-key-class_kpi_row [data-testid="column"] {
        display:flex !important;
        flex-direction:column !important;
        align-items:stretch !important;
        justify-content:center !important;
    }
    .st-key-assignment_kpi_row [data-testid="column"] [data-testid="stElementContainer"],
    .st-key-class_kpi_row [data-testid="column"] [data-testid="stElementContainer"] {
        margin:0 !important;
    }
    .st-key-assignment_kpi_row [data-testid="column"] [data-testid="stMarkdown"],
    .st-key-class_kpi_row [data-testid="column"] [data-testid="stMarkdown"] {
        margin:0 0 0.15rem 0 !important;
    }
    .st-key-assignment_kpi_row [data-testid="column"] p,
    .st-key-class_kpi_row [data-testid="column"] p {
        margin:0 !important;
    }
    /* ── 页面标题区 ── */
    .page-header { margin-bottom:1.75rem; }
    .page-title {
        font-size:1.625rem;
        font-weight:700;
        color:#0F172A;
        letter-spacing:-0.025em;
        line-height:1.2;
    }
    .page-desc {
        font-size:0.875rem;
        color:#94A3B8;
        margin-top:0.3rem;
    }
    .panel-card {
        background:#FFFFFF;
        border:1px solid #F1F5F9;
        border-radius:18px;
        padding:1.25rem 1.4rem;
        box-shadow:0 1px 3px rgba(15,23,42,0.04);
        transition:all 0.25s cubic-bezier(0.4,0,0.2,1);
        margin-bottom:0.9rem;
    }
    .panel-card:hover {
        box-shadow:0 12px 28px rgba(15,23,42,0.08);
        transform:translateY(-2px);
    }
    .panel-title {
        font-size:1rem;
        font-weight:700;
        color:#0F172A;
        margin-bottom:0.2rem;
        letter-spacing:-0.015em;
    }
    .panel-subtitle {
        font-size:0.82rem;
        color:#94A3B8;
        margin-bottom:0.9rem;
    }
    .soft-list-item {
        font-size:0.86rem;
        color:#475569;
        padding:0.45rem 0;
        border-bottom:1px dashed #E2E8F0;
    }
    .soft-list-item:last-child { border-bottom:none; }
    .empty-state {
        border:1px dashed #CBD5E1;
        background:rgba(248,250,252,0.72);
        border-radius:16px;
        padding:1.2rem;
        margin-top:0.4rem;
        text-align:left;
    }
    .empty-state-icon {
        width:38px;
        height:38px;
        border-radius:10px;
        background:#EFF6FF;
        color:#1D4ED8;
        display:flex;
        align-items:center;
        justify-content:center;
        font-size:1.1rem;
        margin-bottom:0.65rem;
    }
    .empty-state-title {
        font-size:0.95rem;
        font-weight:700;
        color:#0F172A;
        margin-bottom:0.2rem;
    }
    .empty-state-desc {
        font-size:0.84rem;
        color:#64748B;
        line-height:1.55;
        margin-bottom:0.75rem;
    }
    .empty-state.empty-state--sky {
        border:1px dashed #D1E2F3;
        background:rgba(255,255,255,0.55);
    }
    .pd-recent-content {
        width:100%;
        display:grid;
        gap:0.75rem;
    }
    /* ── 微信风格私聊区 ── */
    .wx-chat-wrap {
        background:#F5F5F5;
        border:1px solid #E5E7EB;
        border-radius:16px;
        padding:0.85rem;
    }
    .wx-chat-scroll {
        height:calc(100vh - 360px);
        min-height:320px;
        max-height:620px;
        overflow-y:auto;
        padding:0.5rem 0.35rem 0.35rem;
        background:#F5F5F5;
        border-radius:12px;
    }
    .wx-msg {
        display:flex;
        align-items:flex-start;
        gap:0.55rem;
        margin-bottom:0.65rem;
    }
    .wx-msg-self {
        flex-direction:row-reverse;
    }
    .wx-msg-body {
        display:flex;
        flex-direction:column;
        max-width:70%;
    }
    .wx-msg-self .wx-msg-body {
        align-items:flex-end;
    }
    .wx-msg-peer .wx-msg-body {
        align-items:flex-start;
    }
    .wx-avatar {
        width:32px;
        height:32px;
        border-radius:10px;
        display:flex;
        align-items:center;
        justify-content:center;
        font-size:0.8rem;
        font-weight:700;
        color:#0F172A;
        border:1px solid #D1D5DB;
        flex-shrink:0;
    }
    .wx-avatar-self {
        background:#DCFCE7;
    }
    .wx-avatar-peer {
        background:#FFFFFF;
    }
    .wx-bubble {
        border-radius:10px;
        padding:0.6rem 0.78rem;
        font-size:0.92rem;
        line-height:1.45;
        word-break:break-word;
        white-space:pre-wrap;
        box-shadow:0 1px 2px rgba(15,23,42,0.08);
    }
    .wx-msg-self .wx-bubble {
        background:#95EC69;
        color:#111827;
    }
    .wx-msg-peer .wx-bubble {
        background:#FFFFFF;
        color:#0F172A;
        border:1px solid #E5E7EB;
    }
    .wx-time {
        margin-top:0.24rem;
        font-size:0.72rem;
        color:#94A3B8;
        line-height:1;
    }
    .wx-input-wrap {
        margin-top:0.7rem;
        padding-top:0.65rem;
        border-top:1px solid #E2E8F0;
        background:#F5F5F5;
    }
    .wx-input-wrap [data-baseweb="textarea"] textarea {
        background:var(--input-surface) !important;
        border:none !important;
        box-shadow:none !important;
    }
    .wx-input-wrap [data-baseweb="textarea"] {
        border-color:var(--input-sky-border) !important;
        box-shadow:var(--input-halo-rest) !important;
    }
    .wx-input-wrap [data-baseweb="textarea"]:focus-within {
        border-color:var(--input-sky-border-focus) !important;
        box-shadow:var(--input-halo-focus) !important;
    }

    /* ══ 消息中心：macOS 圆钝天蓝社交管理布局 ══ */
    [data-testid="stHorizontalBlock"]:has(.mc-list) {
        gap:1.5rem !important;
        align-items:stretch !important;
    }
    [data-testid="stColumn"]:has(.mc-list) {
        border-radius:1.72rem !important; /* rounded-3xl */
        min-height:560px !important;
        overflow:hidden !important;
        border:1px solid rgba(186, 230, 253, 0.86) !important;
        background:
            linear-gradient(145deg, rgba(224, 247, 250, 0.92) 0%, rgba(161, 223, 255, 0.84) 100%) !important;
        backdrop-filter: blur(20px) saturate(1.2) !important;
        -webkit-backdrop-filter: blur(20px) saturate(1.2) !important;
        box-shadow:
            inset 0 1px 0 rgba(255, 255, 255, 0.9),
            0 12px 28px -18px rgba(14, 116, 144, 0.38),
            0 24px 42px -30px rgba(56, 189, 248, 0.3) !important;
        position:relative;
    }
    [data-testid="stColumn"]:has(.mc-list)::before {
        content:"";
        position:absolute;
        inset:0;
        background:linear-gradient(180deg, rgba(255,255,255,0.34) 0%, rgba(191, 219, 254, 0.08) 100%);
        pointer-events:none;
    }
    [data-testid="stColumn"]:has(.mc-list) {
        padding:0.7rem 0.66rem 1rem !important;
    }
    [data-testid="stColumn"]:has(.mc-chat) {
        background:#F8FAFC !important;
        border:1px solid rgba(226, 232, 240, 0.86) !important;
        border-radius:1.45rem !important;
        padding:0.85rem 1.3rem 1.05rem !important;
        min-height:560px !important;
        box-shadow:0 20px 42px -34px rgba(15, 23, 42, 0.28) !important;
    }
    .mc-list { padding:1.5rem 1.25rem 1.45rem; position:relative; z-index:1; }
    .mc-list-title {
        margin:0;
        color:#0C4A6E;
        font-size:1.52rem;
        font-weight:800;
        letter-spacing:-0.02em;
    }
    /* —— 导航壳：整体按压 scale(0.99)，1% 真实手感回馈 —— */
    .mc-nav-shell {
        position:relative;
        margin-top:1.1rem;
        margin-bottom:1.1rem;
        padding-bottom:0.85rem;
        transform-origin:center center;
        transform:translateZ(0) scale(1);
        transition:transform 0.42s cubic-bezier(0.23, 1, 0.32, 1);
        will-change:transform;
    }
    .mc-nav-shell:active {
        transform:translateZ(0) scale(0.99);
        transition:transform 0.14s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .mc-nav-shell [data-testid="stHorizontalBlock"] {
        gap:1.25rem !important;
    }
    @media (prefers-reduced-motion: reduce) {
        .mc-nav-shell,
        .mc-nav-shell:active {
            transition:none !important;
        }
    }
    /* —— 「好友列表 / 添加好友」选项卡：基础态
       色彩 / 字重 200ms 平滑插值，曲线 ease-out，避免线性突变 —— */
    [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button {
        min-height:unset !important;
        padding:0 !important;
        border:none !important;
        border-radius:0 !important;
        background:transparent !important;
        box-shadow:none !important;
        color:#94A3B8 !important;
        font-size:0.98rem !important;
        font-weight:600 !important;
        letter-spacing:-0.005em !important;
        text-align:left !important;
        justify-content:flex-start !important;
        transform:translateY(0) scale(1);
        transform-origin:center center;
        transition:color 0.2s cubic-bezier(0.4, 0, 0.2, 1),
                   font-weight 0.2s cubic-bezier(0.4, 0, 0.2, 1),
                   letter-spacing 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
        will-change:color, transform !important;
        backface-visibility:hidden;
        -webkit-tap-highlight-color:transparent;
    }
    /* —— hover / focus：Apple 质感 Q 弹抖动（沿 Y 轴 2-3px 极小往复） —— */
    [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button:hover,
    [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button:focus,
    [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button:focus-visible {
        color:#075985 !important;
        background:transparent !important;
        border:none !important;
        box-shadow:none !important;
        outline:none !important;
        animation:mcNavTabBounce 0.5s cubic-bezier(0.16, 1, 0.3, 1) both !important;
    }
    /* —— press：scale(0.98) → 弹回（不破坏 translateY 基准） —— */
    [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button:active {
        color:#075985 !important;
        background:transparent !important;
        border:none !important;
        box-shadow:none !important;
        animation:mcNavTabPress 0.34s cubic-bezier(0.16, 1, 0.3, 1) both !important;
    }
    /* —— 减少动效偏好：尊重无障碍设置 —— */
    @media (prefers-reduced-motion: reduce) {
        [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button:hover,
        [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button:focus,
        [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button:focus-visible,
        [data-testid="stColumn"]:has(.mc-list) .mc-nav-shell .stButton > button:active {
            animation:none !important;
        }
    }
    .mc-divider {
        width:100%;
        height:2px;
        border-radius:9999px;
        background:rgba(186, 230, 253, 0.72);
        margin:0.8rem 0 0.55rem;
    }
    /* —— 内容区：Fade & Slide
       Out-Quint 曲线 (0.23, 1, 0.32, 1) — 极致优雅的减速
       新内容从 -24px / +24px 之外淡入到位，与指示器滑动同步 —— */
    .mc-tab-content {
        padding:0.1rem 0 0.2rem;
        will-change:transform, opacity;
        backface-visibility:hidden;
        transform:translateZ(0);
    }
    .mc-pane-enter-left {
        animation:mcPaneInLeft 0.56s cubic-bezier(0.23, 1, 0.32, 1) both;
    }
    .mc-pane-enter-right {
        animation:mcPaneInRight 0.56s cubic-bezier(0.23, 1, 0.32, 1) both;
    }
    @keyframes mcPaneInLeft {
        0%   { opacity:0; transform:translate3d(-24px, 0, 0); filter:blur(2px); }
        60%  { opacity:1; filter:blur(0); }
        100% { opacity:1; transform:translate3d(0, 0, 0); filter:blur(0); }
    }
    @keyframes mcPaneInRight {
        0%   { opacity:0; transform:translate3d(24px, 0, 0); filter:blur(2px); }
        60%  { opacity:1; filter:blur(0); }
        100% { opacity:1; transform:translate3d(0, 0, 0); filter:blur(0); }
    }
    @media (prefers-reduced-motion: reduce) {
        .mc-pane-enter-left,
        .mc-pane-enter-right {
            animation:none !important;
        }
    }
    .mc-list-empty {
        padding:1.4rem 0.1rem 0.2rem;
        color:#64748B;
        font-size:0.92rem;
        line-height:1.7;
    }
    /* ── 好友选择卡片（前端设计大神 / Restrained + Q 弹微交互大师 / 上抬反馈）
       结构：先渲染透明 st.button（承接 hover + click），紧随其后的 div.mc-friend-pick
             以负 margin-top 上覆按钮（pointer-events:none，纯展示）。
       hover：按钮 :hover 通过 :has() + 兄弟选择器把状态传给卡片，卡片 -6px 上抬到接近
              上方分隔线，保留 ~5px 呼吸缝。Q 弹关键帧叠加细微抖动余韵。 */
    /* 抵消父级 stVerticalBlock 默认 gap (~1rem)：每张「按钮+卡片」对都
       向上靠拢，同时让首张卡片紧贴分隔线，对任意数量好友一致生效 */
    [class*="st-key-mc_pick_friend_"] {
        margin-top:-0.5rem !important;
        margin-bottom:0 !important;
    }
    [class*="st-key-mc_pick_friend_"] button {
        width:100% !important;
        height:calc(3.8rem + 10px) !important;
        min-height:calc(3.8rem + 10px) !important;
        padding:10px 0 0 0 !important;
        margin:0 !important;
        background:transparent !important;
        border:0 !important;
        box-shadow:none !important;
        color:transparent !important;
        font-size:0 !important;
        line-height:0 !important;
        cursor:pointer !important;
        border-radius:14px !important;
        position:relative !important;
        z-index:1 !important;
        -webkit-tap-highlight-color:transparent !important;
        animation:none !important;
        transition:none !important;
    }
    [class*="st-key-mc_pick_friend_"] button:hover,
    [class*="st-key-mc_pick_friend_"] button:focus,
    [class*="st-key-mc_pick_friend_"] button:focus-visible,
    [class*="st-key-mc_pick_friend_"] button:active {
        background:transparent !important;
        outline:none !important;
        animation:none !important;
        transform:none !important;
    }

    .mc-friend-pick {
        --pick-h:3.8rem;
        box-sizing:border-box;
        margin:calc(-1 * var(--pick-h) - 10px) 0 0 0;
        width:100%;
        min-height:var(--pick-h);
        padding:0.7rem 0.9rem;
        display:flex;
        align-items:center;
        gap:0.8rem;
        background:#FFFFFF;
        border:1px solid rgba(226,232,240,0.85);
        border-radius:14px;
        box-shadow:0 1px 2px rgba(15,23,42,0.04);
        pointer-events:none;
        position:relative;
        z-index:2;
        transform:translateY(0) scale(1);
        transform-origin:center center;
        will-change:transform, box-shadow;
        backface-visibility:hidden;
        transition:
            transform 0.42s cubic-bezier(0.16,1,0.3,1),
            box-shadow 0.42s cubic-bezier(0.16,1,0.3,1),
            border-color 0.22s cubic-bezier(0.16,1,0.3,1);
    }
    .mc-friend-pick--active {
        border-color:rgba(37,99,235,0.45);
        box-shadow:0 0 0 1px rgba(37,99,235,0.16),0 4px 14px rgba(37,99,235,0.10);
    }

    /* hover：上抬接近上方横线（剩 5px 呼吸），叠加 Q 弹抖动余韵 */
    [class*="st-key-mc_pick_friend_"]:has(button:hover) + [data-testid="stElementContainer"] .mc-friend-pick {
        border-color:rgba(191,219,254,0.95);
        box-shadow:0 12px 22px -10px rgba(15,23,42,0.14);
        animation:mcPickQBounce 0.5s cubic-bezier(0.16,1,0.3,1) both;
    }

    /* focus-visible：键盘聚焦时给卡片一圈品牌色环 */
    [class*="st-key-mc_pick_friend_"]:has(button:focus-visible) + [data-testid="stElementContainer"] .mc-friend-pick {
        border-color:rgba(37,99,235,0.55);
        box-shadow:0 0 0 3px rgba(37,99,235,0.18),0 7px 18px -10px rgba(15,23,42,0.14);
        transform:translateY(-5px) scale(1);
    }

    /* active：以 -5px 为基线做按下回弹（Q 弹 press） */
    [class*="st-key-mc_pick_friend_"]:has(button:active) + [data-testid="stElementContainer"] .mc-friend-pick {
        animation:mcPickQPress 0.34s cubic-bezier(0.16,1,0.3,1) both;
    }

    /* Q 弹关键帧：从 0 → 上抬到 -5px 稳态，途中带细微弹簧余韵 */
    @keyframes mcPickQBounce {
        0%   { transform:translateY(0)      scale(1);     }
        20%  { transform:translateY(-7.2px) scale(0.992); }
        44%  { transform:translateY(-3.6px) scale(1.006); }
        66%  { transform:translateY(-5.4px) scale(1.002); }
        100% { transform:translateY(-5px)   scale(1);     }
    }
    @keyframes mcPickQPress {
        0%   { transform:translateY(-5px)   scale(1);     }
        36%  { transform:translateY(-4.6px) scale(0.98);  }
        66%  { transform:translateY(-5.3px) scale(1.012); }
        100% { transform:translateY(-5px)   scale(1);     }
    }
    @media (prefers-reduced-motion: reduce) {
        .mc-friend-pick { transition:none !important; }
        [class*="st-key-mc_pick_friend_"]:has(button:hover) + [data-testid="stElementContainer"] .mc-friend-pick,
        [class*="st-key-mc_pick_friend_"]:has(button:active) + [data-testid="stElementContainer"] .mc-friend-pick,
        [class*="st-key-mc_pick_friend_"]:has(button:focus-visible) + [data-testid="stElementContainer"] .mc-friend-pick {
            animation:none !important;
            transform:none !important;
        }
    }
    .mc-friend-list__avatar {
        width:2rem;
        height:2rem;
        min-width:2rem;
        min-height:2rem;
        border-radius:9999px;
        display:flex;
        align-items:center;
        justify-content:center;
        font-size:0.78rem;
        font-weight:700;
        color:#075985;
        background:#e0f2fe;
        border:1px solid rgba(125,211,252,0.9);
        box-sizing:border-box;
        padding:0;
        line-height:1;
        flex-shrink:0;
        overflow:hidden;
        text-align:center;
    }
    .mc-friend-list__avatar-char {
        display:block;
        margin:0;
        padding:0;
        line-height:1;
        letter-spacing:0;
        font:inherit;
        color:inherit;
        text-align:center;
        transform:none;
    }
    .mc-friend-list__body {
        min-width:0;
        flex:1;
        display:flex;
        flex-direction:row;
        flex-wrap:wrap;
        align-items:center;
        gap:0.6rem;
    }
    .mc-friend-list__pills {
        display:inline-flex;
        flex-wrap:wrap;
        align-items:center;
        gap:0.4rem;
    }
    .mc-friend-list__name {
        font-size:0.88rem;
        font-weight:700;
        color:#1e293b;
        letter-spacing:-0.02em;
        line-height:1.25;
    }
    .mc-friend-pill {
        display:inline-flex; align-items:center; justify-content:center; gap:0.24rem;
        font-size:0.6rem; font-weight:600; line-height:1.15;
        letter-spacing:0.01em;
        padding:0.18rem 0.78rem; border-radius:9999px; white-space:nowrap;
    }
    .mc-friend-pill--role {
        color:#0c4a6e;
        background:#e0f2fe;
        border:1px solid rgba(125,211,252,0.8);
    }
    .mc-friend-pill--status {
        color:#14532d;
        background:#ecfdf5;
        border:1px solid rgba(134,239,172,0.6);
    }
    .mc-friend-pill--status-muted {
        color:#475569;
        background:#f1f5f9;
        border:1px solid #e2e8f0;
    }
    .mc-friend-pill__dot {
        width:6px; height:6px; border-radius:9999px; background:#22c55e; flex-shrink:0;
    }
    .mc-friend-pill__dot--off { background:#94a3b8; }
    /* —— 副操作按钮统一尺寸（不含主操作「添加好友」）—— */
    .st-key-mc_add_friend_action button,
    [class*="st-key-mc_accept_"] button,
    [class*="st-key-mc_reject_"] button {
        min-height:2.05rem !important;
        border-radius:0.75rem !important;
        box-shadow:none !important;
    }
    /* —— 好友申请（接受 / 拒绝）等辅助按钮保持安静、无动画 —— */
    [class*="st-key-mc_accept_"] button,
    [class*="st-key-mc_reject_"] button {
        animation:none !important;
    }
    [class*="st-key-mc_accept_"] button:hover,
    [class*="st-key-mc_reject_"] button:hover,
    [class*="st-key-mc_accept_"] button:active,
    [class*="st-key-mc_reject_"] button:active {
        animation:none !important;
        transform:none !important;
        box-shadow:none !important;
    }
    /* —— 「添加好友」主操作按钮：Q 弹微交互（仅 transform，60FPS） —— */
    .st-key-mc_add_friend_action button {
        transform:translateY(0) scale(1);
        transform-origin:center center;
        transition:color 0.22s cubic-bezier(0.16, 1, 0.3, 1),
                   background 0.22s cubic-bezier(0.16, 1, 0.3, 1),
                   border-color 0.22s cubic-bezier(0.16, 1, 0.3, 1),
                   box-shadow 0.22s cubic-bezier(0.16, 1, 0.3, 1) !important;
        will-change:transform !important;
        backface-visibility:hidden !important;
        -webkit-tap-highlight-color:transparent !important;
    }
    .st-key-mc_add_friend_action button:hover,
    .st-key-mc_add_friend_action button:focus,
    .st-key-mc_add_friend_action button:focus-visible {
        border-color:#BFDBFE !important;
        box-shadow:0 5px 16px rgba(79, 134, 247, 0.15) !important;
        outline:none !important;
        animation:mcNavTabBounce 0.5s cubic-bezier(0.16, 1, 0.3, 1) both !important;
    }
    .st-key-mc_add_friend_action button:active {
        animation:mcNavTabPress 0.34s cubic-bezier(0.16, 1, 0.3, 1) both !important;
    }
    @media (prefers-reduced-motion: reduce) {
        .st-key-mc_add_friend_action button:hover,
        .st-key-mc_add_friend_action button:focus,
        .st-key-mc_add_friend_action button:focus-visible,
        .st-key-mc_add_friend_action button:active {
            animation:none !important;
        }
    }
    .mc-request-row {
        border:1px solid rgba(186, 230, 253, 0.7);
        border-radius:0.9rem;
        padding:0.65rem 0.7rem;
        margin-bottom:0.6rem;
        background:rgba(255,255,255,0.52);
    }
    .mc-request-user {
        color:#334155;
        font-size:0.86rem;
        margin-bottom:0.45rem;
    }
    .mc-search-block {
        margin-top:1.35rem;
        padding-top:1.05rem;
        border-top:1px solid rgba(186, 230, 253, 0.72);
    }
    [data-testid="stColumn"]:has(.mc-list) [data-baseweb="input"] input,
    [data-testid="stColumn"]:has(.mc-list) [data-baseweb="select"] > div {
        background:rgba(255,255,255,0.82) !important;
        color:#334155 !important;
    }
    [data-testid="stColumn"]:has(.mc-list) [data-baseweb="input"] input {
        min-height:2.3rem !important;
    }
    [data-testid="stColumn"]:has(.mc-list) [data-baseweb="input"] input::placeholder { color:#4B6F8E !important; }
    [data-testid="stColumn"]:has(.mc-list) [data-baseweb="input"],
    [data-testid="stColumn"]:has(.mc-list) [data-baseweb="select"] {
        border-color:rgba(125, 211, 252, 0.5) !important;
        background:rgba(255,255,255,0.7) !important;
        border-radius:0.95rem !important;
        box-shadow:var(--input-halo-rest) !important;
    }
    [data-testid="stColumn"]:has(.mc-list) [data-baseweb="input"]:focus-within,
    [data-testid="stColumn"]:has(.mc-list) [data-baseweb="select"]:focus-within {
        border-color:var(--input-sky-border-focus) !important;
        box-shadow:var(--input-halo-focus) !important;
    }
    [data-testid="stColumn"]:has(.mc-list) label p,
    [data-testid="stColumn"]:has(.mc-list) label span { color:#334155 !important; }
    .mc-chat-header {
        font-size:1rem; font-weight:700; color:#334155;
        padding:0.4rem 0 0.65rem;
        border-bottom:1px solid #E2E8F0; margin-bottom:0.75rem;
    }
    [data-testid="stColumn"]:has(.mc-chat) .wx-chat-scroll {
        height:calc(100vh - 430px) !important;
        min-height:220px !important; max-height:480px !important;
    }
    [data-testid="stColumn"]:has(.mc-chat) [data-testid="stFormSubmitButton"] > button {
        background:#FF4D4F !important; border-color:#FF4D4F !important;
        color:#111827 !important; font-weight:600 !important; margin-top:0.3rem !important;
        will-change:transform !important;
    }
    [data-testid="stColumn"]:has(.mc-chat) [data-testid="stFormSubmitButton"] > button:hover {
        background:#FF4D4F !important; border-color:#FF4D4F !important; color:#111827 !important;
        box-shadow:0 5px 18px rgba(244,63,94,0.24) !important;
        animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    [data-testid="stColumn"]:has(.mc-chat) [data-testid="stFormSubmitButton"] > button:active {
        animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both !important;
    }
    [data-testid="stColumn"]:has(.mc-chat) [data-baseweb="textarea"] textarea {
        background:var(--input-surface) !important; color:#334155 !important;
        border:none !important; box-shadow:none !important;
    }
    [data-testid="stColumn"]:has(.mc-chat) [data-baseweb="textarea"] {
        border-color:var(--input-sky-border) !important;
        box-shadow:var(--input-halo-rest) !important;
    }
    [data-testid="stColumn"]:has(.mc-chat) [data-baseweb="textarea"]:focus-within {
        border-color:var(--input-sky-border-focus) !important;
        box-shadow:var(--input-halo-focus) !important;
    }

    .class-static-shell {
        width: 100%;
        margin: 0;
        padding: 0;
        border: none;
        border-radius: 0;
        background: transparent;
        box-shadow: none;
    }
    .st-key-class_create_card,
    .st-key-class_list_card,
    .st-key-assignment_publish_card,
    .st-key-assignment_recent_card {
        width: 100% !important;
        margin: 0 0 1rem 0 !important;
        border-radius: 1.5rem !important; /* rounded-3xl */
        border: 1px solid rgba(186, 230, 253, 0.96) !important;
        background: linear-gradient(180deg, #f0f9ff 0%, #e0f2fe 100%) !important;
        box-shadow:
            0 12px 24px rgba(14, 165, 233, 0.08),
            0 5px 12px rgba(15, 23, 42, 0.05),
            inset 0 1px 0 rgba(255, 255, 255, 0.82) !important;
        overflow: hidden !important;
        transform-origin: center center !important;
        transition:
            box-shadow 0.32s cubic-bezier(0.16, 1, 0.3, 1),
            transform 0.32s cubic-bezier(0.16, 1, 0.3, 1) !important;
    }
    .st-key-class_create_card:hover,
    .st-key-class_list_card:hover,
    .st-key-assignment_publish_card:hover,
    .st-key-assignment_recent_card:hover {
        transform: translateY(-2px) scale(1.008) !important;
        box-shadow:
            0 18px 28px rgba(14, 165, 233, 0.11),
            0 8px 16px rgba(15, 23, 42, 0.08),
            inset 0 1px 0 rgba(255, 255, 255, 0.88) !important;
        animation: elasticJiggle 0.46s cubic-bezier(0.16, 1, 0.3, 1) both !important;
    }
    .st-key-class_create_card:active,
    .st-key-class_list_card:active,
    .st-key-assignment_publish_card:active,
    .st-key-assignment_recent_card:active {
        animation: elasticPress 0.32s cubic-bezier(0.16, 1, 0.3, 1) both !important;
    }

    .pd-card-header {
        min-height: min(54vw, 300px);
        padding: 2rem 1.5rem 1.35rem;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        gap: 0.6rem;
    }
    .pd-card-icon {
        font-size: 2.9rem;
        line-height: 1;
        filter: drop-shadow(0 6px 12px rgba(14, 165, 233, 0.16));
    }
    .pd-card-title {
        font-size: 1.22rem;
        font-weight: 700;
        color: #0c4a6e;
        letter-spacing: -0.02em;
        line-height: 1.28;
    }
    .pd-card-subtitle {
        max-width: 80%;
        margin: 0 auto;
        font-size: 0.95rem;
        color: #475569;
        line-height: 1.68;
    }
    .pd-panel-inner {
        padding: 0 1.5rem 1.5rem;
        border-top: 1px solid rgba(125, 211, 252, 0.34);
    }
    .pd-functional-zone {
        width: 100%;
        display: grid;
        gap: 0.9rem;
    }
    @keyframes qElasticBounce {
        0%   { transform: translateY(0) scale(1); }
        20%  { transform: translateY(-2.6px) scale(0.992); }
        44%  { transform: translateY(1.2px) scale(1.006); }
        66%  { transform: translateY(-0.5px) scale(1.002); }
        100% { transform: translateY(0) scale(1); }
    }
    @keyframes qElasticPress {
        0%   { transform: scale(1) translateY(0); }
        36%  { transform: scale(0.98) translateY(0.4px); }
        66%  { transform: scale(1.012) translateY(-0.3px); }
        100% { transform: scale(1) translateY(0); }
    }
    /* 作业发布卡片 — 整卡呼吸感 */
    .st-key-assignment_publish_card .pd-panel-inner {
        padding: 0.75rem 2.4rem 2.1rem !important;
    }
    .st-key-assignment_publish_card .pd-functional-zone {
        gap: 1.55rem !important;
    }
    /* 作业发布表单：统一增加左右留白，避免输入框/选项卡片贴边 */
    .st-key-assignment_publish_card .st-key-teacher_publish_title,
    .st-key-assignment_publish_card .st-key-teacher_publish_content_text,
    .st-key-assignment_publish_card .st-key-teacher_assignment_image_uploader,
    .st-key-assignment_publish_card .st-key-teacher_assignment_file_uploader,
    .st-key-assignment_publish_card .st-key-teacher_publish_standard_answer,
    .st-key-assignment_publish_card .st-key-teacher_publish_deadline,
    .st-key-assignment_publish_card .st-key-teacher_publish_target_search,
    .st-key-assignment_publish_card .st-key-teacher_publish_target_search_result,
    .st-key-assignment_publish_card .st-key-teacher_publish_target_add_btn,
    .st-key-assignment_publish_card .st-key-teacher_publish_target_remove_pick,
    .st-key-assignment_publish_card .st-key-teacher_publish_target_remove_btn,
    .st-key-assignment_publish_card .st-key-teacher_publish_assignment_btn {
        padding-left: 0.45rem !important;
        padding-right: 0.45rem !important;
    }
    .st-key-assignment_publish_card label[data-testid="stWidgetLabel"] p,
    .st-key-assignment_publish_card label p {
        font-size: 0.92rem !important;
        font-weight: 600 !important;
        color: #1e293b !important;
        letter-spacing: 0.01em;
    }
    .st-key-assignment_publish_card input::placeholder,
    .st-key-assignment_publish_card textarea::placeholder {
        color: #94a3b8 !important;
        opacity: 1 !important;
        font-weight: 400 !important;
    }
    /* 附件区小节标签 */
    .assignment-attach-label {
        margin: 0.2rem 0 0.55rem;
        font-size: 0.92rem;
        font-weight: 600;
        color: #1e293b;
        letter-spacing: 0.01em;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 0.5rem;
    }
    .assignment-attach-label-hint {
        font-size: 0.78rem;
        font-weight: 400;
        color: #64748b;
        letter-spacing: 0.02em;
    }
    /* 已选附件的极简标签 */
    .assignment-attach-tag {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        margin-top: 0.4rem;
        padding: 0.42rem 0.95rem;
        background: linear-gradient(180deg, rgba(239, 246, 255, 0.95) 0%, rgba(219, 234, 254, 0.85) 100%);
        border: 1px solid #bfdbfe;
        border-radius: 999px;
        font-size: 0.84rem;
        font-weight: 500;
        color: #1e3a8a;
        box-shadow: 0 2px 6px rgba(37, 99, 235, 0.06);
        max-width: 100%;
        overflow: hidden;
    }
    .assignment-attach-tag-icon {
        font-size: 0.96rem;
        line-height: 1;
        flex-shrink: 0;
    }
    .assignment-attach-tag-name {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 100%;
    }
    /* 把 file_uploader 改造成"图片上传 / 文件上传"两颗紧凑按钮 */
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploader"],
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploader"] {
        margin: 0 !important;
    }
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"],
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] {
        background: transparent !important;
        border: none !important;
        padding: 0 !important;
        min-height: 0 !important;
        box-shadow: none !important;
        display: block !important;
    }
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzoneInstructions"],
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzoneInstructions"] {
        display: none !important;
    }
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button,
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button {
        position: relative !important;
        width: 100% !important;
        min-height: 48px !important;
        padding: 0.55rem 1.15rem !important;
        background: rgba(255, 255, 255, 0.86) !important;
        border: 1px solid #bfdbfe !important;
        border-radius: 0.82rem !important;
        color: transparent !important;
        font-size: 0 !important;
        box-shadow: 0 2px 8px rgba(37, 99, 235, 0.08) !important;
        cursor: pointer !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        transform: translateY(0) scale(1);
        transform-origin: center center;
        will-change: transform;
        backface-visibility: hidden;
        -webkit-tap-highlight-color: transparent;
        transition:
            color 0.22s cubic-bezier(0.16, 1, 0.3, 1),
            box-shadow 0.22s cubic-bezier(0.16, 1, 0.3, 1),
            border-color 0.22s cubic-bezier(0.16, 1, 0.3, 1),
            background-color 0.22s cubic-bezier(0.16, 1, 0.3, 1) !important;
    }
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button > *,
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button > * {
        display: none !important;
    }
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button::after {
        content: "🖼  图片上传";
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        color: #1e3a8a;
        font-size: 0.94rem;
        font-weight: 600;
        letter-spacing: 0.02em;
        pointer-events: none;
    }
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button::after {
        content: "📎  文件上传";
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        color: #1e3a8a;
        font-size: 0.94rem;
        font-weight: 600;
        letter-spacing: 0.02em;
        pointer-events: none;
    }
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button:hover,
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button:focus-visible,
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button:hover,
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button:focus-visible {
        border-color: #93c5fd !important;
        box-shadow: 0 6px 14px rgba(37, 99, 235, 0.14) !important;
        animation: qElasticBounce 0.5s cubic-bezier(0.16, 1, 0.3, 1) both;
        outline: none !important;
    }
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button:active,
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button:active {
        animation: qElasticPress 0.34s cubic-bezier(0.16, 1, 0.3, 1) both;
    }
    /* 隐藏默认的"已选择文件"小框，改用我们自己的极简标签 */
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderFile"],
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderFile"],
    .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderFileData"],
    .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderFileData"] {
        display: none !important;
    }
    /* 清除附件用的小 ✕ 按钮 */
    .st-key-clear_image_attach button,
    .st-key-clear_file_attach button {
        width: 32px !important;
        min-width: 32px !important;
        height: 32px !important;
        min-height: 32px !important;
        padding: 0 !important;
        margin-top: 0.4rem !important;
        border-radius: 999px !important;
        background: transparent !important;
        color: #94a3b8 !important;
        border: 1px solid transparent !important;
        box-shadow: none !important;
        font-size: 0.85rem !important;
        font-weight: 500 !important;
    }
    .st-key-clear_image_attach button:hover,
    .st-key-clear_file_attach button:hover {
        background: rgba(239, 246, 255, 0.85) !important;
        border-color: #bfdbfe !important;
        color: #1d4ed8 !important;
    }
    @media (prefers-reduced-motion: reduce) {
        .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button:hover,
        .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button:focus-visible,
        .st-key-teacher_assignment_image_uploader [data-testid="stFileUploaderDropzone"] button:active,
        .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button:hover,
        .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button:focus-visible,
        .st-key-teacher_assignment_file_uploader [data-testid="stFileUploaderDropzone"] button:active {
            animation: none !important;
        }
    }
    .class-list-wrap {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 0.75rem;
    }
    .class-list-item {
        border: 1px solid rgba(186, 230, 253, 0.9);
        border-radius: 0.9rem;
        padding: 0.7rem 0.8rem;
        background: rgba(255, 255, 255, 0.72);
        color: #0f172a;
    }
    .class-list-item-name {
        font-weight: 600;
        color: #0f172a;
        word-break: break-word;
    }
    .class-list-item-code {
        margin-top: 0.2rem;
        color: #334155;
        font-size: 0.88rem;
        word-break: break-all;
    }
    .class-action-grid {
        width: 100%;
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 0.6rem;
        align-items: center;
    }

    @media (max-width: 960px) {
        .class-static-shell {
            padding: 0;
        }
        .pd-card-subtitle {
            max-width: 92%;
        }
        .pd-panel-inner {
            padding: 0 1.4rem 1.2rem;
        }
        .st-key-assignment_publish_card .pd-panel-inner {
            padding: 0.5rem 1.5rem 1.4rem !important;
        }
    }
    </style>
    """

    st.markdown(base_css, unsafe_allow_html=True)
    if is_logged_in:
        st.markdown(dashboard_css, unsafe_allow_html=True)
    else:
        st.markdown(auth_css, unsafe_allow_html=True)


def initialize_session_state() -> None:
    defaults = {
        "is_logged_in": False,
        "user_id": None,
        "username": "",
        "role": "",
        "current_page": "",
        "auth_view": "login",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def handle_login(db: DatabaseManager) -> None:
    with st.form("login_form"):
        username = st.text_input("用户名")
        password = st.text_input("密码")
        remember_me = st.checkbox("记住密码", value=True)
        submit = st.form_submit_button("登录", use_container_width=True, type="primary")
    if not submit:
        return
    st.session_state["remember_me"] = bool(remember_me)
    try:
        user = db.get_user_by_username(username.strip())
        if user is None or not db.verify_user_password(username.strip(), password):
            st.error("用户名或密码错误。")
            return
        if user.status == "banned":
            st.error("账号已被封禁，请联系管理员。")
            return
        st.session_state["is_logged_in"] = True
        st.session_state["user_id"] = user.id
        st.session_state["username"] = user.username
        st.session_state["role"] = user.role
        first_page = ROLE_PAGES.get(user.role, [""])[0]
        st.session_state["current_page"] = first_page
        st.rerun()
    except DatabaseError as exc:
        st.error(f"登录失败：{exc}")


def handle_register(db: DatabaseManager) -> None:
    with st.form("register_form", clear_on_submit=True):
        username = st.text_input("用户名")
        password = st.text_input("密码")
        role_label = st.selectbox("角色", list(ROLE_LABEL_TO_VALUE.keys()))
        contact = st.text_input("联系方式（邮箱/手机，可选）")
        submit = st.form_submit_button("注册", use_container_width=True, type="primary")
    if not submit:
        return
    if not username.strip():
        st.warning("用户名不能为空。")
        return
    try:
        db.create_user(
            username=username.strip(),
            password=password,
            role=ROLE_LABEL_TO_VALUE[role_label],
            contact=contact.strip() or None,
        )
        st.success("注册成功，请登录。")
    except DatabaseError as exc:
        st.error(f"注册失败：{exc}")


def handle_forgot_password_page(db: DatabaseManager) -> None:
    top_left, top_right = st.columns([0.68, 0.32], vertical_alignment="center")
    with top_left:
        st.markdown('<div class="auth-section-title">重置密码</div>', unsafe_allow_html=True)
    with top_right:
        if st.button("返回登录", key="back_to_login", use_container_width=True):
            st.session_state["auth_view"] = "login"
            st.rerun()

    with st.form("reset_form", clear_on_submit=True):
        username = st.text_input("用户名")
        contact = st.text_input("邮箱地址/手机")
        new_password = st.text_input("新密码")
        confirm_password = st.text_input("确认密码")
        submit = st.form_submit_button("重置密码", use_container_width=True, type="primary")

    if not submit:
        return
    if not username.strip() or not contact.strip() or len(new_password) < 6:
        st.warning("请填写完整信息且密码不少于 6 位。")
        return
    if new_password != confirm_password:
        st.warning("两次输入的新密码不一致。")
        return
    try:
        if not db.verify_user_contact(username.strip(), contact.strip()):
            st.error("用户名与联系方式不匹配。")
            return
        db.reset_user_password(username.strip(), new_password)
        st.success("密码已重置，请重新登录。")
        st.session_state["auth_view"] = "login"
    except DatabaseError as exc:
        st.error(f"重置失败：{exc}")


def render_auth_page(db: DatabaseManager) -> None:
    st.markdown('<div class="auth-logo">🎓</div>', unsafe_allow_html=True)
    st.markdown('<div class="auth-brand">AI 批改系统</div>', unsafe_allow_html=True)
    st.markdown('<div class="auth-sub">智能作业批改 · 高效教学管理</div>', unsafe_allow_html=True)
    if st.session_state.get("auth_view") == "forgot":
        handle_forgot_password_page(db)
        return

    login_tab, register_tab = st.tabs(["登录", "注册"])
    with login_tab:
        st.markdown('<div class="auth-section-title">欢迎回来</div>', unsafe_allow_html=True)
        handle_login(db)
        if st.button("忘记密码？点此找回", key="goto_forgot_password"):
            st.session_state["auth_view"] = "forgot"
            st.rerun()
    with register_tab:
        st.markdown('<div class="auth-section-title">创建账户</div>', unsafe_allow_html=True)
        handle_register(db)


def render_sidebar() -> None:
    role = st.session_state["role"]
    username = st.session_state["username"]
    avatar_char = username[0].upper() if username else "?"
    st.sidebar.markdown(
        f"""<div style="padding:0.75rem 0 1.25rem;border-bottom:1px solid #F1F5F9;margin-bottom:1rem;">
            <div style="font-size:1rem;font-weight:700;color:#0F172A;letter-spacing:-0.015em;margin-bottom:1rem;">
                🤖 AI 批改系统
            </div>
            <div style="display:flex;align-items:center;gap:0.625rem;">
                <div style="flex-shrink:0;width:34px;height:34px;border-radius:50%;
                    background:linear-gradient(135deg,#BFDBFE,#C7D2FE);
                    display:flex;align-items:center;justify-content:center;
                    font-size:0.875rem;font-weight:700;color:#1E40AF;">
                    {avatar_char}
                </div>
                <div>
                    <div style="font-size:0.875rem;font-weight:600;color:#0F172A;line-height:1.3;">{username}</div>
                    <div style="font-size:0.75rem;color:#94A3B8;line-height:1.3;">{ROLE_VALUE_TO_LABEL.get(role, role)}</div>
                </div>
            </div>
        </div>""",
        unsafe_allow_html=True,
    )
    pages = ROLE_PAGES.get(role, [])
    if pages:
        current = st.session_state.get("current_page", "")
        default_index = pages.index(current) if current in pages else 0
        selected_page = st.sidebar.radio(
            "导航",
            options=pages,
            index=default_index,
            label_visibility="collapsed",
            key="current_page",
        )
        _ = selected_page
    st.sidebar.markdown("<div style='height:0.75rem'></div>", unsafe_allow_html=True)
    if st.sidebar.button("退出登录", use_container_width=True):
        st.session_state["is_logged_in"] = False
        st.session_state["user_id"] = None
        st.session_state["username"] = ""
        st.session_state["role"] = ""
        st.rerun()


def call_ai_and_grade(db: DatabaseManager, submission_id: int, standard_answer: str, student_answer: str) -> None:
    try:
        with st.spinner("AI 正在批改..."):
            result = grade_answer(standard_answer, student_answer)
    except MissingDeepSeekAPIKeyError as exc:
        st.error(str(exc))
        st.info(
            "部署提示：进入 Streamlit Cloud → App → Settings → Secrets，"
            '添加一行 `DEEPSEEK_API_KEY = "你的真实密钥"`，保存后应用会自动重启。'
        )
        return
    except Exception as exc:
        db.grade_submission(submission_id, 0.0, f"AI 批改失败：{exc}", "error")
        st.error(f"AI 批改失败：{exc}")
        return

    db.grade_submission(submission_id, float(result["score"]), str(result["comment"]), "graded")
    st.success(f"批改完成：{result['score']} 分")


def save_assignment_attachment(uploaded_file: Any, creator_id: int, attachment_type: str) -> Dict[str, Any]:
    upload_root = Path("uploads") / "assignments" / f"teacher_{creator_id}"
    upload_root.mkdir(parents=True, exist_ok=True)
    original_name = Path(str(uploaded_file.name)).name
    suffix = Path(original_name).suffix.lower()
    random_tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{random_tag}{suffix}"
    target_path = upload_root / stored_name
    file_bytes = uploaded_file.getvalue()
    target_path.write_bytes(file_bytes)
    mime_type = str(uploaded_file.type or "").strip() or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    return {
        "attachment_type": attachment_type,
        "attachment_name": original_name,
        "attachment_path": str(target_path),
        "attachment_mime": mime_type,
        "attachment_size": len(file_bytes),
    }


def render_stat_card(label: str, value: str) -> None:
    safe_label = html.escape(str(label))
    safe_value = html.escape(str(value))
    st.markdown(
        f"""
        <section class="stat-card" role="group" aria-label="{html.escape(str(label), quote=True)}">
            <div class="stat-label">{safe_label}</div>
            <div class="stat-value">{safe_value}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_panel_header(title: str, subtitle: str) -> None:
    st.markdown(
        (
            f'<div class="panel-card">'
            f'<div class="panel-title">{title}</div>'
            f'<div class="panel-subtitle">{subtitle}</div>'
            f"</div>"
        ),
        unsafe_allow_html=True,
    )


def render_empty_state(
    icon: str,
    title: str,
    description: str,
    extra_class: str = "",
) -> None:
    class_attr = f"empty-state {extra_class}".strip()
    st.markdown(
        (
            f'<div class="{class_attr}">'
            f'<div class="empty-state-icon">{icon}</div>'
            f'<div class="empty-state-title">{title}</div>'
            f'<div class="empty-state-desc">{description}</div>'
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def format_chat_time(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%H:%M")
    except ValueError:
        return value.replace("T", " ")[:16]


def build_private_chat_html(
    messages: List[Dict[str, Any]],
    current_user_id: int,
    current_username: str,
    friend_username: str,
) -> str:
    chunks: List[str] = ['<div id="wx-chat-scroll" class="wx-chat-scroll">']
    for msg in messages:
        is_self = int(msg["sender_id"]) == current_user_id
        row_class = "wx-msg wx-msg-self" if is_self else "wx-msg wx-msg-peer"
        avatar_source = current_username if is_self else friend_username
        avatar_text = (avatar_source[:1] if avatar_source else "?").upper()
        avatar_class = "wx-avatar wx-avatar-self" if is_self else "wx-avatar wx-avatar-peer"
        content = html.escape(str(msg.get("content", ""))).replace("\n", "<br/>")
        ts = html.escape(format_chat_time(msg.get("timestamp", "")))
        chunks.append(
            f'<div class="{row_class}">'
            f'<div class="{avatar_class}">{html.escape(avatar_text)}</div>'
            '<div class="wx-msg-body">'
            f'<div class="wx-bubble">{content}</div>'
            f'<div class="wx-time">{ts}</div>'
            "</div>"
            "</div>"
        )
    chunks.append("</div>")
    return "".join(chunks)


def render_copy_code_button(code_text: str, element_id: str) -> None:
    """浏览器端一键复制，避免 DataFrame 复制浮层问题。"""
    safe_code = code_text.replace("\\", "\\\\").replace("'", "\\'")
    components.html(
        f"""
        <style>
            @keyframes elasticJiggle {{
                0%   {{ transform: translateY(0)      scale(1);     }}
                18%  {{ transform: translateY(-3px)   scale(0.979); }}
                38%  {{ transform: translateY(1.4px)  scale(1.010); }}
                58%  {{ transform: translateY(-1px)   scale(1.003); }}
                78%  {{ transform: translateY(0.4px)  scale(0.999); }}
                100% {{ transform: translateY(0)      scale(1);     }}
            }}
            @keyframes elasticPress {{
                0%   {{ transform: scale(1);     }}
                30%  {{ transform: scale(0.962); }}
                62%  {{ transform: scale(1.018); }}
                84%  {{ transform: scale(0.997); }}
                100% {{ transform: scale(1);     }}
            }}
            #copy-wrap {{ display:flex; justify-content:flex-end; }}
            #copy-wrap button {{
                background:#4F86F7; color:#fff; border:none; border-radius:8px;
                padding:6px 10px; font-size:12px; cursor:pointer;
                will-change:transform;
                transition:box-shadow 0.22s ease, background 0.22s ease;
            }}
            #copy-wrap button:hover {{
                box-shadow:0 5px 14px rgba(79,134,247,0.30);
                animation:elasticJiggle 0.46s cubic-bezier(0.16,1,0.3,1) both;
            }}
            #copy-wrap button:active {{
                animation:elasticPress 0.32s cubic-bezier(0.16,1,0.3,1) both;
            }}
        </style>
        <div id="copy-wrap">
            <button id="{element_id}">复制班级码</button>
        </div>
        <script>
            const btn = document.getElementById("{element_id}");
            if (btn) {{
                btn.onclick = async () => {{
                    try {{
                        await navigator.clipboard.writeText('{safe_code}');
                        const old = btn.innerText;
                        btn.innerText = '已复制 ✓';
                        setTimeout(() => btn.innerText = old, 1200);
                    }} catch (e) {{
                        btn.innerText = '复制失败';
                        setTimeout(() => btn.innerText = '复制班级码', 1200);
                    }}
                }};
            }}
        </script>
        """,
        height=38,
    )


def inject_assignment_card_css() -> None:
    """Scoped styles for assignment list, submission records, and grading center."""
    st.markdown(
        """
        <style>
        /* === Shared card shell tokens === */
        :root {
            --acard-radius: 1.65rem;
            --acard-border: 1px solid rgba(186, 230, 253, 0.72);
            --acard-bg: linear-gradient(180deg, rgba(255, 255, 255, 0.98) 0%, rgba(240, 249, 255, 0.92) 100%);
            --acard-shadow:
                0 1px 0 rgba(255, 255, 255, 0.95) inset,
                0 10px 22px -14px rgba(125, 211, 252, 0.38),
                0 20px 42px -24px rgba(56, 189, 248, 0.34),
                0 30px 58px -36px rgba(14, 116, 144, 0.24);
            --acard-text: #0f172a;
            --acard-muted: #64748b;
        }

        /* === Card shells (left & right on student submit, and trays) === */
        .st-key-student_assignment_list_card,
        .st-key-student_answer_card,
        .st-key-grading_tray {
            width: 100% !important;
            border: var(--acard-border) !important;
            border-radius: var(--acard-radius) !important;
            background: var(--acard-bg) !important;
            box-shadow: var(--acard-shadow) !important;
            padding: 24px 28px !important;
            min-height: 360px;
            animation: acard-fade-up 0.55s cubic-bezier(0.22, 1, 0.36, 1) both;
        }
        .st-key-student_answer_card { min-height: 420px; }

        /* Submission tray: sky-blue gradient surface for the student's record list */
        .st-key-submission_tray {
            width: 100% !important;
            border: 1px solid rgba(125, 211, 252, 0.55) !important;
            border-radius: 1.65rem !important;
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 45%, #7dd3fc 100%) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.85) inset,
                0 18px 36px -22px rgba(14, 165, 233, 0.35),
                0 30px 60px -36px rgba(14, 116, 144, 0.32) !important;
            padding: 26px 28px !important;
            min-height: 360px;
            animation: acard-fade-up 0.6s cubic-bezier(0.22, 1, 0.36, 1) both;
        }
        /* Header on sky-blue base needs a subtler divider so it doesn't look harsh */
        .st-key-submission_tray .acard-header {
            border-bottom-color: rgba(255, 255, 255, 0.65) !important;
        }

        /* === Outer sky-blue shell wrapping the student-submit two-column layout === */
        .st-key-student_submit_shell {
            width: 100% !important;
            border: 1px solid rgba(125, 211, 252, 0.55) !important;
            border-radius: 1.65rem !important;
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 45%, #7dd3fc 100%) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.85) inset,
                0 18px 36px -22px rgba(14, 165, 233, 0.35),
                0 30px 60px -36px rgba(14, 116, 144, 0.32) !important;
            padding: 28px 30px !important;
            min-height: 480px;
            display: flex !important;
            flex-direction: column !important;
            justify-content: center !important;
            align-items: stretch !important;
            gap: 1.25rem !important;
            animation: acard-fade-up 0.6s cubic-bezier(0.22, 1, 0.36, 1) both;
        }
        /* Make the inner two columns inherit the shell width with proper gap */
        .st-key-student_submit_shell > [data-testid="stVerticalBlock"] {
            width: 100% !important;
        }
        /* The inner left/right white cards become slightly more translucent over sky bg */
        .st-key-student_submit_shell .st-key-student_assignment_list_card,
        .st-key-student_submit_shell .st-key-student_answer_card {
            background:
                linear-gradient(180deg, rgba(255, 255, 255, 0.96) 0%, rgba(248, 252, 255, 0.92) 100%) !important;
            border-color: rgba(255, 255, 255, 0.7) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.9) inset,
                0 14px 30px -22px rgba(14, 116, 144, 0.32) !important;
        }

        @keyframes acard-fade-up {
            from { opacity: 0; transform: translate3d(0, 12px, 0); }
            to   { opacity: 1; transform: translate3d(0, 0, 0); }
        }

        .acard-header {
            display: flex;
            flex-direction: column;
            gap: 4px;
            padding: 0 4px 14px;
            border-bottom: 1px solid rgba(224, 242, 254, 0.85);
            margin-bottom: 16px;
        }
        .acard-title {
            font-size: 1.08rem;
            font-weight: 700;
            color: var(--acard-text);
            letter-spacing: -0.02em;
            line-height: 1.3;
        }
        .acard-subtitle {
            font-size: 0.82rem;
            color: var(--acard-muted);
            letter-spacing: -0.01em;
        }

        /* === Assignment list row (clickable, status-tinted) === */
        .st-key-student_assignment_list_card [data-testid="stVerticalBlock"] { gap: 10px !important; }

        .assignment-row-wrap {
            position: relative;
            margin-bottom: 0;
        }
        .assignment-row-wrap[data-status="pending"] [data-testid="stButton"] > button,
        .assignment-row-wrap[data-status="active-pending"] [data-testid="stButton"] > button {
            background: linear-gradient(135deg, #fff5f5 0%, #fee2e2 50%, #fecaca 100%) !important;
            border: 1px solid rgba(252, 165, 165, 0.6) !important;
            color: #7f1d1d !important;
        }
        .assignment-row-wrap[data-status="done"] [data-testid="stButton"] > button,
        .assignment-row-wrap[data-status="active-done"] [data-testid="stButton"] > button {
            background: linear-gradient(135deg, #ecfdf5 0%, #d1fae5 50%, #a7f3d0 100%) !important;
            border: 1px solid rgba(110, 231, 183, 0.7) !important;
            color: #064e3b !important;
        }
        .assignment-row-wrap[data-status="active-pending"] [data-testid="stButton"] > button,
        .assignment-row-wrap[data-status="active-done"] [data-testid="stButton"] > button {
            outline: 2px solid rgba(56, 189, 248, 0.85) !important;
            outline-offset: 2px;
            box-shadow:
                0 0 0 1px rgba(125, 211, 252, 0.55),
                0 14px 28px -14px rgba(14, 165, 233, 0.45) !important;
        }

        .assignment-row-wrap [data-testid="stButton"] > button {
            width: 100%;
            text-align: left !important;
            justify-content: flex-start !important;
            padding: 12px 18px !important;
            border-radius: 14px !important;
            font-weight: 600 !important;
            font-size: 0.95rem !important;
            line-height: 1.3 !important;
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
            min-height: 48px !important;
            transition: transform 0.25s cubic-bezier(0.22, 1, 0.36, 1),
                        box-shadow 0.25s cubic-bezier(0.22, 1, 0.36, 1) !important;
            will-change: transform;
        }
        .assignment-row-wrap [data-testid="stButton"] > button:hover {
            transform: translateY(-2px);
            box-shadow: 0 16px 28px -16px rgba(14, 116, 144, 0.32) !important;
        }
        .assignment-row-wrap [data-testid="stButton"] > button:active {
            transform: scale(0.98);
        }

        /* === Right answer pane: macOS-grade slide + scale-in === */
        .answer-pane-anchor { display: none; }
        .st-key-student_answer_card .answer-pane-anchor + div [data-testid="stVerticalBlock"],
        .answer-pane > [data-testid="stVerticalBlock"],
        [class*="st-key-answer_pane_inner_"] {
            animation: answer-pane-mac-in 0.55s cubic-bezier(0.22, 1, 0.36, 1) both;
            will-change: transform, opacity;
            transform-origin: 22% 50%;
        }
        @keyframes answer-pane-mac-in {
            0%   { opacity: 0; transform: translate3d(20px, 0, 0) scale(0.985); }
            60%  { opacity: 1; transform: translate3d(-2px, 0, 0) scale(1.004); }
            100% { opacity: 1; transform: translate3d(0, 0, 0)    scale(1);     }
        }
        @media (prefers-reduced-motion: reduce) {
            .st-key-student_answer_card .answer-pane-anchor + div [data-testid="stVerticalBlock"],
            [class*="st-key-answer_pane_inner_"] {
                animation: none !important;
            }
        }

        .answer-meta {
            color: var(--acard-text);
            font-size: 0.95rem;
            line-height: 1.6;
            padding: 6px 4px 10px;
        }
        .answer-meta strong { color: #0c4a6e; }

        /* === Expander cards (submission records & grading center) === */
        .st-key-submission_tray [data-testid="stExpander"],
        .st-key-grading_tray [data-testid="stExpander"] {
            border: 1px solid rgba(186, 230, 253, 0.7) !important;
            border-radius: 1.1rem !important;
            background: linear-gradient(180deg, rgba(255, 255, 255, 0.98) 0%, rgba(224, 242, 254, 0.55) 100%) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.92) inset,
                0 10px 22px -18px rgba(125, 211, 252, 0.34) !important;
            margin-bottom: 12px;
            overflow: hidden;
            transition: transform 0.25s cubic-bezier(0.22, 1, 0.36, 1),
                        box-shadow 0.25s cubic-bezier(0.22, 1, 0.36, 1);
        }
        .st-key-submission_tray [data-testid="stExpander"]:hover,
        .st-key-grading_tray [data-testid="stExpander"]:hover {
            transform: translateY(-2px);
            box-shadow:
                0 14px 28px -18px rgba(14, 116, 144, 0.32),
                0 22px 38px -28px rgba(56, 189, 248, 0.32) !important;
        }
        .st-key-submission_tray [data-testid="stExpander"] summary,
        .st-key-grading_tray [data-testid="stExpander"] summary,
        .st-key-submission_tray [data-testid="stExpander"] details > summary,
        .st-key-grading_tray [data-testid="stExpander"] details > summary {
            padding: 14px 22px !important;
            border-radius: 1.1rem !important;
            background: transparent !important;
            font-weight: 600;
            color: var(--acard-text);
        }
        .st-key-submission_tray [data-testid="stExpander"] summary svg,
        .st-key-grading_tray [data-testid="stExpander"] summary svg {
            transition: transform 0.42s cubic-bezier(0.22, 1, 0.36, 1);
        }
        .st-key-submission_tray [data-testid="stExpander"] details[open] > summary svg,
        .st-key-grading_tray [data-testid="stExpander"] details[open] > summary svg {
            transform: rotate(180deg);
        }

        /* === macOS-grade smooth expander dropdown ===
           Uses interpolate-size: allow-keywords (Chromium 129+/Safari 17.4+/FF 129+)
           to animate height: 0 -> auto. Older browsers fall back to native snap. */
        @supports (interpolate-size: allow-keywords) {
            .st-key-submission_tray,
            .st-key-grading_tray { interpolate-size: allow-keywords; }
            .st-key-submission_tray [data-testid="stExpander"] details > :not(summary),
            .st-key-grading_tray   [data-testid="stExpander"] details > :not(summary) {
                height: 0;
                opacity: 0;
                overflow: hidden;
                transform: translateY(-6px);
                transition:
                    height 0.42s cubic-bezier(0.22, 1, 0.36, 1),
                    opacity 0.32s cubic-bezier(0.22, 1, 0.36, 1) 0.04s,
                    transform 0.42s cubic-bezier(0.22, 1, 0.36, 1);
                will-change: height, opacity, transform;
            }
            .st-key-submission_tray [data-testid="stExpander"] details[open] > :not(summary),
            .st-key-grading_tray   [data-testid="stExpander"] details[open] > :not(summary) {
                height: auto;
                opacity: 1;
                transform: translateY(0);
            }
        }
        /* Fallback for older browsers: keep native behaviour but soften with opacity fade */
        @supports not (interpolate-size: allow-keywords) {
            .st-key-submission_tray [data-testid="stExpander"] details[open] > :not(summary),
            .st-key-grading_tray   [data-testid="stExpander"] details[open] > :not(summary) {
                animation: expander-soft-fade 0.4s cubic-bezier(0.22, 1, 0.36, 1) both;
            }
            @keyframes expander-soft-fade {
                from { opacity: 0; transform: translateY(-6px); }
                to   { opacity: 1; transform: translateY(0);    }
            }
        }
        @media (prefers-reduced-motion: reduce) {
            .st-key-submission_tray [data-testid="stExpander"] details > :not(summary),
            .st-key-grading_tray   [data-testid="stExpander"] details > :not(summary) {
                transition: none !important;
                animation: none !important;
            }
        }

        .submission-row-head {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 10px;
        }
        .submission-row-title {
            font-size: 1rem;
            font-weight: 700;
            color: var(--acard-text);
            letter-spacing: -0.02em;
        }
        .submission-row-meta {
            font-size: 0.78rem;
            color: var(--acard-muted);
        }
        .submission-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            border-radius: 999px;
            padding: 0.22rem 0.7rem;
            font-size: 0.7rem;
            font-weight: 600;
            line-height: 1;
            letter-spacing: -0.01em;
        }
        .submission-pill--graded { color: #166534; background: rgba(220, 252, 231, 0.92); }
        .submission-pill--pending { color: #9a3412; background: rgba(254, 215, 170, 0.85); }
        .submission-pill--score { color: #0c4a6e; background: rgba(186, 230, 253, 0.78); }
        .submission-pill--count { color: #1e3a8a; background: rgba(191, 219, 254, 0.78); }

        .submission-detail-block {
            background: rgba(255, 255, 255, 0.78);
            border: 1px solid rgba(224, 242, 254, 0.95);
            border-radius: 14px;
            padding: 14px 18px;
            margin-bottom: 10px;
        }
        .submission-detail-label {
            font-size: 0.78rem;
            font-weight: 700;
            color: #0c4a6e;
            letter-spacing: 0.02em;
            text-transform: uppercase;
            margin-bottom: 6px;
        }
        .submission-detail-text {
            font-size: 0.92rem;
            line-height: 1.6;
            color: #0f172a;
            white-space: pre-wrap;
        }
        .submission-detail-text--muted { color: var(--acard-muted); font-style: italic; }

        /* Per-student card inside an assignment expander */
        .grading-student-card {
            background: linear-gradient(180deg, rgba(255, 255, 255, 0.96) 0%, rgba(240, 249, 255, 0.78) 100%);
            border: 1px solid rgba(186, 230, 253, 0.7);
            border-radius: 14px;
            padding: 14px 18px;
            margin-bottom: 10px;
        }
        .grading-student-head {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 8px;
        }
        .grading-student-avatar {
            width: 32px;
            height: 32px;
            border-radius: 999px;
            background: radial-gradient(circle at 30% 30%, rgba(224, 242, 254, 0.95), rgba(240, 249, 255, 0.85));
            border: 1px solid rgba(186, 230, 253, 0.85);
            color: #0c4a6e;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 0.82rem;
        }
        .grading-student-name {
            font-weight: 700;
            color: #0f172a;
            font-size: 0.95rem;
            letter-spacing: -0.01em;
        }

        /* Q 弹关键帧（批改区按钮，仅 transform，60FPS）
           hover：先入场弹簧余韵，再停留在上抬 -2px 的稳态——hover 全程可感知 */
        @keyframes qGradeElasticBounce {
            0%   { transform: translateY(0)      scale(1);     }
            22%  { transform: translateY(-3px)   scale(0.988); }
            48%  { transform: translateY(-0.6px) scale(1.012); }
            72%  { transform: translateY(-2.4px) scale(1.002); }
            100% { transform: translateY(-2px)   scale(1);     }
        }
        @keyframes qGradeElasticPress {
            0%   { transform: translateY(-2px)   scale(1);     }
            36%  { transform: translateY(-1.4px) scale(0.97);  }
            66%  { transform: translateY(-2.6px) scale(1.014); }
            100% { transform: translateY(-2px)   scale(1);     }
        }

        /* 批改区：AI 重批 / 保存评语 各自独立一行、水平居中 */
        .st-key-teacher_ai_regrade_row,
        .st-key-teacher_save_feedback_row {
            width: 100% !important;
        }
        .st-key-teacher_ai_regrade_row [data-testid="stVerticalBlock"],
        .st-key-teacher_save_feedback_row [data-testid="stVerticalBlock"] {
            width: 100% !important;
        }
        .st-key-teacher_ai_regrade_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3)),
        .st-key-teacher_save_feedback_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3)) {
            display: flex !important;
            justify-content: center !important;
            align-items: stretch !important;
            gap: 0.75rem !important;
        }
        .st-key-teacher_ai_regrade_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"],
        .st-key-teacher_save_feedback_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"] {
            flex: 0 0 auto !important;
        }
        .st-key-teacher_ai_regrade_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"]:nth-child(1),
        .st-key-teacher_ai_regrade_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"]:nth-child(3),
        .st-key-teacher_save_feedback_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"]:nth-child(1),
        .st-key-teacher_save_feedback_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"]:nth-child(3) {
            min-width: 0 !important;
        }
        .st-key-teacher_ai_regrade_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"]:nth-child(2) {
            flex: 0 1 min(100%, 18rem) !important;
            min-width: min(100%, 12rem) !important;
            max-width: 18rem !important;
        }
        .st-key-teacher_save_feedback_row
            [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"]:nth-child(2) {
            flex: 0 1 min(100%, 20rem) !important;
            min-width: min(100%, 12rem) !important;
            max-width: 20rem !important;
        }

        /* 基础态——显式写 transform 起点，避免动画首帧 reflow；提升特异性以压过 .stButton > button 全局规则 */
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button,
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button {
            border-radius: 12px !important;
            font-weight: 650 !important;
            font-size: 0.9rem !important;
            letter-spacing: 0.01em !important;
            min-height: 44px !important;
            line-height: 1.1 !important;
            white-space: nowrap !important;
            width: 100% !important;
            justify-content: center !important;
            padding: 0.5rem 1.1rem !important;
            transform: translateY(0) scale(1);
            transform-origin: center center !important;
            will-change: transform !important;
            backface-visibility: hidden !important;
            -webkit-tap-highlight-color: transparent !important;
            transition: background 0.22s cubic-bezier(0.16, 1, 0.3, 1),
                border-color 0.22s cubic-bezier(0.16, 1, 0.3, 1),
                box-shadow 0.22s cubic-bezier(0.16, 1, 0.3, 1),
                color 0.22s cubic-bezier(0.16, 1, 0.3, 1) !important;
            animation: none !important;
        }
        /* 保存评语：浅蓝渐变（与背景天蓝呼应，作为副 CTA） */
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button,
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button p {
            color: #0c4a6e !important;
        }
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button {
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 50%, #7dd3fc 100%) !important;
            border: 1px solid rgba(125, 211, 252, 0.7) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.9) inset,
                0 8px 18px -12px rgba(14, 165, 233, 0.42) !important;
        }
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):hover,
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):focus,
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):focus-visible {
            background: linear-gradient(135deg, #bae6fd 0%, #7dd3fc 50%, #38bdf8 100%) !important;
            border-color: rgba(56, 189, 248, 0.9) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.95) inset,
                0 14px 26px -10px rgba(14, 165, 233, 0.55) !important;
            outline: none !important;
            animation: qGradeElasticBounce 0.55s cubic-bezier(0.16, 1, 0.3, 1) both !important;
        }
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):focus-visible {
            box-shadow:
                0 0 0 2px #f8fafc,
                0 0 0 4px rgba(56, 189, 248, 0.55) !important;
        }
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):active {
            background: linear-gradient(135deg, #7dd3fc 0%, #38bdf8 50%, #0ea5e9 100%) !important;
            border-color: rgba(14, 165, 233, 0.95) !important;
            animation: qGradeElasticPress 0.34s cubic-bezier(0.16, 1, 0.3, 1) both !important;
        }

        /* AI 重批：深天蓝渐变（主 CTA） */
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button,
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button p {
            color: #f0f9ff !important;
        }
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button {
            background: linear-gradient(135deg, #38bdf8 0%, #0ea5e9 50%, #0284c7 100%) !important;
            border: 1px solid rgba(2, 132, 199, 0.9) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.28) inset,
                0 10px 22px -12px rgba(2, 132, 199, 0.6) !important;
        }
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):hover,
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):focus,
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):focus-visible {
            background: linear-gradient(135deg, #0ea5e9 0%, #0284c7 50%, #0369a1 100%) !important;
            border-color: rgba(3, 105, 161, 0.95) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.22) inset,
                0 16px 30px -10px rgba(14, 165, 233, 0.6) !important;
            outline: none !important;
            animation: qGradeElasticBounce 0.55s cubic-bezier(0.16, 1, 0.3, 1) both !important;
        }
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):focus-visible {
            box-shadow: 0 0 0 2px #f8fafc, 0 0 0 4px #0ea5e9 !important;
        }
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):active {
            background: linear-gradient(135deg, #0284c7 0%, #0369a1 50%, #075985 100%) !important;
            animation: qGradeElasticPress 0.34s cubic-bezier(0.16, 1, 0.3, 1) both !important;
        }
        html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:disabled,
        html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:disabled {
            animation: none !important;
            transform: translateY(0) scale(1) !important;
            will-change: auto !important;
        }
        @media (prefers-reduced-motion: reduce) {
            html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):hover,
            html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):focus,
            html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):focus-visible,
            html body [class*="st-key-teacher_save_feedback_"] [data-testid="stButton"] > button:not(:disabled):active,
            html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):hover,
            html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):focus,
            html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):focus-visible,
            html body [class*="st-key-teacher_ai_regrade_"] [data-testid="stButton"] > button:not(:disabled):active {
                animation: none !important;
                transform: translateY(0) scale(1) !important;
            }
        }

        /* Stagger card entry inside trays */
        .st-key-submission_tray > [data-testid="stVerticalBlock"] > [data-testid="stVerticalBlockBorderWrapper"]:nth-child(1),
        .st-key-grading_tray   > [data-testid="stVerticalBlock"] > [data-testid="stVerticalBlockBorderWrapper"]:nth-child(1) {
            animation-delay: 0ms;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_teacher_pages(db: DatabaseManager, page: str) -> None:
    user_id = int(st.session_state["user_id"])
    if page == "班级管理":
        classes = db.list_classes_by_teacher(user_id)
        submissions = db.list_submissions_for_teacher(user_id)
        pending_count = len([item for item in submissions if item.get("status") != "graded"])
        graded_count = len([item for item in submissions if item.get("status") == "graded"])
        avg_score = 0.0
        scored_rows = [float(item["score"]) for item in submissions if item.get("score") is not None]
        if scored_rows:
            avg_score = sum(scored_rows) / len(scored_rows)

        with st.container(key="class_kpi_row"):
            col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
            with col1:
                render_stat_card("班级数量", str(len(classes)))
            with col2:
                render_stat_card("已收作业", str(len(submissions)))
            with col3:
                render_stat_card("已批改", str(graded_count))
            with col4:
                render_stat_card("平均分", f"{avg_score:.1f}")

        left, right = st.columns([7, 3], gap="large")
        with left:
            st.markdown('<div class="class-static-shell">', unsafe_allow_html=True)

            with st.container(key="class_create_card"):
                st.markdown(
                    """
                    <div class="pd-card-header">
                        <span class="pd-card-icon">🏫</span>
                        <div class="pd-card-title">班级创建</div>
                        <div class="pd-card-subtitle">创建班级后系统会立即生成 6 位班级码，便于学生快速加入与后续作业发布。</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.markdown('<div class="pd-panel-inner"><div class="pd-functional-zone">', unsafe_allow_html=True)
                with st.form("create_class", clear_on_submit=True):
                    class_name = st.text_input("班级名称", placeholder="例如：高一（3）班")
                    submit = st.form_submit_button(
                        "创建班级",
                        use_container_width=True,
                        key="teacher_create_class_btn",
                    )
                if submit and class_name.strip():
                    st.success(f"创建成功：{db.create_class(user_id, class_name.strip())['class_code']}")
                st.markdown('</div></div>', unsafe_allow_html=True)

            with st.container(key="class_list_card"):
                st.markdown(
                    """
                    <div class="pd-card-header">
                        <span class="pd-card-icon">📋</span>
                        <div class="pd-card-title">班级列表</div>
                        <div class="pd-card-subtitle">集中查看班级名称与班级码，按需复制并分发给学生，维持统一的班级管理入口。</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.markdown('<div class="pd-panel-inner"><div class="pd-functional-zone">', unsafe_allow_html=True)
                if classes:
                    st.markdown('<div class="class-list-wrap">', unsafe_allow_html=True)
                    for idx, item in enumerate(classes):
                        st.markdown(
                            f"""
                            <div class="class-list-item">
                                <div class="class-list-item-name">{html.escape(item.class_name)}</div>
                                <div class="class-list-item-code">班级码：{html.escape(item.class_code)}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        render_copy_code_button(item.class_code, f"copy_class_code_{idx}")
                    st.markdown('</div>', unsafe_allow_html=True)
                else:
                    render_empty_state("📚", "还没有班级", "创建首个班级后，系统会自动生成 6 位班级码，学生可立即加入。")
                if pending_count > 0:
                    st.warning(f"当前还有 {pending_count} 份作业待批改，建议优先处理。")
                st.markdown('</div></div>', unsafe_allow_html=True)

            st.markdown('</div>', unsafe_allow_html=True)
        with right:
            st.markdown(
                '<div class="panel-card"><div class="panel-title">操作日志</div>'
                '<div class="panel-subtitle">最近活动将帮助你快速掌握当前教学进度。</div>'
                f'<div class="soft-list-item">班级总数：{len(classes)} 个</div>'
                f'<div class="soft-list-item">待批改作业：{pending_count} 份</div>'
                f'<div class="soft-list-item">已批改作业：{graded_count} 份</div>'
                f'<div class="soft-list-item">平均分：{avg_score:.1f}</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="panel-card"><div class="panel-title">使用建议</div>'
                '<div class="soft-list-item">1. 班级名建议使用“年级+班级”结构，便于检索</div>'
                '<div class="soft-list-item">2. 先建立班级，再批量发布作业，流程更顺畅</div>'
                '<div class="soft-list-item">3. 建议每日固定时间处理待批改队列</div>'
                '</div>',
                unsafe_allow_html=True,
            )
    elif page == "作业发布":
        classes = db.list_classes_by_teacher(user_id)
        assignments = db.list_assignments_by_creator(user_id)
        class_map = {f"{c.class_name}": c.class_id for c in classes}
        upcoming = len([a for a in assignments if a.get("deadline")])
        with st.container(key="assignment_kpi_row"):
            col1, col2, col3 = st.columns([1, 1, 1])
            with col1:
                render_stat_card("可选班级", str(len(classes)))
            with col2:
                render_stat_card("已发布作业", str(len(assignments)))
            with col3:
                render_stat_card("含截止时间", str(upcoming))

        left, right = st.columns([7, 3], gap="large")
        with left:
            if not classes:
                render_empty_state("🧭", "当前无法发布作业", "你还没有可投放的班级，请先前往“班级管理”创建班级。")
                if st.button("立即去创建班级", key="jump_to_class_manage", use_container_width=True):
                    st.session_state["current_page"] = "班级管理"
                    st.rerun()
            else:
                st.markdown('<div class="class-static-shell">', unsafe_allow_html=True)
                with st.container(key="assignment_publish_card"):
                    st.markdown(
                        """
                        <div class="pd-card-header">
                            <span class="pd-card-icon">📝</span>
                            <div class="pd-card-title">作业发布</div>
                            <div class="pd-card-subtitle">填写题目与标准答案，选择目标班级并设置截止时间，学生即可在「作业提交」中查看。</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        '<div class="pd-panel-inner"><div class="pd-functional-zone">',
                        unsafe_allow_html=True,
                    )
                    if st.session_state.pop("teacher_publish_success", False):
                        st.success("发布成功。")
                    st.markdown(
                        '<div style="text-align:center;font-weight:700;color:#1f2a44;margin:2px 0 8px;">标题</div>',
                        unsafe_allow_html=True,
                    )
                    title = st.text_input(
                        "标题",
                        key="teacher_publish_title",
                        placeholder="为本次作业起一个简短的题目标题",
                        label_visibility="collapsed",
                    )
                    st.markdown(
                        '<div style="text-align:center;font-weight:700;color:#1f2a44;margin:8px 0;">题目内容</div>',
                        unsafe_allow_html=True,
                    )
                    content = st.text_area(
                        "题目内容",
                        placeholder="输入作业题目正文或问题描述",
                        key="teacher_publish_content_text",
                        height=140,
                        label_visibility="collapsed",
                    )
                    st.markdown(
                        '<div class="assignment-attach-label" style="text-align:center;">附件（可选）</div>',
                        unsafe_allow_html=True,
                    )
                    attach_col_img, attach_col_file = st.columns(2, gap="small")
                    with attach_col_img:
                        uploaded_image = st.file_uploader(
                            "图片上传",
                            type=["png", "jpg", "jpeg", "webp"],
                            key="teacher_assignment_image_uploader",
                            label_visibility="collapsed",
                            accept_multiple_files=False,
                        )
                    with attach_col_file:
                        uploaded_file = st.file_uploader(
                            "文件上传",
                            type=["pdf", "doc", "docx", "txt", "ppt", "pptx", "xlsx", "xls"],
                            key="teacher_assignment_file_uploader",
                            label_visibility="collapsed",
                            accept_multiple_files=False,
                        )
                    if uploaded_image is not None:
                        tag_col_img, clear_col_img = st.columns([18, 1], gap="small")
                        with tag_col_img:
                            st.markdown(
                                '<div class="assignment-attach-tag">'
                                '<span class="assignment-attach-tag-icon">🖼</span>'
                                f'<span class="assignment-attach-tag-name">{uploaded_image.name}</span>'
                                '</div>',
                                unsafe_allow_html=True,
                            )
                        with clear_col_img:
                            if st.button("✕", key="clear_image_attach", help="移除图片附件"):
                                st.session_state.pop("teacher_assignment_image_uploader", None)
                                st.rerun()
                    if uploaded_file is not None:
                        tag_col_file, clear_col_file = st.columns([18, 1], gap="small")
                        with tag_col_file:
                            st.markdown(
                                '<div class="assignment-attach-tag">'
                                '<span class="assignment-attach-tag-icon">📎</span>'
                                f'<span class="assignment-attach-tag-name">{uploaded_file.name}</span>'
                                '</div>',
                                unsafe_allow_html=True,
                            )
                        with clear_col_file:
                            if st.button("✕", key="clear_file_attach", help="移除文件附件"):
                                st.session_state.pop("teacher_assignment_file_uploader", None)
                                st.rerun()
                    st.markdown(
                        '<div style="text-align:center;font-weight:700;color:#1f2a44;margin:8px 0;">标准答案</div>',
                        unsafe_allow_html=True,
                    )
                    standard_answer = st.text_area(
                        "标准答案",
                        placeholder="填写本题的参考答案，AI 批改时会以此对照",
                        key="teacher_publish_standard_answer",
                        height=140,
                        label_visibility="collapsed",
                    )
                    st.markdown(
                        '<div style="text-align:center;font-weight:700;color:#1f2a44;margin:8px 0;">截止日期</div>',
                        unsafe_allow_html=True,
                    )
                    deadline = st.date_input(
                        "截止日期",
                        value=datetime.now().date(),
                        key="teacher_publish_deadline",
                        label_visibility="collapsed",
                    )
                    st.markdown(
                        '<div style="text-align:center;font-weight:700;color:#1f2a44;margin:8px 0;">目标班级</div>',
                        unsafe_allow_html=True,
                    )
                    if "teacher_publish_targets_manual" not in st.session_state:
                        st.session_state["teacher_publish_targets_manual"] = []
                    target_keyword = st.text_input(
                        "搜索班级",
                        key="teacher_publish_target_search",
                        placeholder="输入班级名称进行搜索",
                    )
                    filtered_class_options = [
                        class_name
                        for class_name in class_map.keys()
                        if target_keyword.strip() in class_name
                    ] if target_keyword.strip() else []
                    selected_target_option: Optional[str] = None
                    if target_keyword.strip():
                        if not filtered_class_options:
                            st.warning("未找到匹配班级，请调整关键词。")
                        else:
                            selected_target_option = st.radio(
                                "搜索结果",
                                options=filtered_class_options,
                                key="teacher_publish_target_search_result",
                            )
                    if st.button(
                        "添加到目标班级",
                        key="teacher_publish_target_add_btn",
                        use_container_width=True,
                    ):
                        if not target_keyword.strip():
                            st.warning("请先输入班级关键词。")
                        elif selected_target_option is None:
                            st.warning("请先选择要添加的班级。")
                        else:
                            current_targets = st.session_state.get("teacher_publish_targets_manual", [])
                            if selected_target_option in current_targets:
                                st.info("该班级已在目标列表中。")
                            else:
                                st.session_state["teacher_publish_targets_manual"] = current_targets + [
                                    selected_target_option
                                ]
                                st.success("已添加到目标班级。")
                    targets = st.session_state.get("teacher_publish_targets_manual", [])
                    if targets:
                        selected_target_to_remove = st.radio(
                            "已选目标班级（点击可移除）",
                            options=targets,
                            key="teacher_publish_target_remove_pick",
                        )
                        if st.button(
                            "移除选中班级",
                            key="teacher_publish_target_remove_btn",
                            use_container_width=True,
                        ):
                            st.session_state["teacher_publish_targets_manual"] = [
                                class_name
                                for class_name in targets
                                if class_name != selected_target_to_remove
                            ]
                            st.rerun()
                    submit = st.button(
                        "发布作业",
                        use_container_width=True,
                        type="primary",
                        key="teacher_publish_assignment_btn",
                    )
                    if submit:
                        attachment_payload: Dict[str, Any] = {}
                        if not title.strip():
                            st.warning("请先填写作业标题。")
                        elif not content.strip():
                            st.warning("请先填写题目内容。")
                        elif not targets:
                            st.warning("请至少选择一个目标班级。")
                        else:
                            if uploaded_file is not None:
                                try:
                                    attachment_payload = save_assignment_attachment(
                                        uploaded_file, user_id, "file"
                                    )
                                except OSError:
                                    st.error("文件保存失败，请重试。")
                                    st.stop()
                            elif uploaded_image is not None:
                                try:
                                    attachment_payload = save_assignment_attachment(
                                        uploaded_image, user_id, "image"
                                    )
                                except OSError:
                                    st.error("图片保存失败，请重试。")
                                    st.stop()
                            db.create_assignment(
                                title=title.strip(),
                                content=content.strip(),
                                standard_answer=standard_answer.strip(),
                                deadline=datetime.combine(deadline, datetime.min.time()),
                                target_classes=[class_map[t] for t in targets],
                                creator_id=user_id,
                                attachment_type=attachment_payload.get("attachment_type"),
                                attachment_name=attachment_payload.get("attachment_name"),
                                attachment_path=attachment_payload.get("attachment_path"),
                                attachment_mime=attachment_payload.get("attachment_mime"),
                                attachment_size=attachment_payload.get("attachment_size"),
                            )
                            for _publish_key in (
                                "teacher_publish_title",
                                "teacher_publish_content_text",
                                "teacher_assignment_image_uploader",
                                "teacher_assignment_file_uploader",
                                "teacher_publish_standard_answer",
                                "teacher_publish_deadline",
                                "teacher_publish_targets_manual",
                                "teacher_publish_target_search",
                                "teacher_publish_target_search_result",
                                "teacher_publish_target_remove_pick",
                            ):
                                st.session_state.pop(_publish_key, None)
                            st.session_state["teacher_publish_success"] = True
                            st.rerun()
                    st.markdown("</div></div>", unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)

            st.markdown('<div class="class-static-shell">', unsafe_allow_html=True)
            with st.container(key="assignment_recent_card"):
                st.markdown(
                    """
                    <div class="pd-card-header">
                        <span class="pd-card-icon">📋</span>
                        <div class="pd-card-title">最近发布</div>
                        <div class="pd-card-subtitle">最近 5 条作业发布记录</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.markdown(
                    '<div class="pd-panel-inner"><div class="pd-recent-content">',
                    unsafe_allow_html=True,
                )
                class_name_map = db.get_class_name_map(user_id)
                if assignments:
                    for item in assignments[:5]:
                        class_names = [class_name_map.get(class_id, f"班级{class_id}") for class_id in json.loads(item["target_classes"])]
                        st.markdown(f"**{item['title']}**")
                        st.caption(f"目标班级：{', '.join(class_names)}")
                        st.caption(f"截止时间：{item['deadline'] or '未设置'}")
                        st.divider()
                else:
                    render_empty_state(
                        "📝",
                        "暂未发布作业",
                        "建议先创建一个练习作业，用于验证班级批改流程。",
                        extra_class="empty-state--sky",
                    )
                st.markdown("</div></div>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)
        with right:
            st.markdown(
                '<div class="panel-card"><div class="panel-title">发布清单</div>'
                '<div class="soft-list-item">1. 标题尽量包含章节和题型</div>'
                '<div class="soft-list-item">2. 标准答案建议结构化，便于 AI 批改</div>'
                '<div class="soft-list-item">3. 目标班级至少选择一个</div>'
                '<div class="soft-list-item">4. 截止时间建议晚于当前 24 小时</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="panel-card"><div class="panel-title">发布后流程</div>'
                '<div class="soft-list-item">学生提交后会进入“批改中心”</div>'
                '<div class="soft-list-item">你可以在批改中心执行 AI 重批</div>'
                '<div class="soft-list-item">消息中心可同步推送提醒</div>'
                '</div>',
                unsafe_allow_html=True,
            )
    elif page == "批改中心":
        inject_assignment_card_css()
        records = db.list_submissions_for_teacher(user_id)
        if not records:
            render_empty_state("📥", "暂无提交", "学生提交作业后，会按作业分组在此展示。")
            return

        groups: Dict[int, Dict[str, Any]] = {}
        for r in records:
            aid = int(r["assignment_id"])
            g = groups.setdefault(
                aid,
                {
                    "title": str(r.get("assignment_title") or ""),
                    "content": str(r.get("content") or ""),
                    "standard": str(r.get("standard_answer") or ""),
                    "items": [],
                },
            )
            g["items"].append(r)

        with st.container(key="grading_tray"):
            st.markdown(
                f"""
                <div class="acard-header">
                    <div class="acard-title">作业批改中心</div>
                    <div class="acard-subtitle">共 {len(groups)} 份作业 · 点击展开查看每位学生的答题、AI 评语并写入老师评语</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            for aid, g in groups.items():
                items: List[Dict[str, Any]] = g["items"]
                count = len(items)
                graded_scores = [float(it["score"]) for it in items if it.get("score") is not None]
                avg_text = ""
                if graded_scores:
                    avg = sum(graded_scores) / len(graded_scores)
                    avg_text = f" · 平均分 {avg:.1f}"

                expander_label = f"{g['title']} · 已提交 {count} 人{avg_text}"
                with st.expander(expander_label, expanded=False):
                    st.markdown(
                        f"""
                        <div class="submission-row-head">
                            <span class="submission-row-title">{html.escape(g['title'])}</span>
                            <span class="submission-pill submission-pill--count">提交 {count} 人</span>
                            {f'<span class="submission-pill submission-pill--score">平均 {sum(graded_scores)/len(graded_scores):.1f}</span>' if graded_scores else ''}
                        </div>
                        <div class="submission-detail-block">
                            <div class="submission-detail-label">作业内容</div>
                            <div class="submission-detail-text">{html.escape(g['content']) or '<span class="submission-detail-text--muted">未填写。</span>'}</div>
                        </div>
                        <div class="submission-detail-block">
                            <div class="submission-detail-label">参考答案</div>
                            <div class="submission-detail-text">{html.escape(g['standard']) or '<span class="submission-detail-text--muted">未提供。</span>'}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

                    for item in items:
                        sid = int(item["id"])
                        student_username = str(item.get("student_username") or "")
                        student_answer = str(item.get("student_answer") or "")
                        feedback_value = str(item.get("feedback") or "")
                        score_value = item.get("score")
                        status_value = str(item.get("status") or "submitted").lower()
                        is_graded = status_value == "graded" or score_value is not None
                        avatar_text = (student_username[:1] or "?").upper()
                        pill_html = (
                            f'<span class="submission-pill submission-pill--graded">已批改</span>'
                            if is_graded
                            else f'<span class="submission-pill submission-pill--pending">待批改</span>'
                        )
                        score_pill = (
                            f'<span class="submission-pill submission-pill--score">得分 {score_value}</span>'
                            if score_value is not None
                            else ""
                        )

                        st.markdown(
                            f"""
                            <div class="grading-student-card">
                                <div class="grading-student-head">
                                    <span class="grading-student-avatar">{html.escape(avatar_text)}</span>
                                    <span class="grading-student-name">{html.escape(student_username)}</span>
                                    {pill_html}
                                    {score_pill}
                                    <span class="submission-row-meta">提交编号 #{sid}</span>
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                        st.markdown(
                            f"""
                            <div class="submission-detail-block">
                                <div class="submission-detail-label">学生答题</div>
                                <div class="submission-detail-text">{html.escape(student_answer) or '<span class="submission-detail-text--muted">未填写。</span>'}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                        feedback_key = f"teacher_feedback_input_{sid}"
                        score_key = f"teacher_score_input_{sid}"
                        if feedback_key not in st.session_state:
                            st.session_state[feedback_key] = feedback_value
                        if score_key not in st.session_state:
                            st.session_state[score_key] = (
                                float(score_value) if score_value is not None else 0.0
                            )

                        with st.container(key="teacher_ai_regrade_row"):
                            _ai_l, _ai_mid, _ai_r = st.columns([1, 2.4, 1], gap="medium")
                            with _ai_l:
                                st.empty()
                            with _ai_mid:
                                ai_clicked = st.button(
                                    "AI 重批",
                                    key=f"teacher_ai_regrade_{sid}",
                                    use_container_width=True,
                                    type="primary",
                                )
                            with _ai_r:
                                st.empty()

                        st.text_area(
                            "老师评语",
                            key=feedback_key,
                            height=110,
                            placeholder="为该学生写一段反馈…",
                        )

                        with st.container(key="teacher_save_feedback_row"):
                            _sv_l, _sv_mid, _sv_r = st.columns([1, 2.4, 1], gap="medium")
                            with _sv_l:
                                st.empty()
                            with _sv_mid:
                                save_clicked = st.button(
                                    "保存评语",
                                    key=f"teacher_save_feedback_{sid}",
                                    use_container_width=True,
                                )
                            with _sv_r:
                                st.empty()

                        if save_clicked:
                            new_feedback = str(st.session_state.get(feedback_key, "")).strip()
                            new_score = float(st.session_state.get(score_key, 0.0) or 0.0)
                            try:
                                db.grade_submission(
                                    sid,
                                    new_score,
                                    new_feedback,
                                    status="graded",
                                )
                                st.success(f"已保存对 {student_username} 的评语。")
                                st.rerun()
                            except DatabaseError as exc:
                                st.error(f"保存失败：{exc}")
                        if ai_clicked:
                            detail = db.get_submission_detail(sid)
                            if detail:
                                call_ai_and_grade(
                                    db,
                                    sid,
                                    str(detail["standard_answer"] or ""),
                                    str(detail["student_answer"]),
                                )
    elif page == "消息中心":
        render_message_center(db, "teacher")


def render_student_pages(db: DatabaseManager, page: str) -> None:
    user_id = int(st.session_state["user_id"])
    if page == "班级加入":
        classes = db.list_classes_by_student(user_id)
        col1, col2 = st.columns([1.1, 1.9])
        with col1:
            with st.container(key="student_join_class_card"):
                st.markdown(
                    """
                    <div class="student-card-header">
                        <span class="pd-card-icon">📝</span>
                        <div class="pd-card-title">加入班级</div>
                        <div class="pd-card-subtitle">输入 6 位班级码加入所在班级</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.markdown(
                    '<div class="pd-panel-inner student-join-panel"><div class="pd-functional-zone">',
                    unsafe_allow_html=True,
                )
                with st.form("join_class", clear_on_submit=True):
                    class_code = st.text_input("班级码（6位）").upper()
                    submit = st.form_submit_button(
                        "加入",
                        use_container_width=True,
                        key="student_join_class_btn",
                    )
                if submit and len(class_code.strip()) == 6:
                    try:
                        db.add_student_to_class_by_code(user_id, class_code.strip())
                        st.success("加入成功。")
                    except DatabaseError as exc:
                        st.error(str(exc))
                st.markdown("</div></div>", unsafe_allow_html=True)
        with col2:
            with st.container(key="student_joined_list_card"):
                st.markdown(
                    """
                    <div class="student-card-header">
                        <span class="pd-card-icon">📋</span>
                        <div class="pd-card-title">班级列表</div>
                        <div class="pd-card-subtitle">已加入的班级会显示在此，班级码在条目右侧以代码样式展示</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.markdown(
                    '<div class="pd-panel-inner student-list-panel"><div class="pd-functional-zone">',
                    unsafe_allow_html=True,
                )
                if classes:
                    for item in classes:
                        name = (item.class_name or "").strip() or "未命名班级"
                        av = name[0].upper() if name else "?"
                        safe_name = html.escape(name)
                        safe_code = html.escape(item.class_code)
                        st.markdown(
                            f"""
                            <div class="student-class-row">
                                <div class="student-class-row__lead">
                                    <div class="student-class-avatar">{html.escape(av)}</div>
                                    <div class="student-class-body">
                                        <div class="student-class-name-line">
                                            <span class="student-class-name">{safe_name}</span>
                                            <span class="student-pill student-pill--active">
                                                <span class="student-pill-dot"></span>已加入
                                            </span>
                                        </div>
                                        <div class="student-class-sub">班级 ID：{item.class_id}</div>
                                    </div>
                                </div>
                                <div class="student-class-code-chip" title="班级码">{safe_code}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                else:
                    st.info("你还没有加入班级。")
                st.markdown("</div></div>", unsafe_allow_html=True)
    elif page == "作业提交":
        inject_assignment_card_css()
        assignments = db.list_assignments_for_student(user_id)
        if not assignments:
            render_empty_state("📭", "暂无可提交作业", "等老师发布新作业后会在此展示。")
            return

        submitted_records = db.list_submissions_by_student(user_id)
        latest_submission_by_aid: Dict[int, Dict[str, Any]] = {}
        for r in submitted_records:
            aid = int(r["assignment_id"])
            if aid not in latest_submission_by_aid:
                latest_submission_by_aid[aid] = r

        # Sort: pending assignments first (light-red), done assignments after (light-green).
        pending_items = [a for a in assignments if a.id not in latest_submission_by_aid]
        done_items = [a for a in assignments if a.id in latest_submission_by_aid]
        ordered_assignments = pending_items + done_items

        selected_key = "student_selected_assignment_id"
        valid_ids = {a.id for a in ordered_assignments}
        if st.session_state.get(selected_key) not in valid_ids:
            st.session_state[selected_key] = ordered_assignments[0].id
        selected_id = int(st.session_state[selected_key])
        selected_item = next(
            (a for a in ordered_assignments if a.id == selected_id),
            ordered_assignments[0],
        )

        with st.container(key="student_submit_shell"):
            left, right = st.columns([1.05, 1.35])

            with left:
                with st.container(key="student_assignment_list_card"):
                    st.markdown(
                        """
                        <div class="acard-header">
                            <div class="acard-title">作业列表</div>
                            <div class="acard-subtitle">共 {count} 项 · 待提交（淡红）排在前，已提交（淡绿）排在后</div>
                        </div>
                        """.format(count=len(ordered_assignments)),
                        unsafe_allow_html=True,
                    )
                    for item in ordered_assignments:
                        is_done = item.id in latest_submission_by_aid
                        is_active = item.id == selected_id
                        status_attr = "done" if is_done else "pending"
                        if is_active:
                            status_attr = "active-done" if is_done else "active-pending"

                        st.markdown(
                            f'<div class="assignment-row-wrap" data-status="{status_attr}">',
                            unsafe_allow_html=True,
                        )
                        if st.button(
                            item.title,
                            key=f"student_assignment_pick_{item.id}",
                            use_container_width=True,
                        ):
                            st.session_state[selected_key] = item.id
                            st.rerun()
                        st.markdown("</div>", unsafe_allow_html=True)

            with right:
                with st.container(key="student_answer_card"):
                    st.markdown(
                        f"""
                        <div class="acard-header">
                            <div class="acard-title">答题区域</div>
                            <div class="acard-subtitle">当前作业：{html.escape(selected_item.title)}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    anchor_id = f"answer-pane-{selected_item.id}"
                    st.markdown(
                        f'<div class="answer-pane-anchor" data-key="{anchor_id}"></div>',
                        unsafe_allow_html=True,
                    )
                    with st.container(key=f"answer_pane_inner_{selected_item.id}"):
                        safe_content = html.escape(selected_item.content or "")
                        st.markdown(
                            f"""
                            <div class="answer-meta">
                                <strong>题目内容：</strong>{safe_content}
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        attachment_path_obj = (
                            Path(selected_item.attachment_path) if selected_item.attachment_path else None
                        )
                        if attachment_path_obj is not None and attachment_path_obj.exists():
                            attachment_bytes = attachment_path_obj.read_bytes()
                            attachment_name = selected_item.attachment_name or attachment_path_obj.name
                            attachment_mime = selected_item.attachment_mime or "application/octet-stream"
                            st.markdown("**题目附件**")
                            if selected_item.attachment_type == "image":
                                st.image(attachment_bytes, caption=attachment_name, use_container_width=True)
                            st.download_button(
                                label="下载题目附件",
                                data=attachment_bytes,
                                file_name=attachment_name,
                                mime=attachment_mime,
                                key=f"student_assignment_attachment_download_{selected_item.id}",
                                use_container_width=True,
                            )
                        elif selected_item.attachment_path:
                            st.info("该作业附件不存在，可能已被移动，请联系老师重新上传。")

                        with st.form(f"submit_assignment_{selected_item.id}", clear_on_submit=True):
                            answer = st.text_area("我的答案", key=f"student_answer_input_{selected_item.id}")
                            submit = st.form_submit_button(
                                "提交并AI批改", use_container_width=True, type="primary"
                            )
                        if submit and answer.strip():
                            sid = db.create_submission(user_id, selected_item.id, answer.strip())
                            call_ai_and_grade(db, sid, selected_item.standard_answer, answer.strip())
    elif page == "提交记录":
        inject_assignment_card_css()
        rows = db.list_submissions_by_student(user_id)
        if not rows:
            render_empty_state("🗂️", "暂无提交记录", "完成作业并提交后，记录会在此处展示。")
        else:
            with st.container(key="submission_tray"):
                st.markdown(
                    f"""
                    <div class="acard-header">
                        <div class="acard-title">我的提交记录</div>
                        <div class="acard-subtitle">共 {len(rows)} 条 · 点击展开查看作业内容、AI 批改与老师评语</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                for r in rows:
                    title = str(r.get("title") or "")
                    status = str(r.get("status") or "submitted").lower()
                    score = r.get("score")
                    feedback = str(r.get("feedback") or "").strip()
                    student_answer = str(r.get("student_answer") or "").strip()
                    assignment_content = str(r.get("content") or "").strip()
                    standard_answer = str(r.get("standard_answer") or "").strip()

                    is_graded = status == "graded" or score is not None
                    pill_class = "submission-pill--graded" if is_graded else "submission-pill--pending"
                    pill_text = "已批改" if is_graded else "待批改"
                    score_pill = (
                        f'<span class="submission-pill submission-pill--score">得分 {score}</span>'
                        if score is not None
                        else ""
                    )
                    header_html = (
                        f'<div class="submission-row-head">'
                        f'<span class="submission-row-title">{html.escape(title)}</span>'
                        f'<span class="submission-pill {pill_class}">{pill_text}</span>'
                        f"{score_pill}"
                        f"</div>"
                    )

                    expander_label = f"{title} · {pill_text}" + (f" · 得分 {score}" if score is not None else "")
                    with st.expander(expander_label, expanded=False):
                        st.markdown(header_html, unsafe_allow_html=True)
                        st.markdown(
                            f"""
                            <div class="submission-detail-block">
                                <div class="submission-detail-label">作业内容</div>
                                <div class="submission-detail-text">{html.escape(assignment_content) or '<span class="submission-detail-text--muted">未填写。</span>'}</div>
                            </div>
                            <div class="submission-detail-block">
                                <div class="submission-detail-label">我的答案</div>
                                <div class="submission-detail-text">{html.escape(student_answer) or '<span class="submission-detail-text--muted">未填写。</span>'}</div>
                            </div>
                            <div class="submission-detail-block">
                                <div class="submission-detail-label">参考答案</div>
                                <div class="submission-detail-text">{html.escape(standard_answer) or '<span class="submission-detail-text--muted">老师未提供参考答案。</span>'}</div>
                            </div>
                            <div class="submission-detail-block">
                                <div class="submission-detail-label">AI 批改与老师评语</div>
                                <div class="submission-detail-text">{html.escape(feedback) or '<span class="submission-detail-text--muted">暂无评语。</span>'}</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
    elif page == "消息中心":
        render_message_center(db, "student")


def render_admin_page(db: DatabaseManager) -> None:
    users = db.list_users()
    if not users:
        st.info("暂无用户。")
        return

    st.markdown(
        """
        <style>
        .st-key-account_tray {
            width: 100% !important;
            border: 1px solid rgba(186, 230, 253, 0.72) !important;
            border-radius: 1.65rem !important;
            overflow: hidden !important;
            background: linear-gradient(180deg, rgba(255, 255, 255, 0.98) 0%, rgba(240, 249, 255, 0.9) 100%) !important;
            box-shadow:
                0 1px 0 rgba(255, 255, 255, 0.95) inset,
                0 10px 22px -14px rgba(125, 211, 252, 0.38),
                0 20px 42px -24px rgba(56, 189, 248, 0.34),
                0 30px 58px -36px rgba(14, 116, 144, 0.24) !important;
            padding: 0 !important;
        }
        .st-key-account_tray > [data-testid="stVerticalBlock"] {
            padding: 14px 28px !important;
            gap: 0 !important;
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"] {
            padding: 12px 34px;
            margin: 0;
            background: transparent;
            transition: background-color 0.28s cubic-bezier(0.4, 0, 0.2, 1),
                        box-shadow 0.28s cubic-bezier(0.4, 0, 0.2, 1);
            border-radius: 10px;
            gap: 0 !important;
            display: flex;
            align-items: center;
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"]:hover {
            background: rgba(239, 246, 255, 0.72);
            box-shadow: 0 8px 14px -14px rgba(125, 211, 252, 0.72);
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"]:not(:first-child) {
            border-top: 1px solid rgba(224, 242, 254, 0.95);
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"] [data-testid="column"] {
            gap: 0 !important;
            display: flex;
            align-items: center;
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"] [data-testid="column"] > div {
            height: auto;
            display: flex;
            align-items: center;
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"] [data-testid="column"]:first-child > div {
            padding-left: 4px;
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"] [data-testid="column"]:last-child > div {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            padding-right: 4px;
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"] [data-testid="column"]:last-child [data-testid="stButton"] > button {
            border-radius: 0.85rem;
            border: 1px solid rgba(186, 230, 253, 0.78);
            background: rgba(248, 250, 252, 0.84);
            color: #0f172a;
            font-weight: 600;
            font-size: 0.72rem;
            line-height: 1;
            padding-block: 0;
            padding-inline: 10px;
            min-height: 30px;
            white-space: nowrap;
            opacity: 0.72;
            box-shadow: none;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .st-key-account_tray [data-testid="stHorizontalBlock"] [data-testid="column"]:last-child [data-testid="stButton"] > button:hover {
            opacity: 1;
            background: rgba(224, 242, 254, 0.95);
            border-color: rgba(125, 211, 252, 0.9);
            transform: translateY(-1px);
            box-shadow: 0 10px 15px -3px rgba(186, 230, 253, 0.28), 0 4px 6px -4px rgba(186, 230, 253, 0.22);
        }
        .account-main {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .account-main > div:last-child {
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .account-avatar {
            width: 42px;
            height: 42px;
            min-width: 42px;
            min-height: 42px;
            border-radius: 999px;
            border: 1px solid rgba(186, 230, 253, 0.75);
            background: radial-gradient(circle at 30% 30%, rgba(224, 242, 254, 0.92), rgba(240, 249, 255, 0.85));
            color: #0f4c6f;
            box-sizing: border-box;
            padding: 0;
            font-size: 14px;
            font-weight: 700;
            line-height: 1;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            overflow: hidden;
        }
        .account-avatar-char {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 100%;
            margin: 0;
            padding: 0;
            line-height: 1;
            letter-spacing: 0;
            font-size: inherit;
            font-weight: inherit;
            color: inherit;
        }
        .account-title-row {
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
            margin: 0 0 2px;
        }
        .account-name {
            color: #0f172a;
            font-size: 0.9rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            line-height: 1.3;
        }
        .account-role-badge {
            border: 1px solid rgba(186, 230, 253, 0.85);
            background: rgba(224, 242, 254, 0.85);
            color: #0c4a6e;
            border-radius: 999px;
            font-size: 0.7rem;
            line-height: 1;
            font-weight: 600;
            letter-spacing: -0.02em;
            padding: 0.22rem 0.75rem;
        }
        .account-meta {
            color: #64748b;
            font-size: 0.78rem;
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
            line-height: 1.4;
            letter-spacing: -0.02em;
        }
        .account-status {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 5px;
            border-radius: 999px;
            padding: 0.22rem 0.75rem;
            font-size: 0.7rem;
            font-weight: 600;
            line-height: 1;
            letter-spacing: -0.02em;
        }
        .account-status-dot {
            width: 6px;
            height: 6px;
            border-radius: 999px;
            display: inline-block;
        }
        .account-status-active {
            color: #166534;
            background: rgba(220, 252, 231, 0.9);
        }
        .account-status-active .account-status-dot {
            background: #22c55e;
            box-shadow: 0 0 0 2px rgba(34, 197, 94, 0.14);
        }
        .account-status-inactive {
            color: #475569;
            background: rgba(226, 232, 240, 0.9);
        }
        .account-status-inactive .account-status-dot {
            background: #94a3b8;
            box-shadow: 0 0 0 2px rgba(148, 163, 184, 0.16);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("### 账户列表")

    tray = st.container(key="account_tray")
    with tray:
        for user in users:
            user_id = int(user["id"])
            username = str(user["username"])
            role = str(user["role"])
            contact = str(user.get("contact") or "-")
            status = str(user.get("status") or "active").lower()
            avatar_text = html.escape((username[:1] or "?").upper())
            safe_name = html.escape(username)
            safe_role = html.escape(role.capitalize())
            safe_contact = html.escape(contact)
            status_label = "活跃" if status == "active" else "已停用"
            status_class = "account-status-active" if status == "active" else "account-status-inactive"

            info_col, action_col = st.columns([5.2, 1.4], vertical_alignment="center")
            with info_col:
                st.markdown(
                    f"""
                    <div class="account-main">
                        <div class="account-avatar">
                            <span class="account-avatar-char">{avatar_text}</span>
                        </div>
                        <div>
                            <div class="account-title-row">
                                <span class="account-name">{safe_name}</span>
                                <span class="account-role-badge">{safe_role}</span>
                                <span class="account-status {status_class}">
                                    <span class="account-status-dot"></span>{status_label}
                                </span>
                            </div>
                            <div class="account-meta">
                                <span>ID: {user_id}</span>
                                <span>联系方式: {safe_contact}</span>
                            </div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with action_col:
                if st.button("删除账户", key=f"delete_user_{user_id}", use_container_width=True):
                    if user_id == int(st.session_state["user_id"]):
                        st.warning("不能删除当前管理员。")
                    else:
                        try:
                            db.delete_user(user_id)
                            st.success(f"已删除用户：{username}")
                            st.rerun()
                        except DatabaseError as exc:
                            st.error(f"删除失败：{exc}")


def render_message_center(db: DatabaseManager, role: str) -> None:
    current_user_id = int(st.session_state["user_id"])
    current_username = str(st.session_state.get("username", ""))

    # ── 初始化 session state ──
    for key, default in [
        ("mc_social_active_tab", "friends"),
        ("mc_tab_anim_from", None),
        ("mc_selected_type", None),
        ("mc_selected_id", None),
        ("mc_selected_name", ""),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ── 数据准备 ──
    friends = db.list_friends(current_user_id)
    pending_requests = db.list_received_friend_requests(current_user_id)
    conversations: List[Dict[str, Any]] = []
    for friend in friends:
        conversations.append(
            {
                "type": "private",
                "id": int(friend["id"]),
                "name": str(friend["username"]),
            }
        )

    valid_friend_ids: Set[int] = {int(f["id"]) for f in friends}
    if "mc_friend" in st.query_params:
        raw: Any = st.query_params.get("mc_friend")
        if isinstance(raw, list) and len(raw) > 0:
            raw = raw[0]
        try:
            if raw is not None:
                pick_id = int(str(raw))
                if pick_id in valid_friend_ids:
                    st.session_state["mc_selected_type"] = "private"
                    st.session_state["mc_selected_id"] = pick_id
                    for fr in friends:
                        if int(fr["id"]) == pick_id:
                            st.session_state["mc_selected_name"] = str(fr["username"])
                            break
        except (TypeError, ValueError):
            pass
        if "mc_friend" in st.query_params:
            try:
                del st.query_params["mc_friend"]
            except Exception:
                pass
        st.rerun()

    # 默认选中第一条会话
    if st.session_state["mc_selected_id"] is None and conversations:
        st.session_state["mc_selected_type"] = conversations[0]["type"]
        st.session_state["mc_selected_id"] = conversations[0]["id"]
        st.session_state["mc_selected_name"] = conversations[0]["name"]

    sel_type = st.session_state.get("mc_selected_type")
    sel_id = st.session_state.get("mc_selected_id")
    sel_name = str(st.session_state.get("mc_selected_name", ""))
    active_tab = str(st.session_state.get("mc_social_active_tab", "friends"))
    tab_anim_from = st.session_state.get("mc_tab_anim_from")
    content_motion_class = ""
    if tab_anim_from in {"friends", "add"} and tab_anim_from != active_tab:
        content_motion_class = "mc-pane-enter-right" if active_tab == "add" else "mc-pane-enter-left"
        st.session_state["mc_tab_anim_from"] = None

    if sel_type != "private" or (sel_id is not None and int(sel_id) not in valid_friend_ids):
        st.session_state["mc_selected_type"] = None
        st.session_state["mc_selected_id"] = None
        st.session_state["mc_selected_name"] = ""
        sel_type = None
        sel_id = None
        sel_name = ""
    active_tab_key = "mc_tab_add" if active_tab == "add" else "mc_tab_friends"
    st.markdown(
        f"<style>.st-key-{active_tab_key}>button{{"
        f"color:#0C4A6E !important;"
        f"font-weight:720 !important;"
        f"letter-spacing:0 !important;"
        f"transition:color 0.2s cubic-bezier(0.4, 0, 0.2, 1),"
        f"font-weight 0.2s cubic-bezier(0.4, 0, 0.2, 1),"
        f"letter-spacing 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;"
        f"}}</style>",
        unsafe_allow_html=True,
    )

    # ── 双栏布局：社交管理 / 聊天窗口 ──
    col_list, col_chat = st.columns([0.34, 0.66], gap="medium")

    # ── 左侧：好友列表与添加好友合并卡片 ──
    with col_list:
        st.markdown('<div class="mc-list">', unsafe_allow_html=True)
        st.markdown(
            '<div class="mc-list-title">社交管理</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="mc-nav-shell">', unsafe_allow_html=True)
        tab_friend_col, tab_add_col = st.columns(2)
        with tab_friend_col:
            if st.button("好友列表", key="mc_tab_friends", use_container_width=True):
                st.session_state["mc_tab_anim_from"] = active_tab
                st.session_state["mc_social_active_tab"] = "friends"
                st.rerun()
        with tab_add_col:
            if st.button("添加好友", key="mc_tab_add", use_container_width=True):
                st.session_state["mc_tab_anim_from"] = active_tab
                st.session_state["mc_social_active_tab"] = "add"
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        st.markdown('<div class="mc-divider"></div>', unsafe_allow_html=True)

        if active_tab == "friends":
            st.markdown(f'<div class="mc-tab-content {content_motion_class}">', unsafe_allow_html=True)
            if not friends:
                st.markdown(
                    '<div class="mc-list-empty">暂无好友，前往“添加好友”发起新的连接。</div>',
                    unsafe_allow_html=True,
                )
            else:
                for friend in friends:
                    friend_id = int(friend["id"])
                    friend_name = str(friend["username"])
                    role_label = ROLE_VALUE_TO_LABEL.get(str(friend["role"]), str(friend["role"]))
                    letter = html.escape((friend_name[:1] or "?").upper())
                    status_value = str(friend.get("status") or "inactive").lower()
                    is_account_active = status_value == "active"
                    status_pill_text = "活跃" if is_account_active else "停用"
                    is_chat_selected = sel_type == "private" and sel_id is not None and int(sel_id) == friend_id
                    active_cls = " mc-friend-pick--active" if is_chat_selected else ""
                    status_pill_class = (
                        "mc-friend-pill mc-friend-pill--status"
                        if is_account_active
                        else "mc-friend-pill mc-friend-pill--status-muted"
                    )
                    dot_class = (
                        "mc-friend-pill__dot"
                        if is_account_active
                        else "mc-friend-pill__dot mc-friend-pill__dot--off"
                    )
                    if st.button(
                        " ",
                        key=f"mc_pick_friend_{friend_id}",
                        use_container_width=True,
                    ):
                        st.session_state["mc_selected_type"] = "private"
                        st.session_state["mc_selected_id"] = friend_id
                        st.session_state["mc_selected_name"] = friend_name
                        st.rerun()
                    st.markdown(
                        f'<div class="mc-friend-pick{active_cls}">'
                        f'<div class="mc-friend-list__avatar">'
                        f'<span class="mc-friend-list__avatar-char">{letter}</span></div>'
                        f'<div class="mc-friend-list__body">'
                        f'<span class="mc-friend-list__name">{html.escape(friend_name)}</span>'
                        f'<div class="mc-friend-list__pills">'
                        f'<span class="mc-friend-pill mc-friend-pill--role">{html.escape(role_label)}</span>'
                        f'<span class="{status_pill_class}">'
                        f'<span class="{dot_class}"></span>{html.escape(status_pill_text)}</span>'
                        f"</div></div></div>",
                        unsafe_allow_html=True,
                    )
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="mc-tab-content {content_motion_class}">', unsafe_allow_html=True)
            if pending_requests:
                for req in pending_requests:
                    st.markdown(
                        '<div class="mc-request-row">'
                        f'<div class="mc-request-user">{html.escape(str(req["sender_name"]))} 申请添加好友</div>',
                        unsafe_allow_html=True,
                    )
                    ca, cr = st.columns(2)
                    with ca:
                        if st.button("同意", key=f"mc_accept_{req['id']}", use_container_width=True):
                            db.respond_friend_request(int(req["id"]), current_user_id, True)
                            st.rerun()
                    with cr:
                        if st.button("拒绝", key=f"mc_reject_{req['id']}", use_container_width=True):
                            db.respond_friend_request(int(req["id"]), current_user_id, False)
                            st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)
            else:
                st.markdown(
                    '<div class="mc-list-empty">暂无待处理好友申请。</div>',
                    unsafe_allow_html=True,
                )

            st.markdown('<div class="mc-search-block">', unsafe_allow_html=True)
            keyword = st.text_input("搜索用户", key="mc_friend_search", placeholder="输入用户名或联系方式")
            search_results = db.search_users(keyword.strip(), current_user_id) if keyword.strip() else []
            selected_user_id: Optional[int] = None
            if keyword.strip():
                if not search_results:
                    st.warning("未找到匹配用户，请调整关键词。")
                else:
                    user_options: Dict[str, int] = {}
                    for item in search_results:
                        user_label = (
                            f'{str(item["username"])}'
                            f'（{ROLE_VALUE_TO_LABEL.get(item["role"], item["role"])}）'
                        )
                        user_options[user_label] = int(item["id"])
                    # 用 radio 代替 selectbox，避免 BaseWeb 下拉层在部分环境下出现黑底浮层
                    selected_label = st.radio(
                        "搜索结果",
                        options=list(user_options.keys()),
                        key="mc_friend_search_result",
                    )
                    selected_user_id = user_options.get(selected_label)
            if st.button("添加好友", key="mc_add_friend_action", use_container_width=True):
                if not keyword.strip():
                    st.warning("请先输入搜索关键词。")
                elif selected_user_id is None:
                    st.warning("请先选择要添加的用户。")
                else:
                    try:
                        db.send_friend_request(current_user_id, selected_user_id)
                        st.success("好友请求已发送。")
                    except DatabaseError as exc:
                        st.warning(str(exc))
            st.markdown("</div>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

    # ── 右侧：聊天窗口 ──
    with col_chat:
        if sel_id is None:
            st.markdown(
                '<div style="display:flex;flex-direction:column;align-items:center;'
                'justify-content:center;height:58vh;color:#94A3B8;">'
                '<div style="font-size:2.5rem;margin-bottom:1rem;">💬</div>'
                '<div style="font-size:0.95rem;font-weight:600;">选择一个会话开始聊天</div>'
                "</div>",
                unsafe_allow_html=True,
            )
            return

        st.markdown(f'<div class="mc-chat-header">{html.escape(sel_name)}</div>', unsafe_allow_html=True)

        if sel_type == "private":
            msgs = db.list_private_messages(current_user_id, int(sel_id))
            st.markdown(
                build_private_chat_html(
                    messages=msgs,
                    current_user_id=current_user_id,
                    current_username=current_username,
                    friend_username=sel_name,
                ),
                unsafe_allow_html=True,
            )
            components.html(
                "<script>const e=window.parent.document.getElementById('wx-chat-scroll');"
                "if(e)e.scrollTop=e.scrollHeight;</script>",
                height=0,
            )
            with st.form(f"mc_send_p_{sel_id}", clear_on_submit=True):
                content = st.text_area(
                    "", placeholder="输入消息，点击发送",
                    key=f"mc_pi_{sel_id}", label_visibility="collapsed", height=80,
                )
                submit = st.form_submit_button("发送 →", use_container_width=True, type="primary")
            if submit and content.strip():
                db.send_message(current_user_id, int(sel_id), content.strip(), False)
                st.rerun()

        elif sel_type == "group":
            msgs = db.list_group_messages_by_class(int(sel_id))
            st.markdown(
                build_private_chat_html(
                    messages=msgs,
                    current_user_id=current_user_id,
                    current_username=current_username,
                    friend_username=sel_name,
                ),
                unsafe_allow_html=True,
            )
            components.html(
                "<script>const e=window.parent.document.getElementById('wx-chat-scroll');"
                "if(e)e.scrollTop=e.scrollHeight;</script>",
                height=0,
            )
            if role == "teacher":
                with st.form(f"mc_send_g_{sel_id}", clear_on_submit=True):
                    content = st.text_area(
                        "", placeholder="向班级发送通知...",
                        key=f"mc_gi_{sel_id}", label_visibility="collapsed", height=80,
                    )
                    submit = st.form_submit_button("群发 →", use_container_width=True, type="primary")
                if submit and content.strip():
                    db.send_message(current_user_id, int(sel_id), content.strip(), True)
                    st.rerun()
            else:
                st.markdown(
                    '<div style="padding:0.5rem 0;color:#94A3B8;font-size:0.82rem;text-align:center;">'
                    "班级通知仅老师可发送消息</div>",
                    unsafe_allow_html=True,
                )


_PAGE_META: Dict[str, tuple] = {
    "班级管理":  ("班级管理",  "管理班级、查看统计数据与作业完成情况"),
    "作业发布":  ("作业发布",  "创建新作业并分配给目标班级"),
    "批改中心":  ("批改中心",  "查看学生提交，一键触发 AI 智能批改"),
    "消息中心":  ("消息中心",  "私信好友，向班级群发通知"),
    "班级加入":  ("班级加入",  "输入 6 位班级码即可加入所在班级"),
    "作业提交":  ("作业提交",  "选择作业，填写答案，获得即时 AI 反馈"),
    "提交记录":  ("提交记录",  "查看历史提交及批改得分与评语"),
    "用户管理":  ("用户管理",  "管理系统中所有用户账号与权限"),
}


def render_home_page(db: DatabaseManager) -> None:
    role = st.session_state["role"]
    page = st.session_state.get("current_page", "")
    if page in _PAGE_META:
        title, desc = _PAGE_META[page]
        st.markdown(
            f'<div class="page-header">'
            f'<div class="page-title">{title}</div>'
            f'<div class="page-desc">{desc}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    if role == "teacher":
        render_teacher_pages(db, page)
    elif role == "student":
        render_student_pages(db, page)
    elif role == "admin":
        render_admin_page(db)


def main() -> None:
    st.set_page_config(page_title="AI 自动批改系统", page_icon="🤖", layout="wide")
    initialize_session_state()
    inject_custom_css(is_logged_in=bool(st.session_state.get("is_logged_in", False)))
    try:
        db = DatabaseManager()
    except DatabaseError as exc:
        st.error(f"数据库初始化失败：{exc}")
        st.stop()

    if not st.session_state["is_logged_in"]:
        render_auth_page(db)
        st.stop()
    render_sidebar()
    render_home_page(db)


if __name__ == "__main__":
    main()
