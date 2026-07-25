import asyncio
import json
import sys
from pathlib import Path

from ai_support_bot.app.db.database import AsyncSessionLocal, init_db
from ai_support_bot.app.services.rag_service import rag_service


async def main() -> None:
    if len(sys.argv) < 2:
        print("Использование: python -m ai_support_bot.ingest <путь_к_json_файлу>")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    if not file_path.exists():
        print(f"Ошибка: Файл {file_path} не найден!")
        sys.exit(1)

    print(f" Чтение файла: {file_path} (Размер: {file_path.stat().st_size / 1024 / 1024:.2f} МБ)...")
    with open(file_path, "rb") as f:
        raw_bytes = f.read()

    print(" Декодирование JSON...")
    parsed_data = json.loads(raw_bytes.decode("utf-8"))

    print(" Инициализация базы данных...")
    await init_db()

    print(" Обработка и генерация эмбеддингов ИИ (RAG)...")
    async with AsyncSessionLocal() as db:
        result = await rag_service.ingest_file(
            db=db,
            filename=file_path.name,
            raw_bytes=raw_bytes,
            parsed_data=parsed_data,
        )

    print("\n Импорт завершен!")
    print(f"Статус: {result.get('status')}")
    print(f"Создано чанков RAG: {result.get('chunk_count')}")
    print(f"Всего сообщений: {result.get('message_count')}")
    print(f"Пропущено дубликатов: {result.get('skipped_duplicates', 0)}")


if __name__ == "__main__":
    asyncio.run(main())
