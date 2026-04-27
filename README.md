# AI Homework Grader

一个基于 **Streamlit + SQLite + DeepSeek API** 的作业管理与 AI 批改系统。  
支持教师发布作业、学生提交答案、自动评分与评语反馈。

## 功能简介

- 用户登录与角色管理（教师 / 学生 / 管理员）
- 班级与作业管理
- 学生提交作业与查看结果
- 基于 DeepSeek 的自动批改（分数 + 评语）
- 本地 SQLite 数据存储

## 技术栈

- Python 3.10+
- Streamlit
- sqlite3
- OpenAI SDK（兼容调用 DeepSeek 接口）

