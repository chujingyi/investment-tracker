FROM python:3.12-slim
WORKDIR /app
ENV PORT=5050 TZ=Asia/Shanghai PYTHONUNBUFFERED=1 AGENT_KNOWLEDGE_DIR=/app/frameworks
COPY tracker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tracker/ .
COPY ["AI超级周期轮动框架.md", "供应链瓶颈投资框架.md", "动量催化框架.md", "NAV折价套利框架.md", "推荐录入与执行闭环规则.md", "/app/frameworks/"]
EXPOSE 5050
CMD ["sh", "-c", "gunicorn --preload --bind 0.0.0.0:$PORT --workers 2 --timeout 120 wsgi:app"]
