# 文档 5.5 的 Dockerfile 骨架，加上这个 lab 的一点点「Agent 服务」味道。
#
# 三条 Agent 服务容易踩的坑都写在注释里了：
#   1) 冷启动要等模型/向量库，别让 K8s 太早来探活；
#   2) 探活（/live）绝不能依赖下游，否则下游抖一下会被 K8s 全体重启；
#   3) 长任务要配合优雅停机，见 v6_release.py 场景 D。

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 依赖先装，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 探活只回答「进程还活着吗」—— 不要在这里 ping 下游
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/live || exit 1

# 密钥一律走环境变量/Secret，不要写进镜像
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]