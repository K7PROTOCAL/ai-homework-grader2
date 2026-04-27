# GitHub + Streamlit Cloud 部署指南（无明文 API）

## 目标

- 代码仓库不保存任何真实 API Key。
- DeepSeek API Key 只放在 Streamlit Cloud Secrets 中。
- 上传到 GitHub 后可直接部署为公开网站。

## 一、部署前检查

1. 确认项目根目录存在 `.gitignore`，并包含：
   - `.streamlit/secrets.toml`
   - `.env`
   - `*.db`
2. 确认 `ai_service.py` 使用 `st.secrets["DEEPSEEK_API_KEY"]` 读取密钥。
3. 确认 `requirements.txt` 包含 `streamlit`、`openai` 等依赖。

## 二、上传到 GitHub

在 PowerShell 中执行（先激活 `test_env`）：

```powershell
conda activate test_env
cd C:\Users\24333\Desktop\cursor

git init
git add .
git commit -m "Initial commit: deploy-ready for Streamlit Cloud"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git push -u origin main
```

> 如果仓库已初始化，只执行 `git add .` / `git commit` / `git push` 即可。

## 三、在 Streamlit Cloud 创建应用

1. 打开 <https://share.streamlit.io>
2. 使用 GitHub 账号登录。
3. 点击 **New app**。
4. 选择：
   - Repository：你的仓库
   - Branch：`main`
   - Main file path：`app.py`

## 四、配置 Secrets（关键步骤）

在应用的 **Settings -> Secrets** 中粘贴：

```toml
DEEPSEEK_API_KEY = "sk-你的真实密钥"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
```

如果你希望跨重启保持密码可解密一致，也可以补充：

```toml
PASSWORD_ENCRYPTION_KEY = "你的44位Fernet密钥"
```

保存后，应用会自动重启并生效。

## 五、上线后验证

1. 打开公开链接，确认主页可访问。
2. 登录后触发一次 AI 批改。
3. 如果提示缺少密钥，检查 Secrets 中键名是否完全一致：
   - `DEEPSEEK_API_KEY`

## 六、常见问题

1. **Q: 为什么本地可以跑，云端报缺少密钥？**  
   A: Streamlit Cloud 需要单独在 Secrets 页面配置。

2. **Q: 可以把 key 写在代码里再上传吗？**  
   A: 不可以。公开仓库会泄露密钥，必须只放 Secrets。

3. **Q: 不小心提交过密钥怎么办？**  
   A: 立刻去 DeepSeek 平台轮换（废弃旧 key，生成新 key），并清理 Git 历史。
