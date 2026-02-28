# Используем легкую версию Python (версия 3.10 совпадает с твоей локальной)
FROM python:3.10-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем сам скрипт бота
COPY bot.py .

# Указываем команду для запуска
CMD ["python", "bot.py"]