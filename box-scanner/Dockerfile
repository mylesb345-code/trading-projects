FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# keys come from --env-file at run time; .env is never copied into the image (see .dockerignore)
CMD ["python", "main.py", "run"]
