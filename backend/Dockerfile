FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY parsers ./parsers
COPY model_serving ./model_serving
COPY run.py ./run.py

EXPOSE 8000

CMD ["python", "run.py"]
