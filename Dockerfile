# Используем легковесный образ Python
FROM python:3.11-slim

# Отключаем буферизацию логов (чтобы они сразу шли в консоль)
ENV PYTHONUNBUFFERED=1

# Устанавливаем системные зависимости для работы mysql-драйверов и Pillow
RUN apt-get update && apt-get install -y \
    pkg-config default-libmysqlclient-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код проекта
COPY . .

# Команда для запуска бота
CMD ["python", "main.py"]
