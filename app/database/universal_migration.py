import logging
from datetime import datetime

from sqlalchemy import select, text

from app.config import settings
from app.database.database import AsyncSessionLocal, engine
from app.database.models import WebApiToken
from app.utils.security import hash_api_token


logger = logging.getLogger(__name__)


async def get_database_type():
    return engine.dialect.name


async def sync_postgres_sequences() -> bool:
    """Ensure PostgreSQL sequences match the current max values after restores."""

    db_type = await get_database_type()

    if db_type != 'postgresql':
        logger.debug('Пропускаем синхронизацию последовательностей: тип БД %s', db_type)
        return True

    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        cols.table_schema,
                        cols.table_name,
                        cols.column_name,
                        pg_get_serial_sequence(
                            format('%I.%I', cols.table_schema, cols.table_name),
                            cols.column_name
                        ) AS sequence_path
                    FROM information_schema.columns AS cols
                    WHERE cols.column_default LIKE 'nextval(%'
                      AND cols.table_schema NOT IN ('pg_catalog', 'information_schema')
                    """
                )
            )

            sequences = result.fetchall()

            if not sequences:
                logger.info('ℹ️ Не найдено последовательностей PostgreSQL для синхронизации')
                return True

            for table_schema, table_name, column_name, sequence_path in sequences:
                if not sequence_path:
                    continue

                max_result = await conn.execute(
                    text(f'SELECT COALESCE(MAX("{column_name}"), 0) FROM "{table_schema}"."{table_name}"')
                )
                max_value = max_result.scalar() or 0

                parts = sequence_path.split('.')
                if len(parts) == 2:
                    seq_schema, seq_name = parts
                else:
                    seq_schema, seq_name = 'public', parts[-1]

                seq_schema = seq_schema.strip('"')
                seq_name = seq_name.strip('"')
                current_result = await conn.execute(
                    text(f'SELECT last_value, is_called FROM "{seq_schema}"."{seq_name}"')
                )
                current_row = current_result.fetchone()

                if current_row:
                    current_last, is_called = current_row
                    current_next = current_last + 1 if is_called else current_last
                    if current_next > max_value:
                        continue

                await conn.execute(
                    text(
                        """
                        SELECT setval(:sequence_name, :new_value, TRUE)
                        """
                    ),
                    {'sequence_name': sequence_path, 'new_value': max_value},
                )
                logger.info(
                    '🔄 Последовательность %s синхронизирована: MAX=%s, следующий ID=%s',
                    sequence_path,
                    max_value,
                    max_value + 1,
                )

        return True

    except Exception as error:
        logger.error('❌ Ошибка синхронизации последовательностей PostgreSQL: %s', error)
        return False


async def check_table_exists(table_name: str) -> bool:
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                result = await conn.execute(
                    text(f"""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='{table_name}'
                """)
                )
                return result.fetchone() is not None

            if db_type == 'postgresql':
                result = await conn.execute(
                    text("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = :table_name
                """),
                    {'table_name': table_name},
                )
                return result.fetchone() is not None

            if db_type == 'mysql':
                result = await conn.execute(
                    text("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = DATABASE() AND table_name = :table_name
                """),
                    {'table_name': table_name},
                )
                return result.fetchone() is not None

            return False

    except Exception as e:
        logger.error(f'Ошибка проверки существования таблицы {table_name}: {e}')
        return False


async def check_column_exists(table_name: str, column_name: str) -> bool:
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                result = await conn.execute(text(f'PRAGMA table_info({table_name})'))
                columns = result.fetchall()
                return any(col[1] == column_name for col in columns)

            if db_type == 'postgresql':
                result = await conn.execute(
                    text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = :table_name
                    AND column_name = :column_name
                """),
                    {'table_name': table_name, 'column_name': column_name},
                )
                return result.fetchone() is not None

            if db_type == 'mysql':
                result = await conn.execute(
                    text("""
                    SELECT COLUMN_NAME
                    FROM information_schema.COLUMNS
                    WHERE TABLE_NAME = :table_name
                    AND COLUMN_NAME = :column_name
                """),
                    {'table_name': table_name, 'column_name': column_name},
                )
                return result.fetchone() is not None

            return False

    except Exception as e:
        logger.error(f'Ошибка проверки существования колонки {column_name}: {e}')
        return False


async def check_constraint_exists(table_name: str, constraint_name: str) -> bool:
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'postgresql':
                result = await conn.execute(
                    text(
                        """
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                      AND constraint_name = :constraint_name
                """
                    ),
                    {'table_name': table_name, 'constraint_name': constraint_name},
                )
                return result.fetchone() is not None

            if db_type == 'mysql':
                result = await conn.execute(
                    text(
                        """
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE table_schema = DATABASE()
                      AND table_name = :table_name
                      AND constraint_name = :constraint_name
                """
                    ),
                    {'table_name': table_name, 'constraint_name': constraint_name},
                )
                return result.fetchone() is not None

            if db_type == 'sqlite':
                result = await conn.execute(text(f'PRAGMA foreign_key_list({table_name})'))
                rows = result.fetchall()
                return any(row[5] == constraint_name for row in rows)

            return False

    except Exception as e:
        logger.error(f'Ошибка проверки существования ограничения {constraint_name} для {table_name}: {e}')
        return False


async def check_index_exists(table_name: str, index_name: str) -> bool:
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'postgresql':
                result = await conn.execute(
                    text(
                        """
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = :table_name
                      AND indexname = :index_name
                """
                    ),
                    {'table_name': table_name, 'index_name': index_name},
                )
                return result.fetchone() is not None

            if db_type == 'mysql':
                result = await conn.execute(
                    text(
                        """
                    SELECT 1
                    FROM information_schema.statistics
                    WHERE table_schema = DATABASE()
                      AND table_name = :table_name
                      AND index_name = :index_name
                """
                    ),
                    {'table_name': table_name, 'index_name': index_name},
                )
                return result.fetchone() is not None

            if db_type == 'sqlite':
                result = await conn.execute(text(f'PRAGMA index_list({table_name})'))
                rows = result.fetchall()
                return any(row[1] == index_name for row in rows)

            return False

    except Exception as e:
        logger.error(f'Ошибка проверки существования индекса {index_name} для {table_name}: {e}')
        return False


async def fetch_duplicate_payment_links(conn) -> list[tuple[str, int]]:
    result = await conn.execute(
        text(
            'SELECT payment_link_id, COUNT(*) AS cnt '
            'FROM wata_payments '
            "WHERE payment_link_id IS NOT NULL AND payment_link_id <> '' "
            'GROUP BY payment_link_id '
            'HAVING COUNT(*) > 1'
        )
    )
    return [(row[0], row[1]) for row in result.fetchall()]


def _build_dedup_suffix(base_suffix: str, record_id: int, max_length: int = 64) -> tuple[str, int]:
    suffix = f'{base_suffix}{record_id}'
    trimmed_length = max_length - len(suffix)
    if trimmed_length < 1:
        # Fallback: use the record id only to stay within the limit.
        suffix = f'dup-{record_id}'
        trimmed_length = max_length - len(suffix)
    return suffix, trimmed_length


async def resolve_duplicate_payment_links(conn, db_type: str) -> bool:
    duplicates = await fetch_duplicate_payment_links(conn)

    if not duplicates:
        return True

    logger.warning(
        'Найдены дубликаты payment_link_id в wata_payments: %s',
        ', '.join(f'{link}×{count}' for link, count in duplicates[:5]),
    )

    for payment_link_id, _ in duplicates:
        result = await conn.execute(
            text('SELECT id, payment_link_id FROM wata_payments WHERE payment_link_id = :payment_link_id ORDER BY id'),
            {'payment_link_id': payment_link_id},
        )

        rows = result.fetchall()

        if not rows:
            continue

        # Skip the first occurrence to preserve the original link value.
        for duplicate_row in rows[1:]:
            record_id = duplicate_row[0]
            original_link = duplicate_row[1] or ''
            suffix, trimmed_length = _build_dedup_suffix('-dup-', record_id)
            new_base = original_link[:trimmed_length] if trimmed_length > 0 else ''
            new_link = f'{new_base}{suffix}' if new_base else suffix

            await conn.execute(
                text('UPDATE wata_payments SET payment_link_id = :new_link WHERE id = :record_id'),
                {'new_link': new_link, 'record_id': record_id},
            )

    remaining_duplicates = await fetch_duplicate_payment_links(conn)

    if remaining_duplicates:
        logger.error(
            'Не удалось устранить дубликаты payment_link_id: %s',
            ', '.join(f'{link}×{count}' for link, count in remaining_duplicates[:5]),
        )
        return False

    logger.info('✅ Дубликаты payment_link_id устранены')
    return True


async def enforce_wata_payment_link_constraints(
    conn,
    db_type: str,
    unique_index_exists: bool,
    legacy_index_exists: bool,
) -> tuple[bool, bool]:
    try:
        if db_type == 'sqlite':
            await conn.execute(
                text(
                    'UPDATE wata_payments '
                    "SET payment_link_id = 'legacy-' || id "
                    "WHERE payment_link_id IS NULL OR payment_link_id = ''"
                )
            )

            if not await resolve_duplicate_payment_links(conn, db_type):
                return unique_index_exists, legacy_index_exists

            if not unique_index_exists:
                await conn.execute(
                    text('CREATE UNIQUE INDEX IF NOT EXISTS uq_wata_payment_link ON wata_payments(payment_link_id)')
                )
                logger.info('✅ Создан уникальный индекс uq_wata_payment_link для payment_link_id')
                unique_index_exists = True
            else:
                logger.info('ℹ️ Уникальный индекс для payment_link_id уже существует')

            if legacy_index_exists and unique_index_exists:
                await conn.execute(text('DROP INDEX IF EXISTS idx_wata_link_id'))
                logger.info('ℹ️ Удалён устаревший индекс idx_wata_link_id')
                legacy_index_exists = False

            return unique_index_exists, legacy_index_exists

        if db_type == 'postgresql':
            await conn.execute(
                text(
                    'UPDATE wata_payments '
                    "SET payment_link_id = 'legacy-' || id::text "
                    "WHERE payment_link_id IS NULL OR payment_link_id = ''"
                )
            )

            await conn.execute(text('ALTER TABLE wata_payments ALTER COLUMN payment_link_id SET NOT NULL'))
            logger.info('✅ Колонка payment_link_id теперь NOT NULL')

            if not await resolve_duplicate_payment_links(conn, db_type):
                return unique_index_exists, legacy_index_exists

            if not unique_index_exists:
                await conn.execute(
                    text('CREATE UNIQUE INDEX IF NOT EXISTS uq_wata_payment_link ON wata_payments(payment_link_id)')
                )
                logger.info('✅ Создан уникальный индекс uq_wata_payment_link для payment_link_id')
                unique_index_exists = True
            else:
                logger.info('ℹ️ Уникальный индекс для payment_link_id уже существует')

            if legacy_index_exists and unique_index_exists:
                await conn.execute(text('DROP INDEX IF EXISTS idx_wata_link_id'))
                logger.info('ℹ️ Удалён устаревший индекс idx_wata_link_id')
                legacy_index_exists = False

            return unique_index_exists, legacy_index_exists

        if db_type == 'mysql':
            await conn.execute(
                text(
                    'UPDATE wata_payments '
                    "SET payment_link_id = CONCAT('legacy-', id) "
                    "WHERE payment_link_id IS NULL OR payment_link_id = ''"
                )
            )

            await conn.execute(text('ALTER TABLE wata_payments MODIFY COLUMN payment_link_id VARCHAR(64) NOT NULL'))
            logger.info('✅ Колонка payment_link_id теперь NOT NULL')

            if not await resolve_duplicate_payment_links(conn, db_type):
                return unique_index_exists, legacy_index_exists

            if not unique_index_exists:
                await conn.execute(text('CREATE UNIQUE INDEX uq_wata_payment_link ON wata_payments(payment_link_id)'))
                logger.info('✅ Создан уникальный индекс uq_wata_payment_link для payment_link_id')
                unique_index_exists = True
            else:
                logger.info('ℹ️ Уникальный индекс для payment_link_id уже существует')

            if legacy_index_exists and unique_index_exists:
                await conn.execute(text('DROP INDEX idx_wata_link_id ON wata_payments'))
                logger.info('ℹ️ Удалён устаревший индекс idx_wata_link_id')
                legacy_index_exists = False

            return unique_index_exists, legacy_index_exists

        logger.warning('⚠️ Неизвестный тип БД %s — не удалось усилить ограничения payment_link_id', db_type)
        return unique_index_exists, legacy_index_exists

    except Exception as e:
        logger.error(f'Ошибка настройки ограничений payment_link_id: {e}')
        return unique_index_exists, legacy_index_exists


async def create_cryptobot_payments_table():
    table_exists = await check_table_exists('cryptobot_payments')
    if table_exists:
        logger.info('Таблица cryptobot_payments уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE cryptobot_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    invoice_id VARCHAR(255) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    asset VARCHAR(10) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    description TEXT NULL,
                    payload TEXT NULL,
                    bot_invoice_url TEXT NULL,
                    mini_app_invoice_url TEXT NULL,
                    web_app_invoice_url TEXT NULL,
                    paid_at DATETIME NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_cryptobot_payments_user_id ON cryptobot_payments(user_id);
                CREATE INDEX idx_cryptobot_payments_invoice_id ON cryptobot_payments(invoice_id);
                CREATE INDEX idx_cryptobot_payments_status ON cryptobot_payments(status);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE cryptobot_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    invoice_id VARCHAR(255) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    asset VARCHAR(10) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    description TEXT NULL,
                    payload TEXT NULL,
                    bot_invoice_url TEXT NULL,
                    mini_app_invoice_url TEXT NULL,
                    web_app_invoice_url TEXT NULL,
                    paid_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_cryptobot_payments_user_id ON cryptobot_payments(user_id);
                CREATE INDEX idx_cryptobot_payments_invoice_id ON cryptobot_payments(invoice_id);
                CREATE INDEX idx_cryptobot_payments_status ON cryptobot_payments(status);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE cryptobot_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    invoice_id VARCHAR(255) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    asset VARCHAR(10) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    description TEXT NULL,
                    payload TEXT NULL,
                    bot_invoice_url TEXT NULL,
                    mini_app_invoice_url TEXT NULL,
                    web_app_invoice_url TEXT NULL,
                    paid_at DATETIME NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_cryptobot_payments_user_id ON cryptobot_payments(user_id);
                CREATE INDEX idx_cryptobot_payments_invoice_id ON cryptobot_payments(invoice_id);
                CREATE INDEX idx_cryptobot_payments_status ON cryptobot_payments(status);
                """
            else:
                logger.error(f'Неподдерживаемый тип БД для создания таблицы: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('Таблица cryptobot_payments успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы cryptobot_payments: {e}')
        return False


async def create_heleket_payments_table():
    table_exists = await check_table_exists('heleket_payments')
    if table_exists:
        logger.info('Таблица heleket_payments уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE heleket_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    uuid VARCHAR(255) UNIQUE NOT NULL,
                    order_id VARCHAR(128) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    currency VARCHAR(10) NOT NULL,
                    payer_amount VARCHAR(50) NULL,
                    payer_currency VARCHAR(10) NULL,
                    exchange_rate DOUBLE PRECISION NULL,
                    discount_percent INTEGER NULL,
                    status VARCHAR(50) NOT NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    paid_at DATETIME NULL,
                    expires_at DATETIME NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_heleket_payments_user_id ON heleket_payments(user_id);
                CREATE INDEX idx_heleket_payments_uuid ON heleket_payments(uuid);
                CREATE INDEX idx_heleket_payments_order_id ON heleket_payments(order_id);
                CREATE INDEX idx_heleket_payments_status ON heleket_payments(status);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE heleket_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    uuid VARCHAR(255) UNIQUE NOT NULL,
                    order_id VARCHAR(128) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    currency VARCHAR(10) NOT NULL,
                    payer_amount VARCHAR(50) NULL,
                    payer_currency VARCHAR(10) NULL,
                    exchange_rate DOUBLE PRECISION NULL,
                    discount_percent INTEGER NULL,
                    status VARCHAR(50) NOT NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    paid_at TIMESTAMP NULL,
                    expires_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX idx_heleket_payments_user_id ON heleket_payments(user_id);
                CREATE INDEX idx_heleket_payments_uuid ON heleket_payments(uuid);
                CREATE INDEX idx_heleket_payments_order_id ON heleket_payments(order_id);
                CREATE INDEX idx_heleket_payments_status ON heleket_payments(status);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE heleket_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    uuid VARCHAR(255) UNIQUE NOT NULL,
                    order_id VARCHAR(128) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    currency VARCHAR(10) NOT NULL,
                    payer_amount VARCHAR(50) NULL,
                    payer_currency VARCHAR(10) NULL,
                    exchange_rate DOUBLE NULL,
                    discount_percent INT NULL,
                    status VARCHAR(50) NOT NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    paid_at DATETIME NULL,
                    expires_at DATETIME NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_heleket_payments_user_id ON heleket_payments(user_id);
                CREATE INDEX idx_heleket_payments_uuid ON heleket_payments(uuid);
                CREATE INDEX idx_heleket_payments_order_id ON heleket_payments(order_id);
                CREATE INDEX idx_heleket_payments_status ON heleket_payments(status);
                """

            else:
                logger.error(f'Неподдерживаемый тип БД для таблицы heleket_payments: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('Таблица heleket_payments успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы heleket_payments: {e}')
        return False


async def create_mulenpay_payments_table():
    table_exists = await check_table_exists('mulenpay_payments')
    if table_exists:
        logger.info('Таблица mulenpay_payments уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE mulenpay_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    mulen_payment_id INTEGER NULL,
                    uuid VARCHAR(255) NOT NULL UNIQUE,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'created',
                    is_paid BOOLEAN DEFAULT 0,
                    paid_at DATETIME NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_mulenpay_uuid ON mulenpay_payments(uuid);
                CREATE INDEX idx_mulenpay_payment_id ON mulenpay_payments(mulen_payment_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE mulenpay_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    mulen_payment_id INTEGER NULL,
                    uuid VARCHAR(255) NOT NULL UNIQUE,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'created',
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    paid_at TIMESTAMP NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX idx_mulenpay_uuid ON mulenpay_payments(uuid);
                CREATE INDEX idx_mulenpay_payment_id ON mulenpay_payments(mulen_payment_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE mulenpay_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    mulen_payment_id INT NULL,
                    uuid VARCHAR(255) NOT NULL UNIQUE,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'created',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_mulenpay_uuid ON mulenpay_payments(uuid);
                CREATE INDEX idx_mulenpay_payment_id ON mulenpay_payments(mulen_payment_id);
                """

            else:
                logger.error(f'Неподдерживаемый тип БД для таблицы mulenpay_payments: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('Таблица mulenpay_payments успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы mulenpay_payments: {e}')
        return False


async def ensure_mulenpay_payment_schema() -> bool:
    logger.info('=== ОБНОВЛЕНИЕ СХЕМЫ MULEN PAY ===')

    table_exists = await check_table_exists('mulenpay_payments')
    if not table_exists:
        logger.warning('⚠️ Таблица mulenpay_payments отсутствует — создаём заново')
        return await create_mulenpay_payments_table()

    try:
        column_exists = await check_column_exists('mulenpay_payments', 'mulen_payment_id')
        paid_at_column_exists = await check_column_exists('mulenpay_payments', 'paid_at')
        index_exists = await check_index_exists('mulenpay_payments', 'idx_mulenpay_payment_id')

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not column_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    alter_sql = 'ALTER TABLE mulenpay_payments ADD COLUMN mulen_payment_id INTEGER NULL'
                elif db_type == 'mysql':
                    alter_sql = 'ALTER TABLE mulenpay_payments ADD COLUMN mulen_payment_id INT NULL'
                else:
                    logger.error(
                        'Неподдерживаемый тип БД для добавления mulen_payment_id в mulenpay_payments: %s',
                        db_type,
                    )
                    return False

                await conn.execute(text(alter_sql))
                logger.info('✅ Добавлена колонка mulenpay_payments.mulen_payment_id')
            else:
                logger.info('ℹ️ Колонка mulenpay_payments.mulen_payment_id уже существует')

            if not paid_at_column_exists:
                if db_type == 'sqlite':
                    alter_paid_at_sql = 'ALTER TABLE mulenpay_payments ADD COLUMN paid_at DATETIME NULL'
                elif db_type == 'postgresql':
                    alter_paid_at_sql = 'ALTER TABLE mulenpay_payments ADD COLUMN paid_at TIMESTAMP NULL'
                elif db_type == 'mysql':
                    alter_paid_at_sql = 'ALTER TABLE mulenpay_payments ADD COLUMN paid_at DATETIME NULL'
                else:
                    logger.error(
                        'Неподдерживаемый тип БД для добавления paid_at в mulenpay_payments: %s',
                        db_type,
                    )
                    return False

                await conn.execute(text(alter_paid_at_sql))
                logger.info('✅ Добавлена колонка mulenpay_payments.paid_at')
            else:
                logger.info('ℹ️ Колонка mulenpay_payments.paid_at уже существует')

            if not index_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    create_index_sql = (
                        'CREATE INDEX IF NOT EXISTS idx_mulenpay_payment_id ON mulenpay_payments(mulen_payment_id)'
                    )
                elif db_type == 'mysql':
                    create_index_sql = 'CREATE INDEX idx_mulenpay_payment_id ON mulenpay_payments(mulen_payment_id)'
                else:
                    logger.error(
                        'Неподдерживаемый тип БД для создания индекса mulenpay_payment_id: %s',
                        db_type,
                    )
                    return False

                await conn.execute(text(create_index_sql))
                logger.info('✅ Создан индекс idx_mulenpay_payment_id')
            else:
                logger.info('ℹ️ Индекс idx_mulenpay_payment_id уже существует')

        return True

    except Exception as e:
        logger.error(f'Ошибка обновления схемы mulenpay_payments: {e}')
        return False


async def create_pal24_payments_table():
    table_exists = await check_table_exists('pal24_payments')
    if table_exists:
        logger.info('Таблица pal24_payments уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE pal24_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    bill_id VARCHAR(255) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(20) NOT NULL DEFAULT 'normal',
                    status VARCHAR(50) NOT NULL DEFAULT 'NEW',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    last_status VARCHAR(50) NULL,
                    last_status_checked_at DATETIME NULL,
                    link_url TEXT NULL,
                    link_page_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    payment_id VARCHAR(255) NULL,
                    payment_status VARCHAR(50) NULL,
                    payment_method VARCHAR(50) NULL,
                    balance_amount VARCHAR(50) NULL,
                    balance_currency VARCHAR(10) NULL,
                    payer_account VARCHAR(255) NULL,
                    ttl INTEGER NULL,
                    expires_at DATETIME NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_pal24_bill_id ON pal24_payments(bill_id);
                CREATE INDEX idx_pal24_order_id ON pal24_payments(order_id);
                CREATE INDEX idx_pal24_payment_id ON pal24_payments(payment_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE pal24_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    bill_id VARCHAR(255) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(20) NOT NULL DEFAULT 'normal',
                    status VARCHAR(50) NOT NULL DEFAULT 'NEW',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    paid_at TIMESTAMP NULL,
                    last_status VARCHAR(50) NULL,
                    last_status_checked_at TIMESTAMP NULL,
                    link_url TEXT NULL,
                    link_page_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    payment_id VARCHAR(255) NULL,
                    payment_status VARCHAR(50) NULL,
                    payment_method VARCHAR(50) NULL,
                    balance_amount VARCHAR(50) NULL,
                    balance_currency VARCHAR(10) NULL,
                    payer_account VARCHAR(255) NULL,
                    ttl INTEGER NULL,
                    expires_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX idx_pal24_bill_id ON pal24_payments(bill_id);
                CREATE INDEX idx_pal24_order_id ON pal24_payments(order_id);
                CREATE INDEX idx_pal24_payment_id ON pal24_payments(payment_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE pal24_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    bill_id VARCHAR(255) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(20) NOT NULL DEFAULT 'normal',
                    status VARCHAR(50) NOT NULL DEFAULT 'NEW',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    last_status VARCHAR(50) NULL,
                    last_status_checked_at DATETIME NULL,
                    link_url TEXT NULL,
                    link_page_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    payment_id VARCHAR(255) NULL,
                    payment_status VARCHAR(50) NULL,
                    payment_method VARCHAR(50) NULL,
                    balance_amount VARCHAR(50) NULL,
                    balance_currency VARCHAR(10) NULL,
                    payer_account VARCHAR(255) NULL,
                    ttl INT NULL,
                    expires_at DATETIME NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_pal24_bill_id ON pal24_payments(bill_id);
                CREATE INDEX idx_pal24_order_id ON pal24_payments(order_id);
                CREATE INDEX idx_pal24_payment_id ON pal24_payments(payment_id);
                """

            else:
                logger.error(f'Неподдерживаемый тип БД для таблицы pal24_payments: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('Таблица pal24_payments успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы pal24_payments: {e}')
        return False


async def create_wata_payments_table():
    table_exists = await check_table_exists('wata_payments')
    if table_exists:
        logger.info('Таблица wata_payments уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE wata_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    payment_link_id VARCHAR(64) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(50) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'Opened',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    last_status VARCHAR(50) NULL,
                    terminal_public_id VARCHAR(64) NULL,
                    url TEXT NULL,
                    success_redirect_url TEXT NULL,
                    fail_redirect_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    expires_at DATETIME NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE UNIQUE INDEX idx_wata_link_id ON wata_payments(payment_link_id);
                CREATE INDEX idx_wata_order_id ON wata_payments(order_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE wata_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    payment_link_id VARCHAR(64) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(50) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'Opened',
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    paid_at TIMESTAMP NULL,
                    last_status VARCHAR(50) NULL,
                    terminal_public_id VARCHAR(64) NULL,
                    url TEXT NULL,
                    success_redirect_url TEXT NULL,
                    fail_redirect_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    expires_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX idx_wata_link_id ON wata_payments(payment_link_id);
                CREATE INDEX idx_wata_order_id ON wata_payments(order_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE wata_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    payment_link_id VARCHAR(64) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(50) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'Opened',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    last_status VARCHAR(50) NULL,
                    terminal_public_id VARCHAR(64) NULL,
                    url TEXT NULL,
                    success_redirect_url TEXT NULL,
                    fail_redirect_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    expires_at DATETIME NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE UNIQUE INDEX idx_wata_link_id ON wata_payments(payment_link_id);
                CREATE INDEX idx_wata_order_id ON wata_payments(order_id);
                """

            else:
                logger.error(f'Неподдерживаемый тип БД для таблицы wata_payments: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('Таблица wata_payments успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы wata_payments: {e}')
        return False


async def ensure_wata_payment_schema() -> bool:
    try:
        table_exists = await check_table_exists('wata_payments')
        if not table_exists:
            logger.warning('⚠️ Таблица wata_payments отсутствует — создаём заново')
            return await create_wata_payments_table()

        db_type = await get_database_type()

        legacy_link_index_exists = await check_index_exists('wata_payments', 'idx_wata_link_id')
        unique_link_index_exists = await check_index_exists('wata_payments', 'uq_wata_payment_link')
        builtin_unique_index_exists = await check_index_exists('wata_payments', 'wata_payments_payment_link_id_key')
        sqlite_auto_unique_exists = (
            await check_index_exists('wata_payments', 'sqlite_autoindex_wata_payments_1')
            if db_type == 'sqlite'
            else False
        )
        order_index_exists = await check_index_exists('wata_payments', 'idx_wata_order_id')

        payment_link_column_exists = await check_column_exists('wata_payments', 'payment_link_id')
        order_id_column_exists = await check_column_exists('wata_payments', 'order_id')

        unique_index_exists = unique_link_index_exists or builtin_unique_index_exists or sqlite_auto_unique_exists

        async with engine.begin() as conn:
            if not payment_link_column_exists:
                if db_type == 'sqlite':
                    await conn.execute(
                        text("ALTER TABLE wata_payments ADD COLUMN payment_link_id VARCHAR(64) NOT NULL DEFAULT ''")
                    )
                    payment_link_column_exists = True
                    unique_index_exists = False
                elif db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE wata_payments ADD COLUMN IF NOT EXISTS payment_link_id VARCHAR(64)')
                    )
                    payment_link_column_exists = True
                elif db_type == 'mysql':
                    await conn.execute(text('ALTER TABLE wata_payments ADD COLUMN payment_link_id VARCHAR(64)'))
                    payment_link_column_exists = True
                else:
                    logger.warning(
                        '⚠️ Неизвестный тип БД %s — пропущено добавление payment_link_id',
                        db_type,
                    )

                if payment_link_column_exists:
                    logger.info('✅ Добавлена колонка payment_link_id в wata_payments')

            if payment_link_column_exists:
                unique_index_exists, legacy_link_index_exists = await enforce_wata_payment_link_constraints(
                    conn,
                    db_type,
                    unique_index_exists,
                    legacy_link_index_exists,
                )

            if not order_id_column_exists:
                if db_type == 'sqlite':
                    await conn.execute(text('ALTER TABLE wata_payments ADD COLUMN order_id VARCHAR(255)'))
                    order_id_column_exists = True
                elif db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE wata_payments ADD COLUMN IF NOT EXISTS order_id VARCHAR(255)'))
                    order_id_column_exists = True
                elif db_type == 'mysql':
                    await conn.execute(text('ALTER TABLE wata_payments ADD COLUMN order_id VARCHAR(255)'))
                    order_id_column_exists = True
                else:
                    logger.warning(
                        '⚠️ Неизвестный тип БД %s — пропущено добавление order_id',
                        db_type,
                    )

                if order_id_column_exists:
                    logger.info('✅ Добавлена колонка order_id в wata_payments')

            if not order_index_exists:
                if not order_id_column_exists:
                    logger.warning('⚠️ Пропущено создание индекса idx_wata_order_id — колонка order_id отсутствует')
                else:
                    index_created = False
                    if db_type in {'sqlite', 'postgresql'}:
                        await conn.execute(
                            text('CREATE INDEX IF NOT EXISTS idx_wata_order_id ON wata_payments(order_id)')
                        )
                        index_created = True
                    elif db_type == 'mysql':
                        await conn.execute(text('CREATE INDEX idx_wata_order_id ON wata_payments(order_id)'))
                        index_created = True
                    else:
                        logger.warning(
                            '⚠️ Неизвестный тип БД %s — пропущено создание индекса idx_wata_order_id',
                            db_type,
                        )

                    if index_created:
                        logger.info('✅ Создан индекс idx_wata_order_id')
            else:
                logger.info('ℹ️ Индекс idx_wata_order_id уже существует')

        return True

    except Exception as e:
        logger.error(f'Ошибка обновления схемы wata_payments: {e}')
        return False


async def create_freekassa_payments_table():
    """Создаёт таблицу freekassa_payments для платежей через Freekassa."""
    table_exists = await check_table_exists('freekassa_payments')
    if table_exists:
        logger.info('Таблица freekassa_payments уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE freekassa_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    order_id VARCHAR(64) NOT NULL UNIQUE,
                    freekassa_order_id VARCHAR(64) NULL UNIQUE,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    payment_url TEXT NULL,
                    payment_system_id INTEGER NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    paid_at DATETIME NULL,
                    expires_at DATETIME NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    transaction_id INTEGER NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_freekassa_user_id ON freekassa_payments(user_id);
                CREATE UNIQUE INDEX idx_freekassa_order_id ON freekassa_payments(order_id);
                CREATE UNIQUE INDEX idx_freekassa_fk_order_id ON freekassa_payments(freekassa_order_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE freekassa_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    order_id VARCHAR(64) NOT NULL UNIQUE,
                    freekassa_order_id VARCHAR(64) NULL UNIQUE,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    payment_url TEXT NULL,
                    payment_system_id INTEGER NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    paid_at TIMESTAMP NULL,
                    expires_at TIMESTAMP NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    transaction_id INTEGER NULL REFERENCES transactions(id)
                );

                CREATE INDEX idx_freekassa_user_id ON freekassa_payments(user_id);
                CREATE UNIQUE INDEX idx_freekassa_order_id ON freekassa_payments(order_id);
                CREATE UNIQUE INDEX idx_freekassa_fk_order_id ON freekassa_payments(freekassa_order_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE freekassa_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    order_id VARCHAR(64) NOT NULL UNIQUE,
                    freekassa_order_id VARCHAR(64) NULL UNIQUE,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    payment_url TEXT NULL,
                    payment_system_id INT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    paid_at DATETIME NULL,
                    expires_at DATETIME NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    transaction_id INT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_freekassa_user_id ON freekassa_payments(user_id);
                CREATE UNIQUE INDEX idx_freekassa_order_id ON freekassa_payments(order_id);
                CREATE UNIQUE INDEX idx_freekassa_fk_order_id ON freekassa_payments(freekassa_order_id);
                """

            else:
                logger.error(f'Неподдерживаемый тип БД для таблицы freekassa_payments: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('Таблица freekassa_payments успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы freekassa_payments: {e}')
        return False


async def create_kassa_ai_payments_table():
    """Создаёт таблицу kassa_ai_payments для платежей через KassaAI."""
    table_exists = await check_table_exists('kassa_ai_payments')
    if table_exists:
        logger.info('Таблица kassa_ai_payments уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE kassa_ai_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    order_id VARCHAR(64) NOT NULL UNIQUE,
                    kassa_ai_order_id VARCHAR(64) NULL UNIQUE,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    payment_url TEXT NULL,
                    payment_system_id INTEGER NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    paid_at DATETIME NULL,
                    expires_at DATETIME NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    transaction_id INTEGER NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_kassa_ai_user_id ON kassa_ai_payments(user_id);
                CREATE UNIQUE INDEX idx_kassa_ai_order_id ON kassa_ai_payments(order_id);
                CREATE UNIQUE INDEX idx_kassa_ai_kai_order_id ON kassa_ai_payments(kassa_ai_order_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE kassa_ai_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    order_id VARCHAR(64) NOT NULL UNIQUE,
                    kassa_ai_order_id VARCHAR(64) NULL UNIQUE,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    payment_url TEXT NULL,
                    payment_system_id INTEGER NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    paid_at TIMESTAMP NULL,
                    expires_at TIMESTAMP NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    transaction_id INTEGER NULL REFERENCES transactions(id)
                );

                CREATE INDEX idx_kassa_ai_user_id ON kassa_ai_payments(user_id);
                CREATE UNIQUE INDEX idx_kassa_ai_order_id ON kassa_ai_payments(order_id);
                CREATE UNIQUE INDEX idx_kassa_ai_kai_order_id ON kassa_ai_payments(kassa_ai_order_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE kassa_ai_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    order_id VARCHAR(64) NOT NULL UNIQUE,
                    kassa_ai_order_id VARCHAR(64) NULL UNIQUE,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    payment_url TEXT NULL,
                    payment_system_id INT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    paid_at DATETIME NULL,
                    expires_at DATETIME NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    transaction_id INT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_kassa_ai_user_id ON kassa_ai_payments(user_id);
                CREATE UNIQUE INDEX idx_kassa_ai_order_id ON kassa_ai_payments(order_id);
                CREATE UNIQUE INDEX idx_kassa_ai_kai_order_id ON kassa_ai_payments(kassa_ai_order_id);
                """

            else:
                logger.error(f'Неподдерживаемый тип БД для таблицы kassa_ai_payments: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('Таблица kassa_ai_payments успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы kassa_ai_payments: {e}')
        return False


async def create_discount_offers_table():
    table_exists = await check_table_exists('discount_offers')
    if table_exists:
        logger.info('Таблица discount_offers уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                    CREATE TABLE discount_offers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        subscription_id INTEGER NULL,
                        notification_type VARCHAR(50) NOT NULL,
                        discount_percent INTEGER NOT NULL DEFAULT 0,
                        bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        expires_at DATETIME NOT NULL,
                        claimed_at DATETIME NULL,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount',
                        extra_data TEXT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
                    )
                """)
                )
                await conn.execute(
                    text("""
                    CREATE INDEX IF NOT EXISTS ix_discount_offers_user_type
                    ON discount_offers (user_id, notification_type)
                """)
                )

            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                    CREATE TABLE IF NOT EXISTS discount_offers (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        subscription_id INTEGER NULL REFERENCES subscriptions(id) ON DELETE SET NULL,
                        notification_type VARCHAR(50) NOT NULL,
                        discount_percent INTEGER NOT NULL DEFAULT 0,
                        bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        expires_at TIMESTAMP NOT NULL,
                        claimed_at TIMESTAMP NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount',
                        extra_data JSON NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                )
                await conn.execute(
                    text("""
                    CREATE INDEX IF NOT EXISTS ix_discount_offers_user_type
                    ON discount_offers (user_id, notification_type)
                """)
                )

            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                    CREATE TABLE IF NOT EXISTS discount_offers (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        user_id INTEGER NOT NULL,
                        subscription_id INTEGER NULL,
                        notification_type VARCHAR(50) NOT NULL,
                        discount_percent INTEGER NOT NULL DEFAULT 0,
                        bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        expires_at DATETIME NOT NULL,
                        claimed_at DATETIME NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount',
                        extra_data JSON NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        CONSTRAINT fk_discount_offers_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        CONSTRAINT fk_discount_offers_subscription FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
                    )
                """)
                )
                await conn.execute(
                    text("""
                    CREATE INDEX ix_discount_offers_user_type
                    ON discount_offers (user_id, notification_type)
                """)
                )

            else:
                raise ValueError(f'Unsupported database type: {db_type}')

        logger.info('✅ Таблица discount_offers успешно создана')
        return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы discount_offers: {e}')
        return False


async def create_referral_contests_table() -> bool:
    table_exists = await check_table_exists('referral_contests')
    if table_exists:
        logger.info('Таблица referral_contests уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title VARCHAR(255) NOT NULL,
                        description TEXT NULL,
                        prize_text TEXT NULL,
                        contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid',
                        start_at DATETIME NOT NULL,
                        end_at DATETIME NOT NULL,
                        daily_summary_time TIME NOT NULL DEFAULT '12:00:00',
                        daily_summary_times VARCHAR(255) NULL,
                        timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        last_daily_summary_date DATE NULL,
                        last_daily_summary_at DATETIME NULL,
                        final_summary_sent BOOLEAN NOT NULL DEFAULT 0,
                        created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contests (
                        id SERIAL PRIMARY KEY,
                        title VARCHAR(255) NOT NULL,
                        description TEXT NULL,
                        prize_text TEXT NULL,
                        contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid',
                        start_at TIMESTAMP NOT NULL,
                        end_at TIMESTAMP NOT NULL,
                        daily_summary_time TIME NOT NULL DEFAULT '12:00:00',
                        daily_summary_times VARCHAR(255) NULL,
                        timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        last_daily_summary_date DATE NULL,
                        last_daily_summary_at TIMESTAMP NULL,
                        final_summary_sent BOOLEAN NOT NULL DEFAULT FALSE,
                        created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contests (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        title VARCHAR(255) NOT NULL,
                        description TEXT NULL,
                        prize_text TEXT NULL,
                        contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid',
                        start_at DATETIME NOT NULL,
                        end_at DATETIME NOT NULL,
                        daily_summary_time TIME NOT NULL DEFAULT '12:00:00',
                        daily_summary_times VARCHAR(255) NULL,
                        timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        last_daily_summary_date DATE NULL,
                        last_daily_summary_at DATETIME NULL,
                        final_summary_sent BOOLEAN NOT NULL DEFAULT FALSE,
                        created_by INTEGER NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        CONSTRAINT fk_referral_contest_creator FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                    )
                """)
                )
            else:
                raise ValueError(f'Unsupported database type: {db_type}')

        logger.info('✅ Таблица referral_contests создана')
        return True
    except Exception as error:
        logger.error(f'Ошибка создания таблицы referral_contests: {error}')
        return False


async def create_referral_contest_events_table() -> bool:
    table_exists = await check_table_exists('referral_contest_events')
    if table_exists:
        logger.info('Таблица referral_contest_events уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contest_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        contest_id INTEGER NOT NULL,
                        referrer_id INTEGER NOT NULL,
                        referral_id INTEGER NOT NULL,
                        event_type VARCHAR(50) NOT NULL,
                        amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(contest_id) REFERENCES referral_contests(id) ON DELETE CASCADE,
                        FOREIGN KEY(referrer_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(referral_id) REFERENCES users(id) ON DELETE CASCADE,
                        UNIQUE(contest_id, referral_id)
                    )
                """)
                )
                await conn.execute(
                    text("""
                    CREATE INDEX IF NOT EXISTS idx_referral_contest_referrer
                    ON referral_contest_events (contest_id, referrer_id)
                """)
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contest_events (
                        id SERIAL PRIMARY KEY,
                        contest_id INTEGER NOT NULL REFERENCES referral_contests(id) ON DELETE CASCADE,
                        referrer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        referral_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        event_type VARCHAR(50) NOT NULL,
                        amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_referral_contest_referral UNIQUE (contest_id, referral_id)
                    )
                """)
                )
                await conn.execute(
                    text("""
                    CREATE INDEX IF NOT EXISTS idx_referral_contest_referrer
                    ON referral_contest_events (contest_id, referrer_id)
                """)
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contest_events (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        contest_id INTEGER NOT NULL,
                        referrer_id INTEGER NOT NULL,
                        referral_id INTEGER NOT NULL,
                        event_type VARCHAR(50) NOT NULL,
                        amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_referral_contest FOREIGN KEY(contest_id) REFERENCES referral_contests(id) ON DELETE CASCADE,
                        CONSTRAINT fk_referral_contest_referrer FOREIGN KEY(referrer_id) REFERENCES users(id) ON DELETE CASCADE,
                        CONSTRAINT fk_referral_contest_referral FOREIGN KEY(referral_id) REFERENCES users(id) ON DELETE CASCADE,
                        CONSTRAINT uq_referral_contest_referral UNIQUE (contest_id, referral_id)
                    )
                """)
                )
                await conn.execute(
                    text("""
                    CREATE INDEX idx_referral_contest_referrer
                    ON referral_contest_events (contest_id, referrer_id)
                """)
                )
            else:
                raise ValueError(f'Unsupported database type: {db_type}')

        logger.info('✅ Таблица referral_contest_events создана')
        return True
    except Exception as error:
        logger.error(f'Ошибка создания таблицы referral_contest_events: {error}')
    return False


async def create_referral_contest_virtual_participants_table() -> bool:
    table_exists = await check_table_exists('referral_contest_virtual_participants')
    if table_exists:
        logger.info('Таблица referral_contest_virtual_participants уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contest_virtual_participants (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        contest_id INTEGER NOT NULL,
                        display_name VARCHAR(255) NOT NULL,
                        referral_count INTEGER NOT NULL DEFAULT 0,
                        total_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(contest_id) REFERENCES referral_contests(id) ON DELETE CASCADE
                    )
                """)
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contest_virtual_participants (
                        id SERIAL PRIMARY KEY,
                        contest_id INTEGER NOT NULL REFERENCES referral_contests(id) ON DELETE CASCADE,
                        display_name VARCHAR(255) NOT NULL,
                        referral_count INTEGER NOT NULL DEFAULT 0,
                        total_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                )
            else:
                await conn.execute(
                    text("""
                    CREATE TABLE referral_contest_virtual_participants (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        contest_id INT NOT NULL,
                        display_name VARCHAR(255) NOT NULL,
                        referral_count INT NOT NULL DEFAULT 0,
                        total_amount_kopeks INT NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(contest_id) REFERENCES referral_contests(id) ON DELETE CASCADE
                    )
                """)
                )

        logger.info('✅ Таблица referral_contest_virtual_participants создана')
        return True
    except Exception as error:
        logger.error(f'Ошибка создания таблицы referral_contest_virtual_participants: {error}')
    return False


async def ensure_referral_contest_summary_columns() -> bool:
    ok = True
    for column in ['daily_summary_times', 'last_daily_summary_at']:
        exists = await check_column_exists('referral_contests', column)
        if exists:
            logger.info('Колонка %s в referral_contests уже существует', column)
            continue
        try:
            async with engine.begin() as conn:
                db_type = await get_database_type()
                if db_type == 'postgresql':
                    await conn.execute(
                        text(
                            f'ALTER TABLE referral_contests ADD COLUMN {column} '
                            + ('VARCHAR(255)' if column == 'daily_summary_times' else 'TIMESTAMP')
                        )
                    )
                else:
                    await conn.execute(
                        text(
                            f'ALTER TABLE referral_contests ADD COLUMN {column} '
                            + ('VARCHAR(255)' if column == 'daily_summary_times' else 'DATETIME')
                        )
                    )
            logger.info('✅ Колонка %s в referral_contests добавлена', column)
        except Exception as error:
            ok = False
            logger.error('Ошибка добавления %s в referral_contests: %s', column, error)
    return ok


async def create_contest_templates_table() -> bool:
    table_exists = await check_table_exists('contest_templates')
    if table_exists:
        logger.info('Таблица contest_templates уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_templates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(100) NOT NULL,
                        slug VARCHAR(50) NOT NULL UNIQUE,
                        description TEXT NULL,
                        prize_days INTEGER NOT NULL DEFAULT 1,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        times_per_day INTEGER NOT NULL DEFAULT 1,
                        schedule_times VARCHAR(255) NULL,
                        cooldown_hours INTEGER NOT NULL DEFAULT 24,
                        payload TEXT NULL,
                        is_enabled BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_templates (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(100) NOT NULL,
                        slug VARCHAR(50) NOT NULL UNIQUE,
                        description TEXT NULL,
                        prize_days INTEGER NOT NULL DEFAULT 1,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        times_per_day INTEGER NOT NULL DEFAULT 1,
                        schedule_times VARCHAR(255) NULL,
                        cooldown_hours INTEGER NOT NULL DEFAULT 24,
                        payload JSON NULL,
                        is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_templates (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        name VARCHAR(100) NOT NULL,
                        slug VARCHAR(50) NOT NULL UNIQUE,
                        description TEXT NULL,
                        prize_days INTEGER NOT NULL DEFAULT 1,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        times_per_day INTEGER NOT NULL DEFAULT 1,
                        schedule_times VARCHAR(255) NULL,
                        cooldown_hours INTEGER NOT NULL DEFAULT 24,
                        payload JSON NULL,
                        is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """)
                )
            else:
                raise ValueError(f'Unsupported database type: {db_type}')

        logger.info('✅ Таблица contest_templates создана')
        return True
    except Exception as error:
        logger.error(f'Ошибка создания таблицы contest_templates: {error}')
        return False


async def create_contest_rounds_table() -> bool:
    table_exists = await check_table_exists('contest_rounds')
    if table_exists:
        logger.info('Таблица contest_rounds уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_rounds (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        template_id INTEGER NOT NULL,
                        starts_at DATETIME NOT NULL,
                        ends_at DATETIME NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active',
                        payload TEXT NULL,
                        winners_count INTEGER NOT NULL DEFAULT 0,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        message_id BIGINT NULL,
                        chat_id BIGINT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(template_id) REFERENCES contest_templates(id) ON DELETE CASCADE
                    )
                """)
                )
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS idx_contest_round_status ON contest_rounds(status)')
                )
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS idx_contest_round_template ON contest_rounds(template_id)')
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_rounds (
                        id SERIAL PRIMARY KEY,
                        template_id INTEGER NOT NULL REFERENCES contest_templates(id) ON DELETE CASCADE,
                        starts_at TIMESTAMP NOT NULL,
                        ends_at TIMESTAMP NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active',
                        payload JSON NULL,
                        winners_count INTEGER NOT NULL DEFAULT 0,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        message_id BIGINT NULL,
                        chat_id BIGINT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                )
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS idx_contest_round_status ON contest_rounds(status)')
                )
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS idx_contest_round_template ON contest_rounds(template_id)')
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_rounds (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        template_id INTEGER NOT NULL,
                        starts_at DATETIME NOT NULL,
                        ends_at DATETIME NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active',
                        payload JSON NULL,
                        winners_count INTEGER NOT NULL DEFAULT 0,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        message_id BIGINT NULL,
                        chat_id BIGINT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        CONSTRAINT fk_contest_round_template FOREIGN KEY(template_id) REFERENCES contest_templates(id) ON DELETE CASCADE
                    )
                """)
                )
                await conn.execute(text('CREATE INDEX idx_contest_round_status ON contest_rounds(status)'))
                await conn.execute(text('CREATE INDEX idx_contest_round_template ON contest_rounds(template_id)'))
            else:
                raise ValueError(f'Unsupported database type: {db_type}')

        logger.info('✅ Таблица contest_rounds создана')
        return True
    except Exception as error:
        logger.error(f'Ошибка создания таблицы contest_rounds: {error}')
        return False


async def create_contest_attempts_table() -> bool:
    table_exists = await check_table_exists('contest_attempts')
    if table_exists:
        logger.info('Таблица contest_attempts уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        round_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        answer TEXT NULL,
                        is_winner BOOLEAN NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(round_id) REFERENCES contest_rounds(id) ON DELETE CASCADE,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        UNIQUE(round_id, user_id)
                    )
                """)
                )
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS idx_contest_attempt_round ON contest_attempts(round_id)')
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_attempts (
                        id SERIAL PRIMARY KEY,
                        round_id INTEGER NOT NULL REFERENCES contest_rounds(id) ON DELETE CASCADE,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        answer TEXT NULL,
                        is_winner BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_round_user_attempt UNIQUE(round_id, user_id)
                    )
                """)
                )
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS idx_contest_attempt_round ON contest_attempts(round_id)')
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                    CREATE TABLE contest_attempts (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        round_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        answer TEXT NULL,
                        is_winner BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_contest_attempt_round FOREIGN KEY(round_id) REFERENCES contest_rounds(id) ON DELETE CASCADE,
                        CONSTRAINT fk_contest_attempt_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        CONSTRAINT uq_round_user_attempt UNIQUE(round_id, user_id)
                    )
                """)
                )
                await conn.execute(text('CREATE INDEX idx_contest_attempt_round ON contest_attempts(round_id)'))
            else:
                raise ValueError(f'Unsupported database type: {db_type}')

        logger.info('✅ Таблица contest_attempts создана')
        return True
    except Exception as error:
        logger.error(f'Ошибка создания таблицы contest_attempts: {error}')
        return False


async def ensure_referral_contest_type_column() -> bool:
    column_exists = await check_column_exists('referral_contests', 'contest_type')
    if column_exists:
        logger.info('Колонка contest_type в referral_contests уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite' or db_type == 'postgresql' or db_type == 'mysql':
                await conn.execute(
                    text(
                        'ALTER TABLE referral_contests '
                        "ADD COLUMN contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid'"
                    )
                )
            else:
                raise ValueError(f'Unsupported database type: {db_type}')

        logger.info('✅ Колонка contest_type в referral_contests добавлена')
        return True
    except Exception as error:
        logger.error(f'Ошибка добавления contest_type в referral_contests: {error}')
        return False


async def ensure_discount_offer_columns():
    try:
        effect_exists = await check_column_exists('discount_offers', 'effect_type')
        extra_exists = await check_column_exists('discount_offers', 'extra_data')

        if effect_exists and extra_exists:
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not effect_exists:
                if db_type == 'sqlite' or db_type == 'postgresql' or db_type == 'mysql':
                    await conn.execute(
                        text(
                            "ALTER TABLE discount_offers ADD COLUMN effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount'"
                        )
                    )
                else:
                    raise ValueError(f'Unsupported database type: {db_type}')

            if not extra_exists:
                if db_type == 'sqlite':
                    await conn.execute(text('ALTER TABLE discount_offers ADD COLUMN extra_data TEXT NULL'))
                elif db_type == 'postgresql' or db_type == 'mysql':
                    await conn.execute(text('ALTER TABLE discount_offers ADD COLUMN extra_data JSON NULL'))
                else:
                    raise ValueError(f'Unsupported database type: {db_type}')

        logger.info('✅ Колонки effect_type и extra_data для discount_offers проверены')
        return True

    except Exception as e:
        logger.error(f'Ошибка обновления колонок discount_offers: {e}')
        return False


async def ensure_user_promo_offer_discount_columns():
    try:
        percent_exists = await check_column_exists('users', 'promo_offer_discount_percent')
        source_exists = await check_column_exists('users', 'promo_offer_discount_source')
        expires_exists = await check_column_exists('users', 'promo_offer_discount_expires_at')

        if percent_exists and source_exists and expires_exists:
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not percent_exists:
                column_def = 'INTEGER NOT NULL DEFAULT 0'
                if db_type == 'mysql':
                    column_def = 'INT NOT NULL DEFAULT 0'
                await conn.execute(text(f'ALTER TABLE users ADD COLUMN promo_offer_discount_percent {column_def}'))

            if not source_exists:
                if db_type == 'sqlite':
                    column_def = 'TEXT NULL'
                elif db_type == 'postgresql' or db_type == 'mysql':
                    column_def = 'VARCHAR(100) NULL'
                else:
                    raise ValueError(f'Unsupported database type: {db_type}')

                await conn.execute(text(f'ALTER TABLE users ADD COLUMN promo_offer_discount_source {column_def}'))

            if not expires_exists:
                if db_type == 'sqlite':
                    column_def = 'DATETIME NULL'
                elif db_type == 'postgresql':
                    column_def = 'TIMESTAMP NULL'
                elif db_type == 'mysql':
                    column_def = 'DATETIME NULL'
                else:
                    raise ValueError(f'Unsupported database type: {db_type}')

                await conn.execute(text(f'ALTER TABLE users ADD COLUMN promo_offer_discount_expires_at {column_def}'))

        logger.info('✅ Колонки promo_offer_discount_* для users проверены')
        return True
    except Exception as e:
        logger.error(f'Ошибка обновления колонок promo_offer_discount_*: {e}')
        return False


async def ensure_user_notification_settings_column() -> bool:
    """Ensure notification_settings column exists in users table."""
    try:
        column_exists = await check_column_exists('users', 'notification_settings')

        if column_exists:
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                column_def = 'TEXT NULL'
            elif db_type == 'postgresql':
                column_def = 'JSONB NULL'
            elif db_type == 'mysql':
                column_def = 'JSON NULL'
            else:
                column_def = 'TEXT NULL'

            await conn.execute(text(f'ALTER TABLE users ADD COLUMN notification_settings {column_def}'))

        logger.info('✅ Колонка notification_settings для users добавлена')
        return True
    except Exception as e:
        logger.error(f'Ошибка добавления колонки notification_settings: {e}')
        return False


async def ensure_promo_offer_template_active_duration_column() -> bool:
    try:
        column_exists = await check_column_exists('promo_offer_templates', 'active_discount_hours')

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not column_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    column_def = 'INTEGER NULL'
                elif db_type == 'mysql':
                    column_def = 'INT NULL'
                else:
                    raise ValueError(f'Unsupported database type: {db_type}')

                await conn.execute(
                    text(f'ALTER TABLE promo_offer_templates ADD COLUMN active_discount_hours {column_def}')
                )

            await conn.execute(
                text(
                    'UPDATE promo_offer_templates '
                    'SET active_discount_hours = valid_hours '
                    "WHERE offer_type IN ('extend_discount', 'purchase_discount') "
                    'AND (active_discount_hours IS NULL OR active_discount_hours <= 0)'
                )
            )

        logger.info('✅ Колонка active_discount_hours в promo_offer_templates актуальна')
        return True
    except Exception as e:
        logger.error(f'Ошибка обновления active_discount_hours в promo_offer_templates: {e}')
        return False


async def migrate_discount_offer_effect_types():
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE discount_offers SET effect_type = 'percent_discount' WHERE effect_type = 'balance_bonus'")
            )
        logger.info('✅ Типы эффектов discount_offers обновлены на percent_discount')
        return True
    except Exception as e:
        logger.error(f'Ошибка обновления типов эффектов discount_offers: {e}')
        return False


async def reset_discount_offer_bonuses():
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text('UPDATE discount_offers SET bonus_amount_kopeks = 0 WHERE bonus_amount_kopeks <> 0')
            )
            await conn.execute(
                text('UPDATE promo_offer_templates SET bonus_amount_kopeks = 0 WHERE bonus_amount_kopeks <> 0')
            )
        logger.info('✅ Бонусы промо-предложений сброшены до нуля')
        return True
    except Exception as e:
        logger.error(f'Ошибка обнуления бонусов промо-предложений: {e}')
        return False


async def create_promo_offer_templates_table():
    table_exists = await check_table_exists('promo_offer_templates')
    if table_exists:
        logger.info('Таблица promo_offer_templates уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE promo_offer_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL,
                    offer_type VARCHAR(50) NOT NULL,
                    message_text TEXT NOT NULL,
                    button_text VARCHAR(255) NOT NULL,
                    valid_hours INTEGER NOT NULL DEFAULT 24,
                    discount_percent INTEGER NOT NULL DEFAULT 0,
                    bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                    active_discount_hours INTEGER NULL,
                    test_duration_hours INTEGER NULL,
                    test_squad_uuids TEXT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_by INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_promo_offer_templates_type ON promo_offer_templates(offer_type);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS promo_offer_templates (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    offer_type VARCHAR(50) NOT NULL,
                    message_text TEXT NOT NULL,
                    button_text VARCHAR(255) NOT NULL,
                    valid_hours INTEGER NOT NULL DEFAULT 24,
                    discount_percent INTEGER NOT NULL DEFAULT 0,
                    bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                    active_discount_hours INTEGER NULL,
                    test_duration_hours INTEGER NULL,
                    test_squad_uuids JSON NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS ix_promo_offer_templates_type ON promo_offer_templates(offer_type);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS promo_offer_templates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    offer_type VARCHAR(50) NOT NULL,
                    message_text TEXT NOT NULL,
                    button_text VARCHAR(255) NOT NULL,
                    valid_hours INT NOT NULL DEFAULT 24,
                    discount_percent INT NOT NULL DEFAULT 0,
                    bonus_amount_kopeks INT NOT NULL DEFAULT 0,
                    active_discount_hours INT NULL,
                    test_duration_hours INT NULL,
                    test_squad_uuids JSON NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_promo_offer_templates_type ON promo_offer_templates(offer_type);
                """
            else:
                raise ValueError(f'Unsupported database type: {db_type}')

            await conn.execute(text(create_sql))

        logger.info('✅ Таблица promo_offer_templates успешно создана')
        return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы promo_offer_templates: {e}')
        return False


async def create_main_menu_buttons_table() -> bool:
    table_exists = await check_table_exists('main_menu_buttons')
    if table_exists:
        logger.info('Таблица main_menu_buttons уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE main_menu_buttons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text VARCHAR(64) NOT NULL,
                    action_type VARCHAR(20) NOT NULL,
                    action_value TEXT NOT NULL,
                    visibility VARCHAR(20) NOT NULL DEFAULT 'all',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS ix_main_menu_buttons_order ON main_menu_buttons(display_order, id);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS main_menu_buttons (
                    id SERIAL PRIMARY KEY,
                    text VARCHAR(64) NOT NULL,
                    action_type VARCHAR(20) NOT NULL,
                    action_value TEXT NOT NULL,
                    visibility VARCHAR(20) NOT NULL DEFAULT 'all',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS ix_main_menu_buttons_order ON main_menu_buttons(display_order, id);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS main_menu_buttons (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    text VARCHAR(64) NOT NULL,
                    action_type VARCHAR(20) NOT NULL,
                    action_value TEXT NOT NULL,
                    visibility VARCHAR(20) NOT NULL DEFAULT 'all',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    display_order INT NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                );

                CREATE INDEX ix_main_menu_buttons_order ON main_menu_buttons(display_order, id);
                """
            else:
                logger.error(f'Неподдерживаемый тип БД для таблицы main_menu_buttons: {db_type}')
                return False

            await conn.execute(text(create_sql))

        logger.info('✅ Таблица main_menu_buttons успешно создана')
        return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы main_menu_buttons: {e}')
        return False


async def create_promo_offer_logs_table() -> bool:
    table_exists = await check_table_exists('promo_offer_logs')
    if table_exists:
        logger.info('Таблица promo_offer_logs уже существует')
        return True

    try:
        db_type = await get_database_type()
        async with engine.begin() as conn:
            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                    CREATE TABLE IF NOT EXISTS promo_offer_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                        offer_id INTEGER NULL REFERENCES discount_offers(id) ON DELETE SET NULL,
                        action VARCHAR(50) NOT NULL,
                        source VARCHAR(100) NULL,
                        percent INTEGER NULL,
                        effect_type VARCHAR(50) NULL,
                        details JSON NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS ix_promo_offer_logs_created_at ON promo_offer_logs(created_at DESC);
                    CREATE INDEX IF NOT EXISTS ix_promo_offer_logs_user_id ON promo_offer_logs(user_id);
                """)
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                    CREATE TABLE IF NOT EXISTS promo_offer_logs (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        offer_id INTEGER REFERENCES discount_offers(id) ON DELETE SET NULL,
                        action VARCHAR(50) NOT NULL,
                        source VARCHAR(100),
                        percent INTEGER,
                        effect_type VARCHAR(50),
                        details JSONB,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS ix_promo_offer_logs_created_at ON promo_offer_logs(created_at DESC);
                    CREATE INDEX IF NOT EXISTS ix_promo_offer_logs_user_id ON promo_offer_logs(user_id);
                """)
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                    CREATE TABLE IF NOT EXISTS promo_offer_logs (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NULL,
                        offer_id INT NULL,
                        action VARCHAR(50) NOT NULL,
                        source VARCHAR(100) NULL,
                        percent INT NULL,
                        effect_type VARCHAR(50) NULL,
                        details JSON NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_promo_offer_logs_users FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                        CONSTRAINT fk_promo_offer_logs_offers FOREIGN KEY (offer_id) REFERENCES discount_offers(id) ON DELETE SET NULL
                    );

                    CREATE INDEX ix_promo_offer_logs_created_at ON promo_offer_logs(created_at DESC);
                    CREATE INDEX ix_promo_offer_logs_user_id ON promo_offer_logs(user_id);
                """)
                )
            else:
                logger.warning('Неизвестный тип БД для создания promo_offer_logs: %s', db_type)
                return False

        logger.info('✅ Таблица promo_offer_logs успешно создана')
        return True
    except Exception as e:
        logger.error(f'Ошибка создания таблицы promo_offer_logs: {e}')
        return False


async def create_subscription_temporary_access_table():
    table_exists = await check_table_exists('subscription_temporary_access')
    if table_exists:
        logger.info('Таблица subscription_temporary_access уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE subscription_temporary_access (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    offer_id INTEGER NOT NULL,
                    squad_uuid VARCHAR(255) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    deactivated_at DATETIME NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    was_already_connected BOOLEAN NOT NULL DEFAULT 0,
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
                    FOREIGN KEY(offer_id) REFERENCES discount_offers(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_temp_access_subscription ON subscription_temporary_access(subscription_id);
                CREATE INDEX ix_temp_access_offer ON subscription_temporary_access(offer_id);
                CREATE INDEX ix_temp_access_active ON subscription_temporary_access(is_active, expires_at);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS subscription_temporary_access (
                    id SERIAL PRIMARY KEY,
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    offer_id INTEGER NOT NULL REFERENCES discount_offers(id) ON DELETE CASCADE,
                    squad_uuid VARCHAR(255) NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deactivated_at TIMESTAMP NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    was_already_connected BOOLEAN NOT NULL DEFAULT FALSE
                );

                CREATE INDEX IF NOT EXISTS ix_temp_access_subscription ON subscription_temporary_access(subscription_id);
                CREATE INDEX IF NOT EXISTS ix_temp_access_offer ON subscription_temporary_access(offer_id);
                CREATE INDEX IF NOT EXISTS ix_temp_access_active ON subscription_temporary_access(is_active, expires_at);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS subscription_temporary_access (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    subscription_id INT NOT NULL,
                    offer_id INT NOT NULL,
                    squad_uuid VARCHAR(255) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    deactivated_at DATETIME NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    was_already_connected BOOLEAN NOT NULL DEFAULT FALSE,
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
                    FOREIGN KEY(offer_id) REFERENCES discount_offers(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_temp_access_subscription ON subscription_temporary_access(subscription_id);
                CREATE INDEX ix_temp_access_offer ON subscription_temporary_access(offer_id);
                CREATE INDEX ix_temp_access_active ON subscription_temporary_access(is_active, expires_at);
                """
            else:
                raise ValueError(f'Unsupported database type: {db_type}')

            await conn.execute(text(create_sql))

        logger.info('✅ Таблица subscription_temporary_access успешно создана')
        return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы subscription_temporary_access: {e}')
        return False


async def create_user_messages_table():
    table_exists = await check_table_exists('user_messages')
    if table_exists:
        logger.info('Таблица user_messages уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE user_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_text TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    sort_order INTEGER DEFAULT 0,
                    created_by INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX idx_user_messages_active ON user_messages(is_active);
                CREATE INDEX idx_user_messages_sort ON user_messages(sort_order, created_at);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE user_messages (
                    id SERIAL PRIMARY KEY,
                    message_text TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    sort_order INTEGER DEFAULT 0,
                    created_by INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX idx_user_messages_active ON user_messages(is_active);
                CREATE INDEX idx_user_messages_sort ON user_messages(sort_order, created_at);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE user_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    message_text TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    sort_order INT DEFAULT 0,
                    created_by INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX idx_user_messages_active ON user_messages(is_active);
                CREATE INDEX idx_user_messages_sort ON user_messages(sort_order, created_at);
                """
            else:
                logger.error(f'Неподдерживаемый тип БД для создания таблицы: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('Таблица user_messages успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы user_messages: {e}')
        return False


async def ensure_promo_groups_setup():
    logger.info('=== НАСТРОЙКА ПРОМО ГРУПП ===')

    try:
        promo_table_exists = await check_table_exists('promo_groups')

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not promo_table_exists:
                if db_type == 'sqlite':
                    await conn.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS promo_groups (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                name VARCHAR(255) NOT NULL,
                                server_discount_percent INTEGER NOT NULL DEFAULT 0,
                                traffic_discount_percent INTEGER NOT NULL DEFAULT 0,
                                device_discount_percent INTEGER NOT NULL DEFAULT 0,
                                is_default BOOLEAN NOT NULL DEFAULT 0,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                            )
                        """
                        )
                    )
                    await conn.execute(
                        text('CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_groups_name ON promo_groups(name)')
                    )
                elif db_type == 'postgresql':
                    await conn.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS promo_groups (
                                id SERIAL PRIMARY KEY,
                                name VARCHAR(255) NOT NULL,
                                server_discount_percent INTEGER NOT NULL DEFAULT 0,
                                traffic_discount_percent INTEGER NOT NULL DEFAULT 0,
                                device_discount_percent INTEGER NOT NULL DEFAULT 0,
                                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                                CONSTRAINT uq_promo_groups_name UNIQUE (name)
                            )
                        """
                        )
                    )
                elif db_type == 'mysql':
                    await conn.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS promo_groups (
                                id INT AUTO_INCREMENT PRIMARY KEY,
                                name VARCHAR(255) NOT NULL,
                                server_discount_percent INT NOT NULL DEFAULT 0,
                                traffic_discount_percent INT NOT NULL DEFAULT 0,
                                device_discount_percent INT NOT NULL DEFAULT 0,
                                is_default TINYINT(1) NOT NULL DEFAULT 0,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                UNIQUE KEY uq_promo_groups_name (name)
                            ) ENGINE=InnoDB
                        """
                        )
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД для promo_groups: {db_type}')
                    return False

                logger.info('Создана таблица promo_groups')

            if db_type == 'postgresql' and not await check_constraint_exists('promo_groups', 'uq_promo_groups_name'):
                try:
                    await conn.execute(
                        text('ALTER TABLE promo_groups ADD CONSTRAINT uq_promo_groups_name UNIQUE (name)')
                    )
                except Exception as e:
                    logger.warning(f'Не удалось добавить уникальное ограничение uq_promo_groups_name: {e}')

            period_discounts_column_exists = await check_column_exists('promo_groups', 'period_discounts')

            if not period_discounts_column_exists:
                if db_type == 'sqlite':
                    await conn.execute(text('ALTER TABLE promo_groups ADD COLUMN period_discounts JSON'))
                    await conn.execute(
                        text("UPDATE promo_groups SET period_discounts = '{}' WHERE period_discounts IS NULL")
                    )
                elif db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE promo_groups ADD COLUMN period_discounts JSONB'))
                    await conn.execute(
                        text("UPDATE promo_groups SET period_discounts = '{}'::jsonb WHERE period_discounts IS NULL")
                    )
                elif db_type == 'mysql':
                    await conn.execute(text('ALTER TABLE promo_groups ADD COLUMN period_discounts JSON'))
                    await conn.execute(
                        text('UPDATE promo_groups SET period_discounts = JSON_OBJECT() WHERE period_discounts IS NULL')
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД для promo_groups.period_discounts: {db_type}')
                    return False

                logger.info('Добавлена колонка promo_groups.period_discounts')

            auto_assign_column_exists = await check_column_exists('promo_groups', 'auto_assign_total_spent_kopeks')

            if not auto_assign_column_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE promo_groups ADD COLUMN auto_assign_total_spent_kopeks INTEGER DEFAULT 0')
                    )
                elif db_type == 'mysql':
                    await conn.execute(
                        text('ALTER TABLE promo_groups ADD COLUMN auto_assign_total_spent_kopeks INT DEFAULT 0')
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД для promo_groups.auto_assign_total_spent_kopeks: {db_type}')
                    return False

                logger.info('Добавлена колонка promo_groups.auto_assign_total_spent_kopeks')

            addon_discount_column_exists = await check_column_exists('promo_groups', 'apply_discounts_to_addons')
            priority_column_exists = await check_column_exists('promo_groups', 'priority')

            if not addon_discount_column_exists:
                if db_type == 'sqlite':
                    await conn.execute(
                        text('ALTER TABLE promo_groups ADD COLUMN apply_discounts_to_addons BOOLEAN NOT NULL DEFAULT 1')
                    )
                    await conn.execute(
                        text(
                            'UPDATE promo_groups SET apply_discounts_to_addons = 1 WHERE apply_discounts_to_addons IS NULL'
                        )
                    )
                elif db_type == 'postgresql':
                    await conn.execute(
                        text(
                            'ALTER TABLE promo_groups ADD COLUMN apply_discounts_to_addons BOOLEAN NOT NULL DEFAULT TRUE'
                        )
                    )
                    await conn.execute(
                        text(
                            'UPDATE promo_groups SET apply_discounts_to_addons = TRUE WHERE apply_discounts_to_addons IS NULL'
                        )
                    )
                elif db_type == 'mysql':
                    await conn.execute(
                        text(
                            'ALTER TABLE promo_groups ADD COLUMN apply_discounts_to_addons TINYINT(1) NOT NULL DEFAULT 1'
                        )
                    )
                    await conn.execute(
                        text(
                            'UPDATE promo_groups SET apply_discounts_to_addons = 1 WHERE apply_discounts_to_addons IS NULL'
                        )
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД для promo_groups.apply_discounts_to_addons: {db_type}')
                    return False

                logger.info('Добавлена колонка promo_groups.apply_discounts_to_addons')
                addon_discount_column_exists = True

            column_exists = await check_column_exists('users', 'promo_group_id')

            if not column_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE users ADD COLUMN promo_group_id INTEGER'))
                elif db_type == 'mysql':
                    await conn.execute(text('ALTER TABLE users ADD COLUMN promo_group_id INT'))
                else:
                    logger.error(f'Неподдерживаемый тип БД для promo_group_id: {db_type}')
                    return False

                logger.info('Добавлена колонка users.promo_group_id')

            auto_promo_flag_exists = await check_column_exists('users', 'auto_promo_group_assigned')

            if not auto_promo_flag_exists:
                if db_type == 'sqlite':
                    await conn.execute(text('ALTER TABLE users ADD COLUMN auto_promo_group_assigned BOOLEAN DEFAULT 0'))
                elif db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE users ADD COLUMN auto_promo_group_assigned BOOLEAN DEFAULT FALSE')
                    )
                elif db_type == 'mysql':
                    await conn.execute(
                        text('ALTER TABLE users ADD COLUMN auto_promo_group_assigned TINYINT(1) DEFAULT 0')
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД для users.auto_promo_group_assigned: {db_type}')
                    return False

                logger.info('Добавлена колонка users.auto_promo_group_assigned')

            threshold_column_exists = await check_column_exists('users', 'auto_promo_group_threshold_kopeks')

            if not threshold_column_exists:
                if db_type == 'sqlite':
                    await conn.execute(
                        text(
                            'ALTER TABLE users ADD COLUMN auto_promo_group_threshold_kopeks INTEGER NOT NULL DEFAULT 0'
                        )
                    )
                elif db_type == 'postgresql' or db_type == 'mysql':
                    await conn.execute(
                        text('ALTER TABLE users ADD COLUMN auto_promo_group_threshold_kopeks BIGINT NOT NULL DEFAULT 0')
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД для users.auto_promo_group_threshold_kopeks: {db_type}')
                    return False

                logger.info('Добавлена колонка users.auto_promo_group_threshold_kopeks')

            index_exists = await check_index_exists('users', 'ix_users_promo_group_id')

            if not index_exists:
                try:
                    if db_type == 'sqlite' or db_type == 'postgresql':
                        await conn.execute(
                            text('CREATE INDEX IF NOT EXISTS ix_users_promo_group_id ON users(promo_group_id)')
                        )
                    elif db_type == 'mysql':
                        await conn.execute(text('CREATE INDEX ix_users_promo_group_id ON users(promo_group_id)'))
                    logger.info('Создан индекс ix_users_promo_group_id')
                except Exception as e:
                    logger.warning(f'Не удалось создать индекс ix_users_promo_group_id: {e}')

            default_group_name = 'Базовый юзер'
            default_group_id = None

            result = await conn.execute(
                text('SELECT id, is_default FROM promo_groups WHERE name = :name LIMIT 1'),
                {'name': default_group_name},
            )
            row = result.fetchone()

            if row:
                default_group_id = row[0]
                if not row[1]:
                    await conn.execute(
                        text('UPDATE promo_groups SET is_default = :is_default WHERE id = :group_id'),
                        {'is_default': True, 'group_id': default_group_id},
                    )
            else:
                result = await conn.execute(
                    text('SELECT id FROM promo_groups WHERE is_default = :is_default LIMIT 1'),
                    {'is_default': True},
                )
                existing_default = result.fetchone()

                if existing_default:
                    default_group_id = existing_default[0]
                else:
                    insert_params = {
                        'name': default_group_name,
                        'is_default': True,
                    }

                    if priority_column_exists:
                        insert_params['priority'] = 0

                    if addon_discount_column_exists and priority_column_exists:
                        insert_sql = """
                            INSERT INTO promo_groups (
                                name,
                                priority,
                                server_discount_percent,
                                traffic_discount_percent,
                                device_discount_percent,
                                apply_discounts_to_addons,
                                is_default
                            ) VALUES (:name, :priority, 0, 0, 0, :apply_discounts_to_addons, :is_default)
                        """
                        insert_params['apply_discounts_to_addons'] = True
                    elif addon_discount_column_exists:
                        insert_sql = """
                            INSERT INTO promo_groups (
                                name,
                                server_discount_percent,
                                traffic_discount_percent,
                                device_discount_percent,
                                apply_discounts_to_addons,
                                is_default
                            ) VALUES (:name, 0, 0, 0, :apply_discounts_to_addons, :is_default)
                        """
                        insert_params['apply_discounts_to_addons'] = True
                    elif priority_column_exists:
                        insert_sql = """
                            INSERT INTO promo_groups (
                                name,
                                priority,
                                server_discount_percent,
                                traffic_discount_percent,
                                device_discount_percent,
                                is_default
                            ) VALUES (:name, :priority, 0, 0, 0, :is_default)
                        """
                    else:
                        insert_sql = """
                            INSERT INTO promo_groups (
                                name,
                                server_discount_percent,
                                traffic_discount_percent,
                                device_discount_percent,
                                is_default
                            ) VALUES (:name, 0, 0, 0, :is_default)
                        """

                    await conn.execute(text(insert_sql), insert_params)

                    result = await conn.execute(
                        text('SELECT id FROM promo_groups WHERE name = :name LIMIT 1'),
                        {'name': default_group_name},
                    )
                    row = result.fetchone()
                    default_group_id = row[0] if row else None

            if default_group_id is None:
                logger.error('Не удалось определить идентификатор базовой промо-группы')
                return False

            await conn.execute(
                text(
                    """
                    UPDATE users
                    SET promo_group_id = :group_id
                    WHERE promo_group_id IS NULL
                """
                ),
                {'group_id': default_group_id},
            )

            if db_type == 'postgresql':
                constraint_exists = await check_constraint_exists('users', 'fk_users_promo_group_id_promo_groups')
                if not constraint_exists:
                    try:
                        await conn.execute(
                            text(
                                """
                                ALTER TABLE users
                                ADD CONSTRAINT fk_users_promo_group_id_promo_groups
                                FOREIGN KEY (promo_group_id)
                                REFERENCES promo_groups(id)
                                ON DELETE RESTRICT
                            """
                            )
                        )
                        logger.info('Добавлен внешний ключ users -> promo_groups')
                    except Exception as e:
                        logger.warning(f'Не удалось добавить внешний ключ users.promo_group_id: {e}')

                try:
                    await conn.execute(text('ALTER TABLE users ALTER COLUMN promo_group_id SET NOT NULL'))
                except Exception as e:
                    logger.warning(f'Не удалось сделать users.promo_group_id NOT NULL: {e}')

            elif db_type == 'mysql':
                constraint_exists = await check_constraint_exists('users', 'fk_users_promo_group_id_promo_groups')
                if not constraint_exists:
                    try:
                        await conn.execute(
                            text(
                                """
                                ALTER TABLE users
                                ADD CONSTRAINT fk_users_promo_group_id_promo_groups
                                FOREIGN KEY (promo_group_id)
                                REFERENCES promo_groups(id)
                                ON DELETE RESTRICT
                            """
                            )
                        )
                        logger.info('Добавлен внешний ключ users -> promo_groups')
                    except Exception as e:
                        logger.warning(f'Не удалось добавить внешний ключ users.promo_group_id: {e}')

                try:
                    await conn.execute(text('ALTER TABLE users MODIFY promo_group_id INT NOT NULL'))
                except Exception as e:
                    logger.warning(f'Не удалось сделать users.promo_group_id NOT NULL: {e}')

            logger.info('✅ Промо группы настроены')
            return True

    except Exception as e:
        logger.error(f'Ошибка настройки промо групп: {e}')
        return False


async def add_welcome_text_is_enabled_column():
    column_exists = await check_column_exists('welcome_texts', 'is_enabled')
    if column_exists:
        logger.info('Колонка is_enabled уже существует в таблице welcome_texts')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                alter_sql = 'ALTER TABLE welcome_texts ADD COLUMN is_enabled BOOLEAN DEFAULT 1 NOT NULL'
            elif db_type == 'postgresql' or db_type == 'mysql':
                alter_sql = 'ALTER TABLE welcome_texts ADD COLUMN is_enabled BOOLEAN DEFAULT TRUE NOT NULL'
            else:
                logger.error(f'Неподдерживаемый тип БД для добавления колонки: {db_type}')
                return False

            await conn.execute(text(alter_sql))
            logger.info('✅ Поле is_enabled добавлено в таблицу welcome_texts')

            if db_type == 'sqlite':
                update_sql = 'UPDATE welcome_texts SET is_enabled = 1 WHERE is_enabled IS NULL'
            else:
                update_sql = 'UPDATE welcome_texts SET is_enabled = TRUE WHERE is_enabled IS NULL'

            result = await conn.execute(text(update_sql))
            updated_count = result.rowcount
            logger.info(f'Обновлено {updated_count} существующих записей welcome_texts')

            return True

    except Exception as e:
        logger.error(f'Ошибка при добавлении поля is_enabled: {e}')
        return False


async def create_welcome_texts_table():
    table_exists = await check_table_exists('welcome_texts')
    if table_exists:
        logger.info('Таблица welcome_texts уже существует')
        return await add_welcome_text_is_enabled_column()

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE welcome_texts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_content TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    is_enabled BOOLEAN DEFAULT 1 NOT NULL,
                    created_by INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX idx_welcome_texts_active ON welcome_texts(is_active);
                CREATE INDEX idx_welcome_texts_enabled ON welcome_texts(is_enabled);
                CREATE INDEX idx_welcome_texts_updated ON welcome_texts(updated_at);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE welcome_texts (
                    id SERIAL PRIMARY KEY,
                    text_content TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    is_enabled BOOLEAN DEFAULT TRUE NOT NULL,
                    created_by INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX idx_welcome_texts_active ON welcome_texts(is_active);
                CREATE INDEX idx_welcome_texts_enabled ON welcome_texts(is_enabled);
                CREATE INDEX idx_welcome_texts_updated ON welcome_texts(updated_at);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE welcome_texts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    text_content TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    is_enabled BOOLEAN DEFAULT TRUE NOT NULL,
                    created_by INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX idx_welcome_texts_active ON welcome_texts(is_active);
                CREATE INDEX idx_welcome_texts_enabled ON welcome_texts(is_enabled);
                CREATE INDEX idx_welcome_texts_updated ON welcome_texts(updated_at);
                """
            else:
                logger.error(f'Неподдерживаемый тип БД для создания таблицы: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица welcome_texts успешно создана с полем is_enabled')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы welcome_texts: {e}')
        return False


async def create_pinned_messages_table():
    table_exists = await check_table_exists('pinned_messages')
    if table_exists:
        logger.info('Таблица pinned_messages уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE pinned_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL DEFAULT '',
                    media_type VARCHAR(32) NULL,
                    media_file_id VARCHAR(255) NULL,
                    send_before_menu BOOLEAN NOT NULL DEFAULT 1,
                    send_on_every_start BOOLEAN NOT NULL DEFAULT 1,
                    is_active BOOLEAN DEFAULT 1,
                    created_by INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS ix_pinned_messages_active ON pinned_messages(is_active);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE pinned_messages (
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL DEFAULT '',
                    media_type VARCHAR(32) NULL,
                    media_file_id VARCHAR(255) NULL,
                    send_before_menu BOOLEAN NOT NULL DEFAULT TRUE,
                    send_on_every_start BOOLEAN NOT NULL DEFAULT TRUE,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS ix_pinned_messages_active ON pinned_messages(is_active);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE pinned_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    content TEXT NOT NULL DEFAULT '',
                    media_type VARCHAR(32) NULL,
                    media_file_id VARCHAR(255) NULL,
                    send_before_menu BOOLEAN NOT NULL DEFAULT TRUE,
                    send_on_every_start BOOLEAN NOT NULL DEFAULT TRUE,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_by INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_pinned_messages_active ON pinned_messages(is_active);
                """

            else:
                logger.error(f'Неподдерживаемый тип БД для создания таблицы pinned_messages: {db_type}')
                return False

            await conn.execute(text(create_sql))

        logger.info('✅ Таблица pinned_messages успешно создана')
        return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы pinned_messages: {e}')
        return False


async def ensure_pinned_message_media_columns():
    table_exists = await check_table_exists('pinned_messages')
    if not table_exists:
        logger.warning('⚠️ Таблица pinned_messages отсутствует — пропускаем обновление медиа полей')
        return False

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not await check_column_exists('pinned_messages', 'media_type'):
                await conn.execute(text('ALTER TABLE pinned_messages ADD COLUMN media_type VARCHAR(32)'))

            if not await check_column_exists('pinned_messages', 'media_file_id'):
                await conn.execute(text('ALTER TABLE pinned_messages ADD COLUMN media_file_id VARCHAR(255)'))

            if not await check_column_exists('pinned_messages', 'send_before_menu'):
                default_value = 'TRUE' if db_type != 'sqlite' else '1'
                await conn.execute(
                    text(
                        f'ALTER TABLE pinned_messages ADD COLUMN send_before_menu BOOLEAN NOT NULL DEFAULT {default_value}'
                    )
                )

            if not await check_column_exists('pinned_messages', 'send_on_every_start'):
                default_value = 'TRUE' if db_type != 'sqlite' else '1'
                await conn.execute(
                    text(
                        f'ALTER TABLE pinned_messages ADD COLUMN send_on_every_start BOOLEAN NOT NULL DEFAULT {default_value}'
                    )
                )

            await conn.execute(text("UPDATE pinned_messages SET content = '' WHERE content IS NULL"))

            if db_type == 'postgresql':
                await conn.execute(text("ALTER TABLE pinned_messages ALTER COLUMN content SET DEFAULT ''"))
            elif db_type == 'mysql':
                await conn.execute(text("ALTER TABLE pinned_messages MODIFY content TEXT NOT NULL DEFAULT ''"))
            else:
                logger.info('ℹ️ Пропускаем установку DEFAULT для content в SQLite')

        logger.info('✅ Медиа поля pinned_messages приведены в актуальное состояние')
        return True

    except Exception as e:
        logger.error(f'Ошибка обновления медиа полей pinned_messages: {e}')
        return False


async def ensure_user_last_pinned_column():
    try:
        async with engine.begin() as conn:
            if not await check_column_exists('users', 'last_pinned_message_id'):
                await conn.execute(text('ALTER TABLE users ADD COLUMN last_pinned_message_id INTEGER'))
        logger.info('✅ Поле last_pinned_message_id у пользователей готово')
        return True
    except Exception as e:
        logger.error(f'Ошибка добавления поля last_pinned_message_id: {e}')
        return False


async def add_media_fields_to_broadcast_history():
    logger.info('=== ДОБАВЛЕНИЕ ПОЛЕЙ МЕДИА В BROADCAST_HISTORY ===')

    media_fields = {
        'has_media': 'BOOLEAN DEFAULT FALSE',
        'media_type': 'VARCHAR(20)',
        'media_file_id': 'VARCHAR(255)',
        'media_caption': 'TEXT',
    }

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            for field_name, field_type in media_fields.items():
                field_exists = await check_column_exists('broadcast_history', field_name)

                if not field_exists:
                    logger.info(f'Добавление поля {field_name} в таблицу broadcast_history')

                    if db_type == 'sqlite':
                        if 'BOOLEAN' in field_type:
                            field_type = field_type.replace('BOOLEAN DEFAULT FALSE', 'BOOLEAN DEFAULT 0')
                    elif db_type == 'postgresql' or db_type == 'mysql':
                        if 'BOOLEAN' in field_type:
                            field_type = field_type.replace('BOOLEAN DEFAULT FALSE', 'BOOLEAN DEFAULT FALSE')

                    alter_sql = f'ALTER TABLE broadcast_history ADD COLUMN {field_name} {field_type}'
                    await conn.execute(text(alter_sql))
                    logger.info(f'✅ Поле {field_name} успешно добавлено')
                else:
                    logger.info(f'Поле {field_name} уже существует в broadcast_history')

            logger.info('✅ Все поля медиа в broadcast_history готовы')
            return True

    except Exception as e:
        logger.error(f'Ошибка при добавлении полей медиа в broadcast_history: {e}')
        return False


async def add_email_fields_to_broadcast_history():
    """Добавление полей для email-рассылки в broadcast_history."""
    logger.info('=== ДОБАВЛЕНИЕ ПОЛЕЙ EMAIL В BROADCAST_HISTORY ===')

    email_fields = {
        'channel': "VARCHAR(20) DEFAULT 'telegram'",
        'email_subject': 'VARCHAR(255)',
        'email_html_content': 'TEXT',
    }

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            # Добавление новых полей
            for field_name, field_type in email_fields.items():
                field_exists = await check_column_exists('broadcast_history', field_name)

                if not field_exists:
                    logger.info(f'Добавление поля {field_name} в таблицу broadcast_history')

                    alter_sql = f'ALTER TABLE broadcast_history ADD COLUMN {field_name} {field_type}'
                    await conn.execute(text(alter_sql))
                    logger.info(f'✅ Поле {field_name} успешно добавлено')
                else:
                    logger.info(f'Поле {field_name} уже существует в broadcast_history')

            # Сделать message_text nullable для email-only рассылок
            try:
                if db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE broadcast_history ALTER COLUMN message_text DROP NOT NULL'))
                    logger.info('✅ Колонка message_text теперь nullable')
                elif db_type == 'mysql':
                    await conn.execute(text('ALTER TABLE broadcast_history MODIFY COLUMN message_text TEXT NULL'))
                    logger.info('✅ Колонка message_text теперь nullable')
                # SQLite не поддерживает ALTER COLUMN, но там по умолчанию nullable
            except Exception as e:
                # Игнорируем если уже nullable или другая ошибка
                logger.debug(f'message_text nullable: {e}')

            logger.info('✅ Все поля email в broadcast_history готовы')
            return True

    except Exception as e:
        logger.error(f'Ошибка при добавлении полей email в broadcast_history: {e}')
        return False


async def add_ticket_reply_block_columns():
    try:
        col_perm_exists = await check_column_exists('tickets', 'user_reply_block_permanent')
        col_until_exists = await check_column_exists('tickets', 'user_reply_block_until')

        if col_perm_exists and col_until_exists:
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not col_perm_exists:
                if db_type == 'sqlite':
                    alter_sql = 'ALTER TABLE tickets ADD COLUMN user_reply_block_permanent BOOLEAN DEFAULT 0 NOT NULL'
                elif db_type == 'postgresql' or db_type == 'mysql':
                    alter_sql = (
                        'ALTER TABLE tickets ADD COLUMN user_reply_block_permanent BOOLEAN DEFAULT FALSE NOT NULL'
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД для добавления user_reply_block_permanent: {db_type}')
                    return False
                await conn.execute(text(alter_sql))
                logger.info('✅ Добавлена колонка tickets.user_reply_block_permanent')

            if not col_until_exists:
                if db_type == 'sqlite':
                    alter_sql = 'ALTER TABLE tickets ADD COLUMN user_reply_block_until DATETIME NULL'
                elif db_type == 'postgresql':
                    alter_sql = 'ALTER TABLE tickets ADD COLUMN user_reply_block_until TIMESTAMP NULL'
                elif db_type == 'mysql':
                    alter_sql = 'ALTER TABLE tickets ADD COLUMN user_reply_block_until DATETIME NULL'
                else:
                    logger.error(f'Неподдерживаемый тип БД для добавления user_reply_block_until: {db_type}')
                    return False
                await conn.execute(text(alter_sql))
                logger.info('✅ Добавлена колонка tickets.user_reply_block_until')

            return True
    except Exception as e:
        logger.error(f'Ошибка добавления колонок блокировок в tickets: {e}')
        return False


async def add_ticket_sla_columns():
    try:
        col_exists = await check_column_exists('tickets', 'last_sla_reminder_at')
        if col_exists:
            return True
        async with engine.begin() as conn:
            db_type = await get_database_type()
            if db_type == 'sqlite':
                alter_sql = 'ALTER TABLE tickets ADD COLUMN last_sla_reminder_at DATETIME NULL'
            elif db_type == 'postgresql':
                alter_sql = 'ALTER TABLE tickets ADD COLUMN last_sla_reminder_at TIMESTAMP NULL'
            elif db_type == 'mysql':
                alter_sql = 'ALTER TABLE tickets ADD COLUMN last_sla_reminder_at DATETIME NULL'
            else:
                logger.error(f'Неподдерживаемый тип БД для добавления last_sla_reminder_at: {db_type}')
                return False
            await conn.execute(text(alter_sql))
            logger.info('✅ Добавлена колонка tickets.last_sla_reminder_at')
            return True
    except Exception as e:
        logger.error(f'Ошибка добавления SLA колонки в tickets: {e}')
        return False


async def add_user_restriction_columns() -> bool:
    """Добавить колонки ограничений пользователей в таблицу users."""
    try:
        col_topup = await check_column_exists('users', 'restriction_topup')
        col_sub = await check_column_exists('users', 'restriction_subscription')
        col_reason = await check_column_exists('users', 'restriction_reason')

        if col_topup and col_sub and col_reason:
            logger.info('ℹ️ Колонки ограничений пользователей уже существуют')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not col_topup:
                if db_type == 'sqlite':
                    await conn.execute(
                        text('ALTER TABLE users ADD COLUMN restriction_topup BOOLEAN DEFAULT 0 NOT NULL')
                    )
                elif db_type == 'postgresql' or db_type == 'mysql':
                    await conn.execute(
                        text('ALTER TABLE users ADD COLUMN restriction_topup BOOLEAN DEFAULT FALSE NOT NULL')
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД: {db_type}')
                    return False
                logger.info('✅ Добавлена колонка users.restriction_topup')

            if not col_sub:
                if db_type == 'sqlite':
                    await conn.execute(
                        text('ALTER TABLE users ADD COLUMN restriction_subscription BOOLEAN DEFAULT 0 NOT NULL')
                    )
                elif db_type == 'postgresql' or db_type == 'mysql':
                    await conn.execute(
                        text('ALTER TABLE users ADD COLUMN restriction_subscription BOOLEAN DEFAULT FALSE NOT NULL')
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД: {db_type}')
                    return False
                logger.info('✅ Добавлена колонка users.restriction_subscription')

            if not col_reason:
                if db_type == 'sqlite' or db_type == 'postgresql' or db_type == 'mysql':
                    await conn.execute(text('ALTER TABLE users ADD COLUMN restriction_reason VARCHAR(500) NULL'))
                else:
                    logger.error(f'Неподдерживаемый тип БД: {db_type}')
                    return False
                logger.info('✅ Добавлена колонка users.restriction_reason')

            return True

    except Exception as e:
        logger.error(f'Ошибка добавления колонок ограничений пользователей: {e}')
        return False


async def add_user_cabinet_columns() -> bool:
    """Add cabinet (personal account) columns to users table."""
    cabinet_columns = [
        ('email', 'VARCHAR(255)', 'VARCHAR(255)', 'VARCHAR(255)'),
        ('email_verified', 'BOOLEAN DEFAULT 0', 'BOOLEAN DEFAULT FALSE', 'TINYINT(1) DEFAULT 0'),
        ('email_verified_at', 'DATETIME', 'TIMESTAMP', 'DATETIME'),
        ('password_hash', 'VARCHAR(255)', 'VARCHAR(255)', 'VARCHAR(255)'),
        ('email_verification_token', 'VARCHAR(255)', 'VARCHAR(255)', 'VARCHAR(255)'),
        ('email_verification_expires', 'DATETIME', 'TIMESTAMP', 'DATETIME'),
        ('password_reset_token', 'VARCHAR(255)', 'VARCHAR(255)', 'VARCHAR(255)'),
        ('password_reset_expires', 'DATETIME', 'TIMESTAMP', 'DATETIME'),
        ('cabinet_last_login', 'DATETIME', 'TIMESTAMP', 'DATETIME'),
        # Email change fields
        ('email_change_new', 'VARCHAR(255)', 'VARCHAR(255)', 'VARCHAR(255)'),
        ('email_change_code', 'VARCHAR(6)', 'VARCHAR(6)', 'VARCHAR(6)'),
        ('email_change_expires', 'DATETIME', 'TIMESTAMP', 'DATETIME'),
    ]

    try:
        db_type = await get_database_type()
        added_count = 0

        for col_name, sqlite_type, pg_type, mysql_type in cabinet_columns:
            if await check_column_exists('users', col_name):
                continue

            async with engine.begin() as conn:
                if db_type == 'sqlite':
                    col_type = sqlite_type
                elif db_type == 'postgresql':
                    col_type = pg_type
                else:
                    col_type = mysql_type

                await conn.execute(text(f'ALTER TABLE users ADD COLUMN {col_name} {col_type}'))
                added_count += 1
                logger.info(f'✅ Добавлена колонка users.{col_name}')

        if added_count == 0:
            logger.info('ℹ️ Все колонки cabinet уже существуют в таблице users')
        else:
            logger.info(f'✅ Добавлено {added_count} колонок cabinet в таблицу users')

        return True

    except Exception as e:
        logger.error(f'Ошибка добавления колонок cabinet: {e}')
        return False


async def add_subscription_crypto_link_column() -> bool:
    column_exists = await check_column_exists('subscriptions', 'subscription_crypto_link')
    if column_exists:
        logger.info('ℹ️ Колонка subscription_crypto_link уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN subscription_crypto_link TEXT'))
            elif db_type == 'postgresql':
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN subscription_crypto_link VARCHAR'))
            elif db_type == 'mysql':
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN subscription_crypto_link VARCHAR(512)'))
            else:
                logger.error(f'Неподдерживаемый тип БД для добавления subscription_crypto_link: {db_type}')
                return False

            await conn.execute(
                text(
                    'UPDATE subscriptions SET subscription_crypto_link = subscription_url '
                    "WHERE subscription_crypto_link IS NULL OR subscription_crypto_link = ''"
                )
            )

        logger.info('✅ Добавлена колонка subscription_crypto_link в таблицу subscriptions')
        return True
    except Exception as e:
        logger.error(f'Ошибка добавления колонки subscription_crypto_link: {e}')
        return False


async def fix_foreign_keys_for_user_deletion():
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'postgresql':
                try:
                    await conn.execute(
                        text("""
                        ALTER TABLE user_messages
                        DROP CONSTRAINT IF EXISTS user_messages_created_by_fkey;
                    """)
                    )

                    await conn.execute(
                        text("""
                        ALTER TABLE user_messages
                        ADD CONSTRAINT user_messages_created_by_fkey
                        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL;
                    """)
                    )
                    logger.info('Обновлен внешний ключ user_messages.created_by')
                except Exception as e:
                    logger.warning(f'Ошибка обновления FK user_messages: {e}')

                try:
                    await conn.execute(
                        text("""
                        ALTER TABLE promocodes
                        DROP CONSTRAINT IF EXISTS promocodes_created_by_fkey;
                    """)
                    )

                    await conn.execute(
                        text("""
                        ALTER TABLE promocodes
                        ADD CONSTRAINT promocodes_created_by_fkey
                        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL;
                    """)
                    )
                    logger.info('Обновлен внешний ключ promocodes.created_by')
                except Exception as e:
                    logger.warning(f'Ошибка обновления FK promocodes: {e}')

            logger.info('Внешние ключи обновлены для безопасного удаления пользователей')
            return True

    except Exception as e:
        logger.error(f'Ошибка обновления внешних ключей: {e}')
        return False


async def add_referral_commission_percent_column() -> bool:
    column_exists = await check_column_exists('users', 'referral_commission_percent')
    if column_exists:
        logger.info('ℹ️ Колонка referral_commission_percent уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite' or db_type == 'postgresql':
                alter_sql = 'ALTER TABLE users ADD COLUMN referral_commission_percent INTEGER NULL'
            elif db_type == 'mysql':
                alter_sql = 'ALTER TABLE users ADD COLUMN referral_commission_percent INT NULL'
            else:
                logger.error(f'Неподдерживаемый тип БД для добавления referral_commission_percent: {db_type}')
                return False

            await conn.execute(text(alter_sql))
            logger.info('✅ Добавлена колонка referral_commission_percent в таблицу users')
            return True

    except Exception as error:
        logger.error(f'Ошибка добавления referral_commission_percent: {error}')
        return False


async def add_referral_system_columns():
    logger.info('=== МИГРАЦИЯ РЕФЕРАЛЬНОЙ СИСТЕМЫ ===')

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            column_exists = await check_column_exists('users', 'has_made_first_topup')

            if not column_exists:
                logger.info('Добавление колонки has_made_first_topup в таблицу users')

                if db_type == 'sqlite':
                    column_def = 'BOOLEAN DEFAULT 0'
                else:
                    column_def = 'BOOLEAN DEFAULT FALSE'

                await conn.execute(text(f'ALTER TABLE users ADD COLUMN has_made_first_topup {column_def}'))
                logger.info('Колонка has_made_first_topup успешно добавлена')

                logger.info('Обновление существующих пользователей...')

                if db_type == 'sqlite':
                    update_sql = """
                        UPDATE users
                        SET has_made_first_topup = 1
                        WHERE balance_kopeks > 0 OR has_had_paid_subscription = 1
                    """
                else:
                    update_sql = """
                        UPDATE users
                        SET has_made_first_topup = TRUE
                        WHERE balance_kopeks > 0 OR has_had_paid_subscription = TRUE
                    """

                result = await conn.execute(text(update_sql))
                updated_count = result.rowcount

                logger.info(f'Обновлено {updated_count} пользователей с has_made_first_topup = TRUE')
                logger.info('✅ Миграция реферальной системы завершена')

                return True
            logger.info('Колонка has_made_first_topup уже существует')
            return True

    except Exception as e:
        logger.error(f'Ошибка миграции реферальной системы: {e}')
        return False


async def create_subscription_conversions_table():
    table_exists = await check_table_exists('subscription_conversions')
    if table_exists:
        logger.info('Таблица subscription_conversions уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE subscription_conversions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    converted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    trial_duration_days INTEGER NULL,
                    payment_method VARCHAR(50) NULL,
                    first_payment_amount_kopeks INTEGER NULL,
                    first_paid_period_days INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE INDEX idx_subscription_conversions_user_id ON subscription_conversions(user_id);
                CREATE INDEX idx_subscription_conversions_converted_at ON subscription_conversions(converted_at);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE subscription_conversions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    converted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trial_duration_days INTEGER NULL,
                    payment_method VARCHAR(50) NULL,
                    first_payment_amount_kopeks INTEGER NULL,
                    first_paid_period_days INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE INDEX idx_subscription_conversions_user_id ON subscription_conversions(user_id);
                CREATE INDEX idx_subscription_conversions_converted_at ON subscription_conversions(converted_at);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE subscription_conversions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    converted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    trial_duration_days INT NULL,
                    payment_method VARCHAR(50) NULL,
                    first_payment_amount_kopeks INT NULL,
                    first_paid_period_days INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE INDEX idx_subscription_conversions_user_id ON subscription_conversions(user_id);
                CREATE INDEX idx_subscription_conversions_converted_at ON subscription_conversions(converted_at);
                """
            else:
                logger.error(f'Неподдерживаемый тип БД для создания таблицы: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица subscription_conversions успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы subscription_conversions: {e}')
        return False


async def create_subscription_events_table():
    table_exists = await check_table_exists('subscription_events')
    if table_exists:
        logger.info('Таблица subscription_events уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE subscription_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type VARCHAR(50) NOT NULL,
                    user_id INTEGER NOT NULL,
                    subscription_id INTEGER NULL,
                    transaction_id INTEGER NULL,
                    amount_kopeks INTEGER NULL,
                    currency VARCHAR(16) NULL,
                    message TEXT NULL,
                    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    extra JSON NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL,
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_subscription_events_event_type ON subscription_events(event_type);
                CREATE INDEX ix_subscription_events_user_id ON subscription_events(user_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE subscription_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    subscription_id INTEGER NULL REFERENCES subscriptions(id) ON DELETE SET NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id) ON DELETE SET NULL,
                    amount_kopeks INTEGER NULL,
                    currency VARCHAR(16) NULL,
                    message TEXT NULL,
                    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    extra JSON NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX ix_subscription_events_event_type ON subscription_events(event_type);
                CREATE INDEX ix_subscription_events_user_id ON subscription_events(user_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE subscription_events (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    user_id INT NOT NULL,
                    subscription_id INT NULL,
                    transaction_id INT NULL,
                    amount_kopeks INT NULL,
                    currency VARCHAR(16) NULL,
                    message TEXT NULL,
                    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    extra JSON NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL,
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_subscription_events_event_type ON subscription_events(event_type);
                CREATE INDEX ix_subscription_events_user_id ON subscription_events(user_id);
                """
            else:
                logger.error(f'Неподдерживаемый тип БД для создания таблицы subscription_events: {db_type}')
                return False

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица subscription_events успешно создана')
            return True

    except Exception as e:
        logger.error(f'Ошибка создания таблицы subscription_events: {e}')
        return False


async def fix_subscription_duplicates_universal():
    async with engine.begin() as conn:
        db_type = await get_database_type()
        logger.info(f'Обнаружен тип базы данных: {db_type}')

        try:
            result = await conn.execute(
                text("""
                SELECT user_id, COUNT(*) as count
                FROM subscriptions
                GROUP BY user_id
                HAVING COUNT(*) > 1
            """)
            )

            duplicates = result.fetchall()

            if not duplicates:
                logger.info('Дублирующихся подписок не найдено')
                return 0

            logger.info(f'Найдено {len(duplicates)} пользователей с дублирующимися подписками')

            total_deleted = 0

            for user_id_row, count in duplicates:
                user_id = user_id_row

                if db_type == 'sqlite':
                    delete_result = await conn.execute(
                        text("""
                        DELETE FROM subscriptions
                        WHERE user_id = :user_id AND id NOT IN (
                            SELECT MAX(id)
                            FROM subscriptions
                            WHERE user_id = :user_id
                        )
                    """),
                        {'user_id': user_id},
                    )

                elif db_type in ['postgresql', 'mysql']:
                    delete_result = await conn.execute(
                        text("""
                        DELETE FROM subscriptions
                        WHERE user_id = :user_id AND id NOT IN (
                            SELECT max_id FROM (
                                SELECT MAX(id) as max_id
                                FROM subscriptions
                                WHERE user_id = :user_id
                            ) as subquery
                        )
                    """),
                        {'user_id': user_id},
                    )

                else:
                    subs_result = await conn.execute(
                        text("""
                        SELECT id FROM subscriptions
                        WHERE user_id = :user_id
                        ORDER BY created_at DESC, id DESC
                    """),
                        {'user_id': user_id},
                    )

                    sub_ids = [row[0] for row in subs_result.fetchall()]

                    if len(sub_ids) > 1:
                        ids_to_delete = sub_ids[1:]
                        for sub_id in ids_to_delete:
                            await conn.execute(
                                text("""
                                DELETE FROM subscriptions WHERE id = :id
                            """),
                                {'id': sub_id},
                            )
                        delete_result = type('Result', (), {'rowcount': len(ids_to_delete)})()
                    else:
                        delete_result = type('Result', (), {'rowcount': 0})()

                deleted_count = delete_result.rowcount
                total_deleted += deleted_count
                logger.info(f'Удалено {deleted_count} дублирующихся подписок для пользователя {user_id}')

            logger.info(f'Всего удалено дублирующихся подписок: {total_deleted}')
            return total_deleted

        except Exception as e:
            logger.error(f'Ошибка при очистке дублирующихся подписок: {e}')
            raise


async def ensure_server_promo_groups_setup() -> bool:
    logger.info('=== НАСТРОЙКА ДОСТУПА СЕРВЕРОВ К ПРОМОГРУППАМ ===')

    try:
        table_exists = await check_table_exists('server_squad_promo_groups')

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not table_exists:
                if db_type == 'sqlite':
                    create_table_sql = """
                    CREATE TABLE server_squad_promo_groups (
                        server_squad_id INTEGER NOT NULL,
                        promo_group_id INTEGER NOT NULL,
                        PRIMARY KEY (server_squad_id, promo_group_id),
                        FOREIGN KEY (server_squad_id) REFERENCES server_squads(id) ON DELETE CASCADE,
                        FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                    );
                    """
                    create_index_sql = """
                    CREATE INDEX IF NOT EXISTS idx_server_squad_promo_groups_promo ON server_squad_promo_groups(promo_group_id);
                    """
                elif db_type == 'postgresql':
                    create_table_sql = """
                    CREATE TABLE server_squad_promo_groups (
                        server_squad_id INTEGER NOT NULL REFERENCES server_squads(id) ON DELETE CASCADE,
                        promo_group_id INTEGER NOT NULL REFERENCES promo_groups(id) ON DELETE CASCADE,
                        PRIMARY KEY (server_squad_id, promo_group_id)
                    );
                    """
                    create_index_sql = """
                    CREATE INDEX IF NOT EXISTS idx_server_squad_promo_groups_promo ON server_squad_promo_groups(promo_group_id);
                    """
                else:
                    create_table_sql = """
                    CREATE TABLE server_squad_promo_groups (
                        server_squad_id INT NOT NULL,
                        promo_group_id INT NOT NULL,
                        PRIMARY KEY (server_squad_id, promo_group_id),
                        FOREIGN KEY (server_squad_id) REFERENCES server_squads(id) ON DELETE CASCADE,
                        FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                    );
                    """
                    create_index_sql = """
                    CREATE INDEX IF NOT EXISTS idx_server_squad_promo_groups_promo ON server_squad_promo_groups(promo_group_id);
                    """

                await conn.execute(text(create_table_sql))
                await conn.execute(text(create_index_sql))
                logger.info('✅ Таблица server_squad_promo_groups создана')
            else:
                logger.info('ℹ️ Таблица server_squad_promo_groups уже существует')

            default_query = (
                'SELECT id FROM promo_groups WHERE is_default IS TRUE LIMIT 1'
                if db_type == 'postgresql'
                else 'SELECT id FROM promo_groups WHERE is_default = 1 LIMIT 1'
            )
            default_result = await conn.execute(text(default_query))
            default_row = default_result.fetchone()

            if not default_row:
                logger.warning('⚠️ Не найдена базовая промогруппа для назначения серверам')
                return True

            default_group_id = default_row[0]

            servers_result = await conn.execute(text('SELECT id FROM server_squads'))
            server_ids = [row[0] for row in servers_result.fetchall()]

            assigned_count = 0
            for server_id in server_ids:
                existing = await conn.execute(
                    text('SELECT 1 FROM server_squad_promo_groups WHERE server_squad_id = :sid LIMIT 1'),
                    {'sid': server_id},
                )
                if existing.fetchone():
                    continue

                await conn.execute(
                    text('INSERT INTO server_squad_promo_groups (server_squad_id, promo_group_id) VALUES (:sid, :gid)'),
                    {'sid': server_id, 'gid': default_group_id},
                )
                assigned_count += 1

            if assigned_count:
                logger.info(f'✅ Базовая промогруппа назначена {assigned_count} серверам')
            else:
                logger.info('ℹ️ Все серверы уже имеют назначенные промогруппы')

        return True

    except Exception as e:
        logger.error(f'Ошибка настройки таблицы server_squad_promo_groups: {e}')
        return False


async def add_server_trial_flag_column() -> bool:
    column_exists = await check_column_exists('server_squads', 'is_trial_eligible')
    if column_exists:
        logger.info('Колонка is_trial_eligible уже существует в server_squads')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                column_def = 'BOOLEAN NOT NULL DEFAULT 0'
            elif db_type == 'postgresql':
                column_def = 'BOOLEAN NOT NULL DEFAULT FALSE'
            else:
                column_def = 'BOOLEAN NOT NULL DEFAULT FALSE'

            await conn.execute(text(f'ALTER TABLE server_squads ADD COLUMN is_trial_eligible {column_def}'))

            if db_type == 'postgresql':
                await conn.execute(text('ALTER TABLE server_squads ALTER COLUMN is_trial_eligible SET DEFAULT FALSE'))

        logger.info('✅ Добавлена колонка is_trial_eligible в server_squads')
        return True

    except Exception as error:
        logger.error(f'Ошибка добавления колонки is_trial_eligible: {error}')
        return False


async def create_system_settings_table() -> bool:
    table_exists = await check_table_exists('system_settings')
    if table_exists:
        logger.info('ℹ️ Таблица system_settings уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE system_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key VARCHAR(255) NOT NULL UNIQUE,
                    value TEXT NULL,
                    description TEXT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE system_settings (
                    id SERIAL PRIMARY KEY,
                    key VARCHAR(255) NOT NULL UNIQUE,
                    value TEXT NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            else:
                create_sql = """
                CREATE TABLE system_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    key VARCHAR(255) NOT NULL UNIQUE,
                    value TEXT NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица system_settings создана')
            return True

    except Exception as error:
        logger.error(f'Ошибка создания таблицы system_settings: {error}')
        return False


async def create_menu_layout_history_table() -> bool:
    """Создаёт таблицу для хранения истории изменений конфигурации меню."""
    table_exists = await check_table_exists('menu_layout_history')
    if table_exists:
        logger.info('ℹ️ Таблица menu_layout_history уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_table_sql = """
                CREATE TABLE menu_layout_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_json TEXT NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    changes_summary TEXT NULL,
                    user_info VARCHAR(255) NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            elif db_type == 'postgresql':
                create_table_sql = """
                CREATE TABLE menu_layout_history (
                    id SERIAL PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    changes_summary TEXT NULL,
                    user_info VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            else:
                create_table_sql = """
                CREATE TABLE menu_layout_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    changes_summary TEXT NULL,
                    user_info VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB
                """

            await conn.execute(text(create_table_sql))
            await conn.execute(text('CREATE INDEX ix_menu_layout_history_created ON menu_layout_history(created_at)'))
            logger.info('✅ Таблица menu_layout_history создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы menu_layout_history: {error}')
        return False


async def create_button_click_logs_table() -> bool:
    """Создаёт таблицу для логирования кликов по кнопкам меню."""
    table_exists = await check_table_exists('button_click_logs')
    if table_exists:
        logger.info('ℹ️ Таблица button_click_logs уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_table_sql = """
                CREATE TABLE button_click_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    button_id VARCHAR(100) NOT NULL,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    callback_data VARCHAR(255) NULL,
                    clicked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    button_type VARCHAR(20) NULL,
                    button_text VARCHAR(255) NULL
                )
                """
            elif db_type == 'postgresql':
                create_table_sql = """
                CREATE TABLE button_click_logs (
                    id SERIAL PRIMARY KEY,
                    button_id VARCHAR(100) NOT NULL,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    callback_data VARCHAR(255) NULL,
                    clicked_at TIMESTAMP DEFAULT NOW(),
                    button_type VARCHAR(20) NULL,
                    button_text VARCHAR(255) NULL
                )
                """
            else:
                create_table_sql = """
                CREATE TABLE button_click_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    button_id VARCHAR(100) NOT NULL,
                    user_id INTEGER NULL,
                    callback_data VARCHAR(255) NULL,
                    clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    button_type VARCHAR(20) NULL,
                    button_text VARCHAR(255) NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                ) ENGINE=InnoDB
                """

            await conn.execute(text(create_table_sql))

            # Создаём индексы отдельными запросами
            index_statements = [
                'CREATE INDEX ix_button_click_logs_button_id ON button_click_logs(button_id)',
                'CREATE INDEX ix_button_click_logs_user_id ON button_click_logs(user_id)',
                'CREATE INDEX ix_button_click_logs_clicked_at ON button_click_logs(clicked_at)',
                'CREATE INDEX ix_button_click_logs_button_date ON button_click_logs(button_id, clicked_at)',
                'CREATE INDEX ix_button_click_logs_user_date ON button_click_logs(user_id, clicked_at)',
            ]
            for stmt in index_statements:
                await conn.execute(text(stmt))

            logger.info('✅ Таблица button_click_logs создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы button_click_logs: {error}')
        return False


async def fix_button_click_logs_fk() -> bool:
    """Исправляет FK button_click_logs.user_id: users(telegram_id) -> users(id)."""
    table_exists = await check_table_exists('button_click_logs')
    if not table_exists:
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'postgresql':
                # Проверяем, ссылается ли FK на telegram_id (ошибочный вариант)
                check_sql = text("""
                    SELECT ccu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.constraint_column_usage ccu
                        ON tc.constraint_name = ccu.constraint_name
                    WHERE tc.table_name = 'button_click_logs'
                        AND tc.constraint_type = 'FOREIGN KEY'
                        AND ccu.table_name = 'users'
                    LIMIT 1
                """)
                result = await conn.execute(check_sql)
                row = result.fetchone()

                if row and row[0] == 'telegram_id':
                    logger.info('🔧 Исправляем FK button_click_logs.user_id: telegram_id -> id')

                    # Обнуляем невалидные user_id (которые были internal id, а не telegram_id)
                    await conn.execute(
                        text("""
                        UPDATE button_click_logs
                        SET user_id = NULL
                        WHERE user_id IS NOT NULL
                          AND user_id NOT IN (SELECT telegram_id FROM users)
                    """)
                    )

                    # Удаляем старый FK
                    await conn.execute(
                        text('ALTER TABLE button_click_logs DROP CONSTRAINT IF EXISTS button_click_logs_user_id_fkey')
                    )

                    # Меняем тип колонки и добавляем правильный FK
                    await conn.execute(text('ALTER TABLE button_click_logs ALTER COLUMN user_id TYPE INTEGER'))

                    # Обнуляем все значения, т.к. они были записаны неправильно
                    await conn.execute(text('UPDATE button_click_logs SET user_id = NULL'))

                    await conn.execute(
                        text(
                            'ALTER TABLE button_click_logs '
                            'ADD CONSTRAINT button_click_logs_user_id_fkey '
                            'FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL'
                        )
                    )

                    logger.info('✅ FK button_click_logs.user_id исправлен')
                else:
                    logger.debug('FK button_click_logs.user_id уже корректен')

            return True

    except Exception as error:
        logger.error(f'❌ Ошибка исправления FK button_click_logs: {error}')
        return False


async def create_web_api_tokens_table() -> bool:
    table_exists = await check_table_exists('web_api_tokens')
    if table_exists:
        logger.info('ℹ️ Таблица web_api_tokens уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE web_api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL,
                    token_hash VARCHAR(128) NOT NULL UNIQUE,
                    token_prefix VARCHAR(32) NOT NULL,
                    description TEXT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NULL,
                    last_used_at DATETIME NULL,
                    last_used_ip VARCHAR(64) NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_by VARCHAR(255) NULL
                );
                CREATE INDEX idx_web_api_tokens_active ON web_api_tokens(is_active);
                CREATE INDEX idx_web_api_tokens_prefix ON web_api_tokens(token_prefix);
                CREATE INDEX idx_web_api_tokens_last_used ON web_api_tokens(last_used_at);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE web_api_tokens (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    token_hash VARCHAR(128) NOT NULL UNIQUE,
                    token_prefix VARCHAR(32) NOT NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    expires_at TIMESTAMP NULL,
                    last_used_at TIMESTAMP NULL,
                    last_used_ip VARCHAR(64) NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by VARCHAR(255) NULL
                );
                CREATE INDEX idx_web_api_tokens_active ON web_api_tokens(is_active);
                CREATE INDEX idx_web_api_tokens_prefix ON web_api_tokens(token_prefix);
                CREATE INDEX idx_web_api_tokens_last_used ON web_api_tokens(last_used_at);
                """
            else:
                create_sql = """
                CREATE TABLE web_api_tokens (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    token_hash VARCHAR(128) NOT NULL UNIQUE,
                    token_prefix VARCHAR(32) NOT NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NULL,
                    last_used_at TIMESTAMP NULL,
                    last_used_ip VARCHAR(64) NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by VARCHAR(255) NULL
                ) ENGINE=InnoDB;
                CREATE INDEX idx_web_api_tokens_active ON web_api_tokens(is_active);
                CREATE INDEX idx_web_api_tokens_prefix ON web_api_tokens(token_prefix);
                CREATE INDEX idx_web_api_tokens_last_used ON web_api_tokens(last_used_at);
                """

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица web_api_tokens создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы web_api_tokens: {error}')
        return False


async def create_privacy_policies_table() -> bool:
    table_exists = await check_table_exists('privacy_policies')
    if table_exists:
        logger.info('ℹ️ Таблица privacy_policies уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE privacy_policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE privacy_policies (
                    id SERIAL PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            else:
                create_sql = """
                CREATE TABLE privacy_policies (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица privacy_policies создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы privacy_policies: {error}')
        return False


async def create_public_offers_table() -> bool:
    table_exists = await check_table_exists('public_offers')
    if table_exists:
        logger.info('ℹ️ Таблица public_offers уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE public_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE public_offers (
                    id SERIAL PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            else:
                create_sql = """
                CREATE TABLE public_offers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица public_offers создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы public_offers: {error}')
        return False


async def create_faq_settings_table() -> bool:
    table_exists = await check_table_exists('faq_settings')
    if table_exists:
        logger.info('ℹ️ Таблица faq_settings уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE faq_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    is_enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE faq_settings (
                    id SERIAL PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            else:
                create_sql = """
                CREATE TABLE faq_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица faq_settings создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы faq_settings: {error}')
        return False


async def create_faq_pages_table() -> bool:
    table_exists = await check_table_exists('faq_pages')
    if table_exists:
        logger.info('ℹ️ Таблица faq_pages уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE faq_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    language VARCHAR(10) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_faq_pages_language ON faq_pages(language);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE faq_pages (
                    id SERIAL PRIMARY KEY,
                    language VARCHAR(10) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX idx_faq_pages_language ON faq_pages(language);
                CREATE INDEX idx_faq_pages_order ON faq_pages(language, display_order);
                """
            else:
                create_sql = """
                CREATE TABLE faq_pages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    display_order INT NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                CREATE INDEX idx_faq_pages_language ON faq_pages(language);
                CREATE INDEX idx_faq_pages_order ON faq_pages(language, display_order);
                """

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица faq_pages создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы faq_pages: {error}')
        return False


async def ensure_default_web_api_token() -> bool:
    default_token = (settings.WEB_API_DEFAULT_TOKEN or '').strip()
    if not default_token:
        return True

    token_name = (settings.WEB_API_DEFAULT_TOKEN_NAME or 'Bootstrap Token').strip()

    try:
        async with AsyncSessionLocal() as session:
            token_hash = hash_api_token(default_token, settings.WEB_API_TOKEN_HASH_ALGORITHM)
            result = await session.execute(select(WebApiToken).where(WebApiToken.token_hash == token_hash))
            existing = result.scalar_one_or_none()

            if existing:
                updated = False

                if not existing.is_active:
                    existing.is_active = True
                    updated = True

                if token_name and existing.name != token_name:
                    existing.name = token_name
                    updated = True

                if updated:
                    existing.updated_at = datetime.utcnow()
                    await session.commit()
                return True

            token = WebApiToken(
                name=token_name or 'Bootstrap Token',
                token_hash=token_hash,
                token_prefix=default_token[:12],
                description='Автоматически создан при миграции',
                created_by='migration',
                is_active=True,
            )
            session.add(token)
            await session.commit()
            logger.info('✅ Создан дефолтный токен веб-API из конфигурации')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания дефолтного веб-API токена: {error}')
        return False


async def add_promo_group_priority_column() -> bool:
    """Добавляет колонку priority в таблицу promo_groups."""
    column_exists = await check_column_exists('promo_groups', 'priority')
    if column_exists:
        logger.info('Колонка priority уже существует в promo_groups')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite' or db_type == 'postgresql':
                column_def = 'INTEGER NOT NULL DEFAULT 0'
            else:
                column_def = 'INT NOT NULL DEFAULT 0'

            await conn.execute(text(f'ALTER TABLE promo_groups ADD COLUMN priority {column_def}'))

            # Создаем индекс для оптимизации сортировки
            if db_type == 'postgresql' or db_type == 'sqlite':
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS idx_promo_groups_priority ON promo_groups(priority DESC)')
                )
            else:  # MySQL
                await conn.execute(text('CREATE INDEX idx_promo_groups_priority ON promo_groups(priority DESC)'))

        logger.info('✅ Добавлена колонка priority в promo_groups с индексом')
        return True

    except Exception as error:
        logger.error(f'Ошибка добавления колонки priority: {error}')
        return False


async def create_user_promo_groups_table() -> bool:
    """Создает таблицу user_promo_groups для связи Many-to-Many между users и promo_groups."""
    table_exists = await check_table_exists('user_promo_groups')
    if table_exists:
        logger.info('ℹ️ Таблица user_promo_groups уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE user_promo_groups (
                    user_id INTEGER NOT NULL,
                    promo_group_id INTEGER NOT NULL,
                    assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    assigned_by VARCHAR(50) DEFAULT 'system',
                    PRIMARY KEY (user_id, promo_group_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                );
                """
                index_sql = 'CREATE INDEX idx_user_promo_groups_user_id ON user_promo_groups(user_id);'
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE user_promo_groups (
                    user_id INTEGER NOT NULL,
                    promo_group_id INTEGER NOT NULL,
                    assigned_at TIMESTAMP DEFAULT NOW(),
                    assigned_by VARCHAR(50) DEFAULT 'system',
                    PRIMARY KEY (user_id, promo_group_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                );
                """
                index_sql = 'CREATE INDEX idx_user_promo_groups_user_id ON user_promo_groups(user_id);'
            else:  # MySQL
                create_sql = """
                CREATE TABLE user_promo_groups (
                    user_id INT NOT NULL,
                    promo_group_id INT NOT NULL,
                    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    assigned_by VARCHAR(50) DEFAULT 'system',
                    PRIMARY KEY (user_id, promo_group_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                );
                """
                index_sql = 'CREATE INDEX idx_user_promo_groups_user_id ON user_promo_groups(user_id);'

            await conn.execute(text(create_sql))
            await conn.execute(text(index_sql))
            logger.info('✅ Таблица user_promo_groups создана с индексом')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы user_promo_groups: {error}')
        return False


async def migrate_existing_user_promo_groups_data() -> bool:
    """Переносит существующие связи users.promo_group_id в таблицу user_promo_groups."""
    try:
        table_exists = await check_table_exists('user_promo_groups')
        if not table_exists:
            logger.warning('⚠️ Таблица user_promo_groups не существует, пропускаем миграцию данных')
            return False

        column_exists = await check_column_exists('users', 'promo_group_id')
        if not column_exists:
            logger.warning('⚠️ Колонка users.promo_group_id не существует, пропускаем миграцию данных')
            return True

        async with engine.begin() as conn:
            # Проверяем есть ли уже данные в user_promo_groups
            result = await conn.execute(text('SELECT COUNT(*) FROM user_promo_groups'))
            count = result.scalar()

            if count > 0:
                logger.info(f'ℹ️ В таблице user_promo_groups уже есть {count} записей, пропускаем миграцию')
                return True

            # Переносим данные из users.promo_group_id
            db_type = await get_database_type()

            if db_type == 'sqlite':
                migrate_sql = """
                INSERT INTO user_promo_groups (user_id, promo_group_id, assigned_at, assigned_by)
                SELECT id, promo_group_id, CURRENT_TIMESTAMP, 'system'
                FROM users
                WHERE promo_group_id IS NOT NULL
                """
            else:  # PostgreSQL and MySQL
                migrate_sql = """
                INSERT INTO user_promo_groups (user_id, promo_group_id, assigned_at, assigned_by)
                SELECT id, promo_group_id, NOW(), 'system'
                FROM users
                WHERE promo_group_id IS NOT NULL
                """

            result = await conn.execute(text(migrate_sql))
            migrated_count = result.rowcount if hasattr(result, 'rowcount') else 0

            logger.info(f'✅ Перенесено {migrated_count} связей пользователей с промогруппами')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка миграции данных user_promo_groups: {error}')
        return False


async def add_promocode_promo_group_column() -> bool:
    """Добавляет колонку promo_group_id в таблицу promocodes."""
    column_exists = await check_column_exists('promocodes', 'promo_group_id')
    if column_exists:
        logger.info('Колонка promo_group_id уже существует в promocodes')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            # Add column
            if db_type == 'sqlite':
                await conn.execute(text('ALTER TABLE promocodes ADD COLUMN promo_group_id INTEGER'))
            elif db_type == 'postgresql':
                await conn.execute(text('ALTER TABLE promocodes ADD COLUMN promo_group_id INTEGER'))
                # Add foreign key
                await conn.execute(
                    text("""
                        ALTER TABLE promocodes
                        ADD CONSTRAINT fk_promocodes_promo_group
                        FOREIGN KEY (promo_group_id)
                        REFERENCES promo_groups(id)
                        ON DELETE SET NULL
                    """)
                )
                # Add index
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS idx_promocodes_promo_group_id ON promocodes(promo_group_id)')
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                        ALTER TABLE promocodes
                        ADD COLUMN promo_group_id INT,
                        ADD CONSTRAINT fk_promocodes_promo_group
                        FOREIGN KEY (promo_group_id)
                        REFERENCES promo_groups(id)
                        ON DELETE SET NULL
                    """)
                )
                await conn.execute(text('CREATE INDEX idx_promocodes_promo_group_id ON promocodes(promo_group_id)'))

        logger.info('✅ Добавлена колонка promo_group_id в promocodes')
        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления promo_group_id в promocodes: {error}')
        return False


async def add_promocode_first_purchase_only_column() -> bool:
    """Добавляет колонку first_purchase_only в таблицу promocodes."""
    column_exists = await check_column_exists('promocodes', 'first_purchase_only')
    if column_exists:
        logger.info('Колонка first_purchase_only уже существует в promocodes')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(text('ALTER TABLE promocodes ADD COLUMN first_purchase_only BOOLEAN DEFAULT 0'))
            elif db_type == 'postgresql' or db_type == 'mysql':
                await conn.execute(text('ALTER TABLE promocodes ADD COLUMN first_purchase_only BOOLEAN DEFAULT FALSE'))

        logger.info('✅ Добавлена колонка first_purchase_only в promocodes')
        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления first_purchase_only в promocodes: {error}')
        return False


async def migrate_contest_templates_prize_columns() -> bool:
    """Миграция contest_templates: prize_days -> prize_type + prize_value."""
    try:
        prize_type_exists = await check_column_exists('contest_templates', 'prize_type')
        prize_value_exists = await check_column_exists('contest_templates', 'prize_value')

        if prize_type_exists and prize_value_exists:
            logger.info('Колонки prize_type и prize_value уже существуют в contest_templates')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            # Добавляем prize_type
            if not prize_type_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(
                        text("ALTER TABLE contest_templates ADD COLUMN prize_type VARCHAR(20) NOT NULL DEFAULT 'days'")
                    )
                else:
                    await conn.execute(
                        text("ALTER TABLE contest_templates ADD COLUMN prize_type VARCHAR(20) NOT NULL DEFAULT 'days'")
                    )
                logger.info('✅ Добавлена колонка prize_type в contest_templates')

            # Добавляем prize_value
            if not prize_value_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(
                        text("ALTER TABLE contest_templates ADD COLUMN prize_value VARCHAR(50) NOT NULL DEFAULT '1'")
                    )
                else:
                    await conn.execute(
                        text("ALTER TABLE contest_templates ADD COLUMN prize_value VARCHAR(50) NOT NULL DEFAULT '1'")
                    )
                logger.info('✅ Добавлена колонка prize_value в contest_templates')

            # Мигрируем данные из prize_days в prize_value (если prize_days существует)
            prize_days_exists = await check_column_exists('contest_templates', 'prize_days')
            if prize_days_exists:
                await conn.execute(
                    text(
                        "UPDATE contest_templates SET prize_value = CAST(prize_days AS VARCHAR) WHERE prize_type = 'days'"
                    )
                )
                logger.info('✅ Данные из prize_days перенесены в prize_value')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка миграции prize_type/prize_value в contest_templates: {error}')
        return False


async def add_subscription_modem_enabled_column() -> bool:
    """Добавить колонку modem_enabled в subscriptions."""
    try:
        column_exists = await check_column_exists('subscriptions', 'modem_enabled')
        if column_exists:
            logger.info('Колонка modem_enabled уже существует в subscriptions')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN modem_enabled BOOLEAN DEFAULT 0'))
            elif db_type == 'postgresql':
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN modem_enabled BOOLEAN DEFAULT FALSE'))
            else:
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN modem_enabled TINYINT(1) DEFAULT 0'))

        logger.info('✅ Добавлена колонка modem_enabled в subscriptions')
        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления modem_enabled в subscriptions: {error}')
        return False


async def add_subscription_purchased_traffic_column() -> bool:
    """Добавить колонку purchased_traffic_gb в subscriptions."""
    try:
        column_exists = await check_column_exists('subscriptions', 'purchased_traffic_gb')
        if column_exists:
            logger.info('Колонка purchased_traffic_gb уже существует в subscriptions')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite' or db_type == 'postgresql':
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN purchased_traffic_gb INTEGER DEFAULT 0'))
            else:
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN purchased_traffic_gb INT DEFAULT 0'))

        logger.info('✅ Добавлена колонка purchased_traffic_gb в subscriptions')
        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления purchased_traffic_gb в subscriptions: {error}')
        return False


async def add_transaction_receipt_columns() -> bool:
    """Добавить колонки receipt_uuid и receipt_created_at в transactions."""
    try:
        receipt_uuid_exists = await check_column_exists('transactions', 'receipt_uuid')
        receipt_created_at_exists = await check_column_exists('transactions', 'receipt_created_at')

        if receipt_uuid_exists and receipt_created_at_exists:
            logger.info('Колонки receipt_uuid и receipt_created_at уже существуют в transactions')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not receipt_uuid_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE transactions ADD COLUMN receipt_uuid VARCHAR(255)'))
                else:
                    await conn.execute(text('ALTER TABLE transactions ADD COLUMN receipt_uuid VARCHAR(255)'))
                logger.info('✅ Добавлена колонка receipt_uuid в transactions')

            if not receipt_created_at_exists:
                if db_type == 'sqlite':
                    await conn.execute(text('ALTER TABLE transactions ADD COLUMN receipt_created_at DATETIME'))
                elif db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE transactions ADD COLUMN receipt_created_at TIMESTAMP'))
                else:
                    await conn.execute(text('ALTER TABLE transactions ADD COLUMN receipt_created_at DATETIME'))
                logger.info('✅ Добавлена колонка receipt_created_at в transactions')

        # Создаём индекс на receipt_uuid
        try:
            async with engine.begin() as conn:
                db_type = await get_database_type()
                if db_type == 'postgresql' or db_type == 'sqlite':
                    await conn.execute(
                        text('CREATE INDEX IF NOT EXISTS ix_transactions_receipt_uuid ON transactions (receipt_uuid)')
                    )
                else:
                    await conn.execute(text('CREATE INDEX ix_transactions_receipt_uuid ON transactions (receipt_uuid)'))
        except Exception as idx_error:
            logger.warning(f'Индекс на receipt_uuid возможно уже существует: {idx_error}')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонок чеков в transactions: {error}')
        return False


async def add_oauth_provider_columns() -> bool:
    """Добавить колонки OAuth провайдеров (google_id, yandex_id, discord_id, vk_id) в users."""
    try:
        google_exists = await check_column_exists('users', 'google_id')
        yandex_exists = await check_column_exists('users', 'yandex_id')
        discord_exists = await check_column_exists('users', 'discord_id')
        vk_exists = await check_column_exists('users', 'vk_id')

        if google_exists and yandex_exists and discord_exists and vk_exists:
            logger.info('Колонки OAuth провайдеров уже существуют в users')
            return True

        db_type = await get_database_type()

        async with engine.begin() as conn:
            if not google_exists:
                await conn.execute(text('ALTER TABLE users ADD COLUMN google_id VARCHAR(255)'))
                logger.info('✅ Добавлена колонка google_id в users')

            if not yandex_exists:
                await conn.execute(text('ALTER TABLE users ADD COLUMN yandex_id VARCHAR(255)'))
                logger.info('✅ Добавлена колонка yandex_id в users')

            if not discord_exists:
                await conn.execute(text('ALTER TABLE users ADD COLUMN discord_id VARCHAR(255)'))
                logger.info('✅ Добавлена колонка discord_id в users')

            if not vk_exists:
                if db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE users ADD COLUMN vk_id BIGINT'))
                else:
                    await conn.execute(text('ALTER TABLE users ADD COLUMN vk_id INTEGER'))
                logger.info('✅ Добавлена колонка vk_id в users')

        # Создаём уникальные индексы
        for col in ('google_id', 'yandex_id', 'discord_id', 'vk_id'):
            try:
                async with engine.begin() as conn:
                    if db_type in ('postgresql', 'sqlite'):
                        await conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_users_{col} ON users ({col})'))
                    else:
                        await conn.execute(text(f'CREATE UNIQUE INDEX uq_users_{col} ON users ({col})'))
            except Exception as idx_error:
                logger.warning(f'Индекс uq_users_{col} возможно уже существует: {idx_error}')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонок OAuth провайдеров в users: {error}')
        return False


async def create_withdrawal_requests_table() -> bool:
    """Создаёт таблицу для заявок на вывод реферального баланса."""
    try:
        if await check_table_exists('withdrawal_requests'):
            logger.debug('Таблица withdrawal_requests уже существует')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE withdrawal_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_kopeks INTEGER NOT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'pending',
                    payment_details TEXT,
                    risk_score INTEGER DEFAULT 0,
                    risk_analysis TEXT,
                    processed_by INTEGER,
                    processed_at DATETIME,
                    admin_comment TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (processed_by) REFERENCES users(id) ON DELETE SET NULL
                )
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE withdrawal_requests (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    amount_kopeks INTEGER NOT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'pending',
                    payment_details TEXT,
                    risk_score INTEGER DEFAULT 0,
                    risk_analysis TEXT,
                    processed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    processed_at TIMESTAMP,
                    admin_comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            else:  # mysql
                create_sql = """
                CREATE TABLE withdrawal_requests (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    amount_kopeks INT NOT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'pending',
                    payment_details TEXT,
                    risk_score INT DEFAULT 0,
                    risk_analysis TEXT,
                    processed_by INT,
                    processed_at DATETIME,
                    admin_comment TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (processed_by) REFERENCES users(id) ON DELETE SET NULL
                )
                """

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица withdrawal_requests создана')

            # Создаём индексы
            try:
                await conn.execute(text('CREATE INDEX idx_withdrawal_requests_user_id ON withdrawal_requests(user_id)'))
                await conn.execute(text('CREATE INDEX idx_withdrawal_requests_status ON withdrawal_requests(status)'))
            except Exception:
                pass  # Индексы могут уже существовать

        return True
    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы withdrawal_requests: {error}')
        return False


# =============================================================================
# МИГРАЦИЯ ДЛЯ ИНДИВИДУАЛЬНЫХ ДОКУПОК ТРАФИКА
# =============================================================================


async def create_traffic_purchases_table() -> bool:
    """Создаёт таблицу для индивидуальных докупок трафика с отдельными датами истечения."""
    try:
        if await check_table_exists('traffic_purchases'):
            logger.info('ℹ️ Таблица traffic_purchases уже существует')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE traffic_purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    traffic_gb INTEGER NOT NULL,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );
                CREATE INDEX idx_traffic_purchases_subscription_id ON traffic_purchases(subscription_id);
                CREATE INDEX idx_traffic_purchases_expires_at ON traffic_purchases(expires_at);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE traffic_purchases (
                    id SERIAL PRIMARY KEY,
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    traffic_gb INTEGER NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_traffic_purchases_subscription_id ON traffic_purchases(subscription_id);
                CREATE INDEX idx_traffic_purchases_expires_at ON traffic_purchases(expires_at);
                """
            else:  # mysql
                create_sql = """
                CREATE TABLE traffic_purchases (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    subscription_id INT NOT NULL,
                    traffic_gb INT NOT NULL,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
                    INDEX idx_traffic_purchases_subscription_id (subscription_id),
                    INDEX idx_traffic_purchases_expires_at (expires_at)
                );
                """

            await conn.execute(text(create_sql))
            logger.info('✅ Таблица traffic_purchases создана')

        return True
    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы traffic_purchases: {error}')
        return False


# =============================================================================
# МИГРАЦИИ ДЛЯ РЕЖИМА ТАРИФОВ
# =============================================================================


async def create_tariffs_table() -> bool:
    """Создаёт таблицу тарифов для режима продаж 'Тарифы'."""
    try:
        if await check_table_exists('tariffs'):
            logger.info('ℹ️ Таблица tariffs уже существует')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                CREATE TABLE tariffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL,
                    description TEXT,
                    display_order INTEGER DEFAULT 0 NOT NULL,
                    is_active BOOLEAN DEFAULT 1 NOT NULL,
                    traffic_limit_gb INTEGER DEFAULT 100 NOT NULL,
                    device_limit INTEGER DEFAULT 1 NOT NULL,
                    allowed_squads JSON DEFAULT '[]',
                    period_prices JSON DEFAULT '{}' NOT NULL,
                    tier_level INTEGER DEFAULT 1 NOT NULL,
                    is_trial_available BOOLEAN DEFAULT 0 NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """)
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                CREATE TABLE tariffs (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    description TEXT,
                    display_order INTEGER DEFAULT 0 NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE NOT NULL,
                    traffic_limit_gb INTEGER DEFAULT 100 NOT NULL,
                    device_limit INTEGER DEFAULT 1 NOT NULL,
                    allowed_squads JSON DEFAULT '[]',
                    period_prices JSON DEFAULT '{}' NOT NULL,
                    tier_level INTEGER DEFAULT 1 NOT NULL,
                    is_trial_available BOOLEAN DEFAULT FALSE NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
                """)
                )
            else:  # MySQL
                await conn.execute(
                    text("""
                CREATE TABLE tariffs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    description TEXT,
                    display_order INT DEFAULT 0 NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE NOT NULL,
                    traffic_limit_gb INT DEFAULT 100 NOT NULL,
                    device_limit INT DEFAULT 1 NOT NULL,
                    allowed_squads JSON DEFAULT (JSON_ARRAY()),
                    period_prices JSON NOT NULL,
                    tier_level INT DEFAULT 1 NOT NULL,
                    is_trial_available BOOLEAN DEFAULT FALSE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """)
                )

            logger.info('✅ Таблица tariffs создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы tariffs: {error}')
        return False


async def create_tariff_promo_groups_table() -> bool:
    """Создаёт связующую таблицу tariff_promo_groups для M2M связи тарифов и промогрупп."""
    try:
        if await check_table_exists('tariff_promo_groups'):
            logger.info('ℹ️ Таблица tariff_promo_groups уже существует')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text("""
                CREATE TABLE tariff_promo_groups (
                    tariff_id INTEGER NOT NULL,
                    promo_group_id INTEGER NOT NULL,
                    PRIMARY KEY (tariff_id, promo_group_id),
                    FOREIGN KEY (tariff_id) REFERENCES tariffs(id) ON DELETE CASCADE,
                    FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                )
                """)
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("""
                CREATE TABLE tariff_promo_groups (
                    tariff_id INTEGER NOT NULL REFERENCES tariffs(id) ON DELETE CASCADE,
                    promo_group_id INTEGER NOT NULL REFERENCES promo_groups(id) ON DELETE CASCADE,
                    PRIMARY KEY (tariff_id, promo_group_id)
                )
                """)
                )
            else:  # MySQL
                await conn.execute(
                    text("""
                CREATE TABLE tariff_promo_groups (
                    tariff_id INT NOT NULL,
                    promo_group_id INT NOT NULL,
                    PRIMARY KEY (tariff_id, promo_group_id),
                    FOREIGN KEY (tariff_id) REFERENCES tariffs(id) ON DELETE CASCADE,
                    FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                )
                """)
                )

            logger.info('✅ Таблица tariff_promo_groups создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы tariff_promo_groups: {error}')
        return False


async def ensure_tariff_max_device_limit_column() -> bool:
    """Добавляет колонку max_device_limit в таблицу tariffs."""
    try:
        column_exists = await check_column_exists('tariffs', 'max_device_limit')
        if column_exists:
            logger.info('ℹ️ Колонка max_device_limit в tariffs уже существует')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite' or db_type == 'postgresql':
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN max_device_limit INTEGER NULL'))
            else:  # MySQL
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN max_device_limit INT NULL'))

            logger.info('✅ Колонка max_device_limit добавлена в tariffs')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонки max_device_limit: {error}')
        return False


async def add_subscription_tariff_id_column() -> bool:
    """Добавляет колонку tariff_id в таблицу subscriptions."""
    try:
        if await check_column_exists('subscriptions', 'tariff_id'):
            logger.info('ℹ️ Колонка tariff_id уже существует в subscriptions')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text('ALTER TABLE subscriptions ADD COLUMN tariff_id INTEGER REFERENCES tariffs(id)')
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text(
                        'ALTER TABLE subscriptions ADD COLUMN tariff_id INTEGER REFERENCES tariffs(id) ON DELETE SET NULL'
                    )
                )
                # Создаём индекс
                await conn.execute(
                    text('CREATE INDEX IF NOT EXISTS ix_subscriptions_tariff_id ON subscriptions(tariff_id)')
                )
            else:  # MySQL
                await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN tariff_id INT NULL'))
                await conn.execute(
                    text(
                        'ALTER TABLE subscriptions ADD CONSTRAINT fk_subscriptions_tariff '
                        'FOREIGN KEY (tariff_id) REFERENCES tariffs(id) ON DELETE SET NULL'
                    )
                )
                await conn.execute(text('CREATE INDEX ix_subscriptions_tariff_id ON subscriptions(tariff_id)'))

            logger.info('✅ Колонка tariff_id добавлена в subscriptions')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонки tariff_id: {error}')
        return False


async def add_campaign_tariff_columns() -> bool:
    """Добавляет колонки tariff_id и tariff_duration_days в таблицы рекламных кампаний."""
    try:
        campaigns_tariff_id_exists = await check_column_exists('advertising_campaigns', 'tariff_id')
        campaigns_duration_exists = await check_column_exists('advertising_campaigns', 'tariff_duration_days')
        registrations_tariff_id_exists = await check_column_exists('advertising_campaign_registrations', 'tariff_id')
        registrations_duration_exists = await check_column_exists(
            'advertising_campaign_registrations', 'tariff_duration_days'
        )

        if (
            campaigns_tariff_id_exists
            and campaigns_duration_exists
            and registrations_tariff_id_exists
            and registrations_duration_exists
        ):
            logger.info('ℹ️ Колонки tariff в рекламных кампаниях уже существуют')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            # Добавляем колонки в advertising_campaigns
            if not campaigns_tariff_id_exists:
                if db_type == 'sqlite':
                    await conn.execute(
                        text('ALTER TABLE advertising_campaigns ADD COLUMN tariff_id INTEGER REFERENCES tariffs(id)')
                    )
                elif db_type == 'postgresql':
                    await conn.execute(
                        text(
                            'ALTER TABLE advertising_campaigns ADD COLUMN tariff_id INTEGER REFERENCES tariffs(id) ON DELETE SET NULL'
                        )
                    )
                else:  # MySQL
                    await conn.execute(text('ALTER TABLE advertising_campaigns ADD COLUMN tariff_id INT NULL'))
                logger.info('✅ Колонка tariff_id добавлена в advertising_campaigns')

            if not campaigns_duration_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE advertising_campaigns ADD COLUMN tariff_duration_days INTEGER NULL')
                    )
                else:  # MySQL
                    await conn.execute(
                        text('ALTER TABLE advertising_campaigns ADD COLUMN tariff_duration_days INT NULL')
                    )
                logger.info('✅ Колонка tariff_duration_days добавлена в advertising_campaigns')

            # Добавляем колонки в advertising_campaign_registrations
            if not registrations_tariff_id_exists:
                if db_type == 'sqlite':
                    await conn.execute(
                        text(
                            'ALTER TABLE advertising_campaign_registrations ADD COLUMN tariff_id INTEGER REFERENCES tariffs(id)'
                        )
                    )
                elif db_type == 'postgresql':
                    await conn.execute(
                        text(
                            'ALTER TABLE advertising_campaign_registrations ADD COLUMN tariff_id INTEGER REFERENCES tariffs(id) ON DELETE SET NULL'
                        )
                    )
                else:  # MySQL
                    await conn.execute(
                        text('ALTER TABLE advertising_campaign_registrations ADD COLUMN tariff_id INT NULL')
                    )
                logger.info('✅ Колонка tariff_id добавлена в advertising_campaign_registrations')

            if not registrations_duration_exists:
                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(
                        text(
                            'ALTER TABLE advertising_campaign_registrations ADD COLUMN tariff_duration_days INTEGER NULL'
                        )
                    )
                else:  # MySQL
                    await conn.execute(
                        text('ALTER TABLE advertising_campaign_registrations ADD COLUMN tariff_duration_days INT NULL')
                    )
                logger.info('✅ Колонка tariff_duration_days добавлена в advertising_campaign_registrations')

            return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонок tariff в рекламные кампании: {error}')
        return False


async def add_tariff_device_price_column() -> bool:
    """Добавляет колонку device_price_kopeks в таблицу tariffs."""
    try:
        if await check_column_exists('tariffs', 'device_price_kopeks'):
            logger.info('ℹ️ Колонка device_price_kopeks уже существует в tariffs')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite' or db_type == 'postgresql':
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN device_price_kopeks INTEGER DEFAULT NULL'))
            else:  # MySQL
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN device_price_kopeks INT DEFAULT NULL'))

            logger.info('✅ Колонка device_price_kopeks добавлена в tariffs')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонки device_price_kopeks: {error}')
        return False


async def add_tariff_server_traffic_limits_column() -> bool:
    """Добавляет колонку server_traffic_limits в таблицу tariffs."""
    try:
        if await check_column_exists('tariffs', 'server_traffic_limits'):
            logger.info('ℹ️ Колонка server_traffic_limits уже существует в tariffs')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(text("ALTER TABLE tariffs ADD COLUMN server_traffic_limits TEXT DEFAULT '{}'"))
            elif db_type == 'postgresql':
                await conn.execute(text("ALTER TABLE tariffs ADD COLUMN server_traffic_limits JSONB DEFAULT '{}'"))
            else:  # MySQL
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN server_traffic_limits JSON DEFAULT NULL'))

            logger.info('✅ Колонка server_traffic_limits добавлена в tariffs')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонки server_traffic_limits: {error}')
        return False


async def add_tariff_allow_traffic_topup_column() -> bool:
    """Добавляет колонку allow_traffic_topup в таблицу tariffs."""
    try:
        if await check_column_exists('tariffs', 'allow_traffic_topup'):
            logger.info('ℹ️ Колонка allow_traffic_topup уже существует в tariffs')
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(
                    text('ALTER TABLE tariffs ADD COLUMN allow_traffic_topup INTEGER NOT NULL DEFAULT 1')
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text('ALTER TABLE tariffs ADD COLUMN allow_traffic_topup BOOLEAN NOT NULL DEFAULT TRUE')
                )
            else:  # MySQL
                await conn.execute(
                    text('ALTER TABLE tariffs ADD COLUMN allow_traffic_topup BOOLEAN NOT NULL DEFAULT TRUE')
                )

            logger.info('✅ Колонка allow_traffic_topup добавлена в tariffs')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонки allow_traffic_topup: {error}')
        return False


async def create_wheel_tables() -> bool:
    """Создаёт таблицы для колеса удачи: wheel_config, wheel_prizes, wheel_spins."""
    try:
        db_type = await get_database_type()

        # Создание wheel_config
        if not await check_table_exists('wheel_config'):
            async with engine.begin() as conn:
                if db_type == 'sqlite':
                    create_config_sql = """
                    CREATE TABLE wheel_config (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        is_enabled BOOLEAN NOT NULL DEFAULT 0,
                        name VARCHAR(255) NOT NULL DEFAULT 'Колесо удачи',
                        spin_cost_stars INTEGER NOT NULL DEFAULT 50,
                        spin_cost_days INTEGER NOT NULL DEFAULT 3,
                        spin_cost_stars_enabled BOOLEAN NOT NULL DEFAULT 1,
                        spin_cost_days_enabled BOOLEAN NOT NULL DEFAULT 1,
                        rtp_percent REAL NOT NULL DEFAULT 85.0,
                        daily_spin_limit INTEGER NOT NULL DEFAULT 5,
                        min_subscription_days_for_day_payment INTEGER NOT NULL DEFAULT 7,
                        promo_prefix VARCHAR(50) NOT NULL DEFAULT 'WHEEL',
                        promo_validity_days INTEGER NOT NULL DEFAULT 30,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                elif db_type == 'postgresql':
                    create_config_sql = """
                    CREATE TABLE wheel_config (
                        id SERIAL PRIMARY KEY,
                        is_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        name VARCHAR(255) NOT NULL DEFAULT 'Колесо удачи',
                        spin_cost_stars INTEGER NOT NULL DEFAULT 50,
                        spin_cost_days INTEGER NOT NULL DEFAULT 3,
                        spin_cost_stars_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        spin_cost_days_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        rtp_percent REAL NOT NULL DEFAULT 85.0,
                        daily_spin_limit INTEGER NOT NULL DEFAULT 5,
                        min_subscription_days_for_day_payment INTEGER NOT NULL DEFAULT 7,
                        promo_prefix VARCHAR(50) NOT NULL DEFAULT 'WHEEL',
                        promo_validity_days INTEGER NOT NULL DEFAULT 30,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                else:  # mysql
                    create_config_sql = """
                    CREATE TABLE wheel_config (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        is_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        name VARCHAR(255) NOT NULL DEFAULT 'Колесо удачи',
                        spin_cost_stars INT NOT NULL DEFAULT 50,
                        spin_cost_days INT NOT NULL DEFAULT 3,
                        spin_cost_stars_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        spin_cost_days_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        rtp_percent FLOAT NOT NULL DEFAULT 85.0,
                        daily_spin_limit INT NOT NULL DEFAULT 5,
                        min_subscription_days_for_day_payment INT NOT NULL DEFAULT 7,
                        promo_prefix VARCHAR(50) NOT NULL DEFAULT 'WHEEL',
                        promo_validity_days INT NOT NULL DEFAULT 30,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                    """
                await conn.execute(text(create_config_sql))
                logger.info('✅ Таблица wheel_config создана')
        else:
            logger.debug('ℹ️ Таблица wheel_config уже существует')

        # Создание wheel_prizes
        if not await check_table_exists('wheel_prizes'):
            async with engine.begin() as conn:
                if db_type == 'sqlite':
                    create_prizes_sql = """
                    CREATE TABLE wheel_prizes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        config_id INTEGER NOT NULL,
                        prize_type VARCHAR(50) NOT NULL,
                        prize_value INTEGER NOT NULL DEFAULT 0,
                        display_name VARCHAR(255) NOT NULL,
                        emoji VARCHAR(10) NOT NULL DEFAULT '🎁',
                        color VARCHAR(20) NOT NULL DEFAULT '#3B82F6',
                        prize_value_kopeks INTEGER NOT NULL DEFAULT 0,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        manual_probability REAL,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        promo_balance_bonus_kopeks INTEGER NOT NULL DEFAULT 0,
                        promo_subscription_days INTEGER NOT NULL DEFAULT 0,
                        promo_traffic_gb INTEGER NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (config_id) REFERENCES wheel_config(id) ON DELETE CASCADE
                    )
                    """
                elif db_type == 'postgresql':
                    create_prizes_sql = """
                    CREATE TABLE wheel_prizes (
                        id SERIAL PRIMARY KEY,
                        config_id INTEGER NOT NULL REFERENCES wheel_config(id) ON DELETE CASCADE,
                        prize_type VARCHAR(50) NOT NULL,
                        prize_value INTEGER NOT NULL DEFAULT 0,
                        display_name VARCHAR(255) NOT NULL,
                        emoji VARCHAR(10) NOT NULL DEFAULT '🎁',
                        color VARCHAR(20) NOT NULL DEFAULT '#3B82F6',
                        prize_value_kopeks INTEGER NOT NULL DEFAULT 0,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        manual_probability REAL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        promo_balance_bonus_kopeks INTEGER NOT NULL DEFAULT 0,
                        promo_subscription_days INTEGER NOT NULL DEFAULT 0,
                        promo_traffic_gb INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                else:  # mysql
                    create_prizes_sql = """
                    CREATE TABLE wheel_prizes (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        config_id INT NOT NULL,
                        prize_type VARCHAR(50) NOT NULL,
                        prize_value INT NOT NULL DEFAULT 0,
                        display_name VARCHAR(255) NOT NULL,
                        emoji VARCHAR(10) NOT NULL DEFAULT '🎁',
                        color VARCHAR(20) NOT NULL DEFAULT '#3B82F6',
                        prize_value_kopeks INT NOT NULL DEFAULT 0,
                        sort_order INT NOT NULL DEFAULT 0,
                        manual_probability FLOAT,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        promo_balance_bonus_kopeks INT NOT NULL DEFAULT 0,
                        promo_subscription_days INT NOT NULL DEFAULT 0,
                        promo_traffic_gb INT NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        FOREIGN KEY (config_id) REFERENCES wheel_config(id) ON DELETE CASCADE
                    )
                    """
                await conn.execute(text(create_prizes_sql))
                # Индексы
                try:
                    await conn.execute(text('CREATE INDEX idx_wheel_prizes_config_id ON wheel_prizes(config_id)'))
                except Exception:
                    pass
                logger.info('✅ Таблица wheel_prizes создана')
        else:
            logger.debug('ℹ️ Таблица wheel_prizes уже существует')

        # Создание wheel_spins
        if not await check_table_exists('wheel_spins'):
            async with engine.begin() as conn:
                if db_type == 'sqlite':
                    create_spins_sql = """
                    CREATE TABLE wheel_spins (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        config_id INTEGER NOT NULL,
                        prize_id INTEGER,
                        payment_type VARCHAR(50) NOT NULL,
                        payment_amount INTEGER NOT NULL,
                        payment_value_kopeks INTEGER NOT NULL DEFAULT 0,
                        prize_type VARCHAR(50) NOT NULL,
                        prize_value INTEGER NOT NULL DEFAULT 0,
                        prize_value_kopeks INTEGER NOT NULL DEFAULT 0,
                        promocode_id INTEGER,
                        is_applied BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY (config_id) REFERENCES wheel_config(id) ON DELETE CASCADE,
                        FOREIGN KEY (prize_id) REFERENCES wheel_prizes(id) ON DELETE SET NULL,
                        FOREIGN KEY (promocode_id) REFERENCES promocodes(id) ON DELETE SET NULL
                    )
                    """
                elif db_type == 'postgresql':
                    create_spins_sql = """
                    CREATE TABLE wheel_spins (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        config_id INTEGER NOT NULL REFERENCES wheel_config(id) ON DELETE CASCADE,
                        prize_id INTEGER REFERENCES wheel_prizes(id) ON DELETE SET NULL,
                        payment_type VARCHAR(50) NOT NULL,
                        payment_amount INTEGER NOT NULL,
                        payment_value_kopeks INTEGER NOT NULL DEFAULT 0,
                        prize_type VARCHAR(50) NOT NULL,
                        prize_value INTEGER NOT NULL DEFAULT 0,
                        prize_value_kopeks INTEGER NOT NULL DEFAULT 0,
                        promocode_id INTEGER REFERENCES promocodes(id) ON DELETE SET NULL,
                        is_applied BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                else:  # mysql
                    create_spins_sql = """
                    CREATE TABLE wheel_spins (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NOT NULL,
                        config_id INT NOT NULL,
                        prize_id INT,
                        payment_type VARCHAR(50) NOT NULL,
                        payment_amount INT NOT NULL,
                        payment_value_kopeks INT NOT NULL DEFAULT 0,
                        prize_type VARCHAR(50) NOT NULL,
                        prize_value INT NOT NULL DEFAULT 0,
                        prize_value_kopeks INT NOT NULL DEFAULT 0,
                        promocode_id INT,
                        is_applied BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY (config_id) REFERENCES wheel_config(id) ON DELETE CASCADE,
                        FOREIGN KEY (prize_id) REFERENCES wheel_prizes(id) ON DELETE SET NULL,
                        FOREIGN KEY (promocode_id) REFERENCES promocodes(id) ON DELETE SET NULL
                    )
                    """
                await conn.execute(text(create_spins_sql))
                # Индексы
                try:
                    await conn.execute(text('CREATE INDEX idx_wheel_spins_user_id ON wheel_spins(user_id)'))
                    await conn.execute(text('CREATE INDEX idx_wheel_spins_created_at ON wheel_spins(created_at)'))
                except Exception:
                    pass
                logger.info('✅ Таблица wheel_spins создана')
        else:
            logger.debug('ℹ️ Таблица wheel_spins уже существует')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблиц для колеса удачи: {error}')
        return False


async def add_tariff_traffic_topup_columns() -> bool:
    """Добавляет колонки для докупки трафика в тарифах."""
    try:
        columns_added = 0

        # Колонка traffic_topup_enabled
        if not await check_column_exists('tariffs', 'traffic_topup_enabled'):
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite':
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN traffic_topup_enabled INTEGER DEFAULT 0 NOT NULL')
                    )
                elif db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN traffic_topup_enabled BOOLEAN DEFAULT FALSE NOT NULL')
                    )
                else:  # MySQL
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN traffic_topup_enabled TINYINT(1) DEFAULT 0 NOT NULL')
                    )

                logger.info('✅ Колонка traffic_topup_enabled добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка traffic_topup_enabled уже существует в tariffs')

        # Колонка traffic_topup_packages (JSON)
        if not await check_column_exists('tariffs', 'traffic_topup_packages'):
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite':
                    await conn.execute(text("ALTER TABLE tariffs ADD COLUMN traffic_topup_packages TEXT DEFAULT '{}'"))
                elif db_type == 'postgresql':
                    await conn.execute(text("ALTER TABLE tariffs ADD COLUMN traffic_topup_packages JSONB DEFAULT '{}'"))
                else:  # MySQL
                    await conn.execute(text('ALTER TABLE tariffs ADD COLUMN traffic_topup_packages JSON DEFAULT NULL'))

                logger.info('✅ Колонка traffic_topup_packages добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка traffic_topup_packages уже существует в tariffs')

        # Колонка max_topup_traffic_gb (максимальный лимит трафика после докупок)
        if not await check_column_exists('tariffs', 'max_topup_traffic_gb'):
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN max_topup_traffic_gb INTEGER DEFAULT 0 NOT NULL')
                    )
                else:  # MySQL
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN max_topup_traffic_gb INT DEFAULT 0 NOT NULL')
                    )

                logger.info('✅ Колонка max_topup_traffic_gb добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка max_topup_traffic_gb уже существует в tariffs')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонок для докупки трафика: {error}')
        return False


async def add_tariff_daily_columns() -> bool:
    """Добавляет колонки для суточных тарифов."""
    try:
        columns_added = 0

        # Колонка is_daily
        if not await check_column_exists('tariffs', 'is_daily'):
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite':
                    await conn.execute(text('ALTER TABLE tariffs ADD COLUMN is_daily INTEGER DEFAULT 0 NOT NULL'))
                elif db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE tariffs ADD COLUMN is_daily BOOLEAN DEFAULT FALSE NOT NULL'))
                else:  # MySQL
                    await conn.execute(text('ALTER TABLE tariffs ADD COLUMN is_daily TINYINT(1) DEFAULT 0 NOT NULL'))

                logger.info('✅ Колонка is_daily добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка is_daily уже существует в tariffs')

        # Колонка daily_price_kopeks
        if not await check_column_exists('tariffs', 'daily_price_kopeks'):
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite' or db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN daily_price_kopeks INTEGER DEFAULT 0 NOT NULL')
                    )
                else:  # MySQL
                    await conn.execute(text('ALTER TABLE tariffs ADD COLUMN daily_price_kopeks INT DEFAULT 0 NOT NULL'))

                logger.info('✅ Колонка daily_price_kopeks добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка daily_price_kopeks уже существует в tariffs')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонок суточного тарифа: {error}')
        return False


async def add_tariff_custom_days_traffic_columns() -> bool:
    """Добавляет колонки для произвольных дней и трафика в тарифы."""
    try:
        columns_added = 0
        db_type = await get_database_type()

        # === ПРОИЗВОЛЬНОЕ КОЛИЧЕСТВО ДНЕЙ ===
        # custom_days_enabled
        if not await check_column_exists('tariffs', 'custom_days_enabled'):
            async with engine.begin() as conn:
                if db_type == 'sqlite':
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN custom_days_enabled INTEGER DEFAULT 0 NOT NULL')
                    )
                elif db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN custom_days_enabled BOOLEAN DEFAULT FALSE NOT NULL')
                    )
                else:  # MySQL
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN custom_days_enabled TINYINT(1) DEFAULT 0 NOT NULL')
                    )
                logger.info('✅ Колонка custom_days_enabled добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка custom_days_enabled уже существует в tariffs')

        # price_per_day_kopeks
        if not await check_column_exists('tariffs', 'price_per_day_kopeks'):
            async with engine.begin() as conn:
                await conn.execute(
                    text('ALTER TABLE tariffs ADD COLUMN price_per_day_kopeks INTEGER DEFAULT 0 NOT NULL')
                )
                logger.info('✅ Колонка price_per_day_kopeks добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка price_per_day_kopeks уже существует в tariffs')

        # min_days
        if not await check_column_exists('tariffs', 'min_days'):
            async with engine.begin() as conn:
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN min_days INTEGER DEFAULT 1 NOT NULL'))
                logger.info('✅ Колонка min_days добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка min_days уже существует в tariffs')

        # max_days
        if not await check_column_exists('tariffs', 'max_days'):
            async with engine.begin() as conn:
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN max_days INTEGER DEFAULT 365 NOT NULL'))
                logger.info('✅ Колонка max_days добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка max_days уже существует в tariffs')

        # === ПРОИЗВОЛЬНЫЙ ТРАФИК ПРИ ПОКУПКЕ ===
        # custom_traffic_enabled
        if not await check_column_exists('tariffs', 'custom_traffic_enabled'):
            async with engine.begin() as conn:
                if db_type == 'sqlite':
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN custom_traffic_enabled INTEGER DEFAULT 0 NOT NULL')
                    )
                elif db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN custom_traffic_enabled BOOLEAN DEFAULT FALSE NOT NULL')
                    )
                else:  # MySQL
                    await conn.execute(
                        text('ALTER TABLE tariffs ADD COLUMN custom_traffic_enabled TINYINT(1) DEFAULT 0 NOT NULL')
                    )
                logger.info('✅ Колонка custom_traffic_enabled добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка custom_traffic_enabled уже существует в tariffs')

        # traffic_price_per_gb_kopeks
        if not await check_column_exists('tariffs', 'traffic_price_per_gb_kopeks'):
            async with engine.begin() as conn:
                await conn.execute(
                    text('ALTER TABLE tariffs ADD COLUMN traffic_price_per_gb_kopeks INTEGER DEFAULT 0 NOT NULL')
                )
                logger.info('✅ Колонка traffic_price_per_gb_kopeks добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка traffic_price_per_gb_kopeks уже существует в tariffs')

        # min_traffic_gb
        if not await check_column_exists('tariffs', 'min_traffic_gb'):
            async with engine.begin() as conn:
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN min_traffic_gb INTEGER DEFAULT 1 NOT NULL'))
                logger.info('✅ Колонка min_traffic_gb добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка min_traffic_gb уже существует в tariffs')

        # max_traffic_gb
        if not await check_column_exists('tariffs', 'max_traffic_gb'):
            async with engine.begin() as conn:
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN max_traffic_gb INTEGER DEFAULT 1000 NOT NULL'))
                logger.info('✅ Колонка max_traffic_gb добавлена в tariffs')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка max_traffic_gb уже существует в tariffs')

        if columns_added > 0:
            logger.info(f'✅ Добавлено {columns_added} колонок для произвольных дней/трафика')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонок произвольных дней/трафика: {error}')
        return False


async def add_tariff_traffic_reset_mode_column() -> bool:
    """Добавляет колонку traffic_reset_mode в tariffs для настройки режима сброса трафика.

    Значения: DAY, WEEK, MONTH, NO_RESET (NULL = использовать глобальную настройку)
    """
    try:
        if not await check_column_exists('tariffs', 'traffic_reset_mode'):
            async with engine.begin() as conn:
                await conn.execute(text('ALTER TABLE tariffs ADD COLUMN traffic_reset_mode VARCHAR(20) NULL'))
                logger.info('✅ Колонка traffic_reset_mode добавлена в tariffs')
                return True
        else:
            logger.info('ℹ️ Колонка traffic_reset_mode уже существует в tariffs')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонки traffic_reset_mode: {error}')
        return False


async def add_subscription_daily_columns() -> bool:
    """Добавляет колонки для суточных подписок."""
    try:
        columns_added = 0

        # Колонка is_daily_paused
        if not await check_column_exists('subscriptions', 'is_daily_paused'):
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite':
                    await conn.execute(
                        text('ALTER TABLE subscriptions ADD COLUMN is_daily_paused INTEGER DEFAULT 0 NOT NULL')
                    )
                elif db_type == 'postgresql':
                    await conn.execute(
                        text('ALTER TABLE subscriptions ADD COLUMN is_daily_paused BOOLEAN DEFAULT FALSE NOT NULL')
                    )
                else:  # MySQL
                    await conn.execute(
                        text('ALTER TABLE subscriptions ADD COLUMN is_daily_paused TINYINT(1) DEFAULT 0 NOT NULL')
                    )

                logger.info('✅ Колонка is_daily_paused добавлена в subscriptions')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка is_daily_paused уже существует в subscriptions')

        # Колонка last_daily_charge_at
        if not await check_column_exists('subscriptions', 'last_daily_charge_at'):
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite':
                    await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN last_daily_charge_at DATETIME NULL'))
                elif db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN last_daily_charge_at TIMESTAMP NULL'))
                else:  # MySQL
                    await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN last_daily_charge_at DATETIME NULL'))

                logger.info('✅ Колонка last_daily_charge_at добавлена в subscriptions')
                columns_added += 1
        else:
            logger.info('ℹ️ Колонка last_daily_charge_at уже существует в subscriptions')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонок суточной подписки: {error}')
        return False


async def add_subscription_traffic_reset_at_column() -> bool:
    """Добавляет колонку traffic_reset_at в subscriptions для сброса докупленного трафика через 30 дней."""
    try:
        if not await check_column_exists('subscriptions', 'traffic_reset_at'):
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite':
                    await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN traffic_reset_at DATETIME NULL'))
                elif db_type == 'postgresql':
                    await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN traffic_reset_at TIMESTAMP NULL'))
                else:  # MySQL
                    await conn.execute(text('ALTER TABLE subscriptions ADD COLUMN traffic_reset_at DATETIME NULL'))

                logger.info('✅ Колонка traffic_reset_at добавлена в subscriptions')
                return True
        else:
            logger.info('ℹ️ Колонка traffic_reset_at уже существует в subscriptions')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка добавления колонки traffic_reset_at: {error}')
        return False


async def add_user_email_auth_columns() -> bool:
    """
    Миграция для поддержки email-регистрации без Telegram.

    1. Делает telegram_id nullable (для email-only пользователей)
    2. Добавляет колонку auth_type ('telegram' или 'email')
    """
    try:
        db_type = await get_database_type()

        # Проверяем существование колонки auth_type
        auth_type_exists = await check_column_exists('users', 'auth_type')

        async with engine.begin() as conn:
            # 1. Добавляем колонку auth_type если её нет
            if not auth_type_exists:
                if db_type == 'sqlite' or db_type == 'postgresql' or db_type == 'mysql':
                    await conn.execute(
                        text("ALTER TABLE users ADD COLUMN auth_type VARCHAR(20) DEFAULT 'telegram' NOT NULL")
                    )
                else:
                    logger.error(f'Неподдерживаемый тип БД: {db_type}')
                    return False
                logger.info('✅ Добавлена колонка users.auth_type')
            else:
                logger.info('ℹ️ Колонка auth_type уже существует')

            # 2. Делаем telegram_id nullable (только PostgreSQL и MySQL поддерживают ALTER COLUMN)
            # SQLite не поддерживает ALTER COLUMN, но мы можем просто не делать это -
            # новые email-пользователи будут создаваться с telegram_id=NULL если БД уже nullable

            if db_type == 'postgresql':
                # Проверяем является ли telegram_id nullable
                result = await conn.execute(
                    text("""
                    SELECT is_nullable
                    FROM information_schema.columns
                    WHERE table_name = 'users' AND column_name = 'telegram_id'
                """)
                )
                row = result.fetchone()

                if row and row[0] == 'NO':
                    # telegram_id NOT NULL - нужно сделать nullable
                    await conn.execute(text('ALTER TABLE users ALTER COLUMN telegram_id DROP NOT NULL'))
                    logger.info('✅ Колонка users.telegram_id теперь nullable')
                else:
                    logger.info('ℹ️ Колонка telegram_id уже nullable')

            elif db_type == 'mysql':
                # MySQL требует полное определение колонки при ALTER
                result = await conn.execute(
                    text("""
                    SELECT IS_NULLABLE
                    FROM information_schema.COLUMNS
                    WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'telegram_id'
                """)
                )
                row = result.fetchone()

                if row and row[0] == 'NO':
                    await conn.execute(text('ALTER TABLE users MODIFY COLUMN telegram_id BIGINT NULL'))
                    logger.info('✅ Колонка users.telegram_id теперь nullable')
                else:
                    logger.info('ℹ️ Колонка telegram_id уже nullable')

            elif db_type == 'sqlite':
                # SQLite не поддерживает ALTER COLUMN
                # Для SQLite нужна пересоздание таблицы, но это сложно
                # Оставляем как есть - при необходимости нужна ручная миграция
                logger.info('ℹ️ SQLite: изменение nullable требует ручной миграции')

        return True

    except Exception as error:
        logger.error(f'❌ Ошибка миграции email auth: {error}')
        return False


async def create_email_templates_table() -> bool:
    """Create email_templates table for storing custom email template overrides."""
    table_exists = await check_table_exists('email_templates')
    if table_exists:
        logger.info('ℹ️ Таблица email_templates уже существует')
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE email_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_type VARCHAR(50) NOT NULL,
                    language VARCHAR(10) NOT NULL,
                    subject VARCHAR(500) NOT NULL,
                    body_html TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(notification_type, language)
                )
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE email_templates (
                    id SERIAL PRIMARY KEY,
                    notification_type VARCHAR(50) NOT NULL,
                    language VARCHAR(10) NOT NULL,
                    subject VARCHAR(500) NOT NULL,
                    body_html TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(notification_type, language)
                )
                """
            else:
                create_sql = """
                CREATE TABLE email_templates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    notification_type VARCHAR(50) NOT NULL,
                    language VARCHAR(10) NOT NULL,
                    subject VARCHAR(500) NOT NULL,
                    body_html TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_email_templates_type_lang (notification_type, language)
                ) ENGINE=InnoDB
                """

            await conn.execute(text(create_sql))
            await conn.execute(text('CREATE INDEX idx_email_templates_type ON email_templates(notification_type)'))
            logger.info('✅ Таблица email_templates создана')
            return True

    except Exception as error:
        logger.error(f'❌ Ошибка создания таблицы email_templates: {error}')
        return False


async def migrate_cloudpayments_transaction_id_to_bigint() -> bool:
    """
    Миграция колонки transaction_id_cp в cloudpayments_payments с INTEGER на BIGINT.
    CloudPayments transaction IDs могут превышать максимум int32 (2,147,483,647).
    """
    try:
        table_exists = await check_table_exists('cloudpayments_payments')
        if not table_exists:
            logger.info('ℹ️ Таблица cloudpayments_payments не существует, пропускаем миграцию')
            return True

        db_type = await get_database_type()

        async with engine.begin() as conn:
            if db_type == 'postgresql':
                # Проверяем текущий тип колонки
                result = await conn.execute(
                    text("""
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_name = 'cloudpayments_payments'
                    AND column_name = 'transaction_id_cp'
                """)
                )
                row = result.fetchone()

                if row and row[0] == 'bigint':
                    logger.info('ℹ️ Колонка transaction_id_cp уже имеет тип BIGINT')
                    return True

                # Меняем тип на BIGINT
                await conn.execute(
                    text('ALTER TABLE cloudpayments_payments ALTER COLUMN transaction_id_cp TYPE BIGINT')
                )
                logger.info('✅ Колонка transaction_id_cp изменена на BIGINT')

            elif db_type == 'mysql':
                # Проверяем текущий тип колонки
                result = await conn.execute(
                    text("""
                    SELECT DATA_TYPE
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = 'cloudpayments_payments'
                    AND COLUMN_NAME = 'transaction_id_cp'
                """)
                )
                row = result.fetchone()

                if row and row[0].lower() == 'bigint':
                    logger.info('ℹ️ Колонка transaction_id_cp уже имеет тип BIGINT')
                    return True

                await conn.execute(text('ALTER TABLE cloudpayments_payments MODIFY transaction_id_cp BIGINT'))
                logger.info('✅ Колонка transaction_id_cp изменена на BIGINT')

            elif db_type == 'sqlite':
                # SQLite не поддерживает ALTER COLUMN, но INTEGER в SQLite уже 64-bit
                logger.info('ℹ️ SQLite использует 64-bit INTEGER по умолчанию, миграция не требуется')

            return True

    except Exception as error:
        logger.error(f'❌ Ошибка миграции transaction_id_cp на BIGINT: {error}')
        return False


async def run_universal_migration():
    logger.info('=== НАЧАЛО УНИВЕРСАЛЬНОЙ МИГРАЦИИ ===')

    try:
        db_type = await get_database_type()
        logger.info(f'Тип базы данных: {db_type}')

        if db_type == 'postgresql':
            logger.info('=== СИНХРОНИЗАЦИЯ ПОСЛЕДОВАТЕЛЬНОСТЕЙ PostgreSQL ===')
            sequences_synced = await sync_postgres_sequences()
            if sequences_synced:
                logger.info('✅ Последовательности PostgreSQL синхронизированы')
            else:
                logger.warning('⚠️ Не удалось синхронизировать последовательности PostgreSQL')

        referral_migration_success = await add_referral_system_columns()
        if not referral_migration_success:
            logger.warning('⚠️ Проблемы с миграцией реферальной системы')

        commission_column_ready = await add_referral_commission_percent_column()
        if commission_column_ready:
            logger.info('✅ Колонка referral_commission_percent готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой referral_commission_percent')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ SYSTEM_SETTINGS ===')
        system_settings_ready = await create_system_settings_table()
        if system_settings_ready:
            logger.info('✅ Таблица system_settings готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей system_settings')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ WEB_API_TOKENS ===')
        web_api_tokens_ready = await create_web_api_tokens_table()
        if web_api_tokens_ready:
            logger.info('✅ Таблица web_api_tokens готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей web_api_tokens')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ MENU_LAYOUT_HISTORY ===')
        menu_layout_history_ready = await create_menu_layout_history_table()
        if menu_layout_history_ready:
            logger.info('✅ Таблица menu_layout_history готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей menu_layout_history')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ BUTTON_CLICK_LOGS ===')
        button_click_logs_ready = await create_button_click_logs_table()
        if button_click_logs_ready:
            logger.info('✅ Таблица button_click_logs готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей button_click_logs')

        logger.info('=== ИСПРАВЛЕНИЕ FK BUTTON_CLICK_LOGS ===')
        fk_fixed = await fix_button_click_logs_fk()
        if fk_fixed:
            logger.info('✅ FK button_click_logs проверен')
        else:
            logger.warning('⚠️ Проблемы с FK button_click_logs')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНКИ ДЛЯ ТРИАЛЬНЫХ СКВАДОВ ===')
        trial_column_ready = await add_server_trial_flag_column()
        if trial_column_ready:
            logger.info('✅ Колонка is_trial_eligible готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой is_trial_eligible')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ PRIVACY_POLICIES ===')
        privacy_policies_ready = await create_privacy_policies_table()
        if privacy_policies_ready:
            logger.info('✅ Таблица privacy_policies готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей privacy_policies')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ PUBLIC_OFFERS ===')
        public_offers_ready = await create_public_offers_table()
        if public_offers_ready:
            logger.info('✅ Таблица public_offers готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей public_offers')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ FAQ_SETTINGS ===')
        faq_settings_ready = await create_faq_settings_table()
        if faq_settings_ready:
            logger.info('✅ Таблица faq_settings готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей faq_settings')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ FAQ_PAGES ===')
        faq_pages_ready = await create_faq_pages_table()
        if faq_pages_ready:
            logger.info('✅ Таблица faq_pages готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей faq_pages')

        logger.info('=== ПРОВЕРКА БАЗОВЫХ ТОКЕНОВ ВЕБ-API ===')
        default_token_ready = await ensure_default_web_api_token()
        if default_token_ready:
            logger.info('✅ Бутстрап токен веб-API готов')
        else:
            logger.warning('⚠️ Не удалось создать бутстрап токен веб-API')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ CRYPTOBOT ===')
        cryptobot_created = await create_cryptobot_payments_table()
        if cryptobot_created:
            logger.info('✅ Таблица CryptoBot payments готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей CryptoBot payments')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ HELEKET ===')
        heleket_created = await create_heleket_payments_table()
        if heleket_created:
            logger.info('✅ Таблица Heleket payments готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей Heleket payments')

        mulenpay_name = settings.get_mulenpay_display_name()
        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ %s ===', mulenpay_name)
        mulenpay_created = await create_mulenpay_payments_table()
        if mulenpay_created:
            logger.info('✅ Таблица %s payments готова', mulenpay_name)
        else:
            logger.warning('⚠️ Проблемы с таблицей %s payments', mulenpay_name)

        mulenpay_schema_ok = await ensure_mulenpay_payment_schema()
        if mulenpay_schema_ok:
            logger.info('✅ Схема %s payments актуальна', mulenpay_name)
        else:
            logger.warning('⚠️ Не удалось обновить схему %s payments', mulenpay_name)

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ PAL24 ===')
        pal24_created = await create_pal24_payments_table()
        if pal24_created:
            logger.info('✅ Таблица Pal24 payments готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей Pal24 payments')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ WATA ===')
        wata_created = await create_wata_payments_table()
        if wata_created:
            logger.info('✅ Таблица Wata payments готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей Wata payments')

        wata_schema_ok = await ensure_wata_payment_schema()
        if wata_schema_ok:
            logger.info('✅ Схема Wata payments актуальна')
        else:
            logger.warning('⚠️ Не удалось обновить схему Wata payments')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ FREEKASSA ===')
        freekassa_created = await create_freekassa_payments_table()
        if freekassa_created:
            logger.info('✅ Таблица Freekassa payments готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей Freekassa payments')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ KASSA_AI ===')
        kassa_ai_created = await create_kassa_ai_payments_table()
        if kassa_ai_created:
            logger.info('✅ Таблица KassaAI payments готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей KassaAI payments')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ DISCOUNT_OFFERS ===')
        discount_created = await create_discount_offers_table()
        if discount_created:
            logger.info('✅ Таблица discount_offers готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей discount_offers')

        discount_columns_ready = await ensure_discount_offer_columns()
        if discount_columns_ready:
            logger.info('✅ Колонки discount_offers в актуальном состоянии')
        else:
            logger.warning('⚠️ Не удалось обновить колонки discount_offers')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦ ДЛЯ РЕФЕРАЛЬНЫХ КОНКУРСОВ ===')
        contests_table_ready = await create_referral_contests_table()
        if contests_table_ready:
            logger.info('✅ Таблица referral_contests готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей referral_contests')

        contest_events_ready = await create_referral_contest_events_table()
        if contest_events_ready:
            logger.info('✅ Таблица referral_contest_events готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей referral_contest_events')

        virtual_participants_ready = await create_referral_contest_virtual_participants_table()
        if virtual_participants_ready:
            logger.info('✅ Таблица referral_contest_virtual_participants готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей referral_contest_virtual_participants')

        contest_type_ready = await ensure_referral_contest_type_column()
        if contest_type_ready:
            logger.info('✅ Колонка contest_type для referral_contests готова')
        else:
            logger.warning('⚠️ Не удалось добавить contest_type в referral_contests')

        contest_summary_ready = await ensure_referral_contest_summary_columns()
        if contest_summary_ready:
            logger.info('✅ Колонки daily_summary_times/last_daily_summary_at готовы')
        else:
            logger.warning('⚠️ Не удалось обновить колонки сводок для referral_contests')

        contest_templates_ready = await create_contest_templates_table()
        if contest_templates_ready:
            logger.info('✅ Таблица contest_templates готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей contest_templates')

        logger.info('=== МИГРАЦИЯ КОЛОНОК ПРИЗА В CONTEST_TEMPLATES ===')
        prize_columns_ready = await migrate_contest_templates_prize_columns()
        if prize_columns_ready:
            logger.info('✅ Колонки prize_type и prize_value готовы')
        else:
            logger.warning('⚠️ Проблемы с миграцией prize_type/prize_value')

        contest_rounds_ready = await create_contest_rounds_table()
        if contest_rounds_ready:
            logger.info('✅ Таблица contest_rounds готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей contest_rounds')

        contest_attempts_ready = await create_contest_attempts_table()
        if contest_attempts_ready:
            logger.info('✅ Таблица contest_attempts готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей contest_attempts')

        user_discount_columns_ready = await ensure_user_promo_offer_discount_columns()
        if user_discount_columns_ready:
            logger.info('✅ Колонки пользовательских промо-скидок готовы')
        else:
            logger.warning('⚠️ Не удалось обновить пользовательские промо-скидки')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНКИ NOTIFICATION_SETTINGS ===')
        notification_settings_ready = await ensure_user_notification_settings_column()
        if notification_settings_ready:
            logger.info('✅ Колонка notification_settings готова')
        else:
            logger.warning('⚠️ Не удалось добавить колонку notification_settings')

        effect_types_updated = await migrate_discount_offer_effect_types()
        if effect_types_updated:
            logger.info('✅ Типы эффектов промо-предложений обновлены')
        else:
            logger.warning('⚠️ Не удалось обновить типы эффектов промо-предложений')

        bonuses_reset = await reset_discount_offer_bonuses()
        if bonuses_reset:
            logger.info('✅ Бонусные начисления промо-предложений отключены')
        else:
            logger.warning('⚠️ Не удалось обнулить бонусы промо-предложений')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ PROMO_OFFER_TEMPLATES ===')
        promo_templates_created = await create_promo_offer_templates_table()
        if promo_templates_created:
            logger.info('✅ Таблица promo_offer_templates готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей promo_offer_templates')

        logger.info('=== ДОБАВЛЕНИЕ ПРИОРИТЕТА В ПРОМОГРУППЫ ===')
        priority_column_ready = await add_promo_group_priority_column()
        if priority_column_ready:
            logger.info('✅ Колонка priority в promo_groups готова')
        else:
            logger.warning('⚠️ Проблемы с добавлением priority в promo_groups')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ USER_PROMO_GROUPS ===')
        user_promo_groups_ready = await create_user_promo_groups_table()
        if user_promo_groups_ready:
            logger.info('✅ Таблица user_promo_groups готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей user_promo_groups')

        logger.info('=== МИГРАЦИЯ ДАННЫХ В USER_PROMO_GROUPS ===')
        data_migrated = await migrate_existing_user_promo_groups_data()
        if data_migrated:
            logger.info('✅ Данные перенесены в user_promo_groups')
        else:
            logger.warning('⚠️ Проблемы с миграцией данных в user_promo_groups')

        logger.info('=== ДОБАВЛЕНИЕ PROMO_GROUP_ID В PROMOCODES ===')
        promocode_column_ready = await add_promocode_promo_group_column()
        if promocode_column_ready:
            logger.info('✅ Колонка promo_group_id в promocodes готова')
        else:
            logger.warning('⚠️ Проблемы с добавлением promo_group_id в promocodes')

        logger.info('=== ДОБАВЛЕНИЕ FIRST_PURCHASE_ONLY В PROMOCODES ===')
        first_purchase_ready = await add_promocode_first_purchase_only_column()
        if first_purchase_ready:
            logger.info('✅ Колонка first_purchase_only в promocodes готова')
        else:
            logger.warning('⚠️ Проблемы с добавлением first_purchase_only в promocodes')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ MAIN_MENU_BUTTONS ===')
        main_menu_buttons_created = await create_main_menu_buttons_table()
        if main_menu_buttons_created:
            logger.info('✅ Таблица main_menu_buttons готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей main_menu_buttons')

        template_columns_ready = await ensure_promo_offer_template_active_duration_column()
        if template_columns_ready:
            logger.info('✅ Колонка active_discount_hours промо-предложений готова')
        else:
            logger.warning('⚠️ Не удалось обновить колонку active_discount_hours промо-предложений')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ PROMO_OFFER_LOGS ===')
        promo_logs_created = await create_promo_offer_logs_table()
        if promo_logs_created:
            logger.info('✅ Таблица promo_offer_logs готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей promo_offer_logs')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ SUBSCRIPTION_TEMPORARY_ACCESS ===')
        temp_access_created = await create_subscription_temporary_access_table()
        if temp_access_created:
            logger.info('✅ Таблица subscription_temporary_access готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей subscription_temporary_access')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ USER_MESSAGES ===')
        user_messages_created = await create_user_messages_table()
        if user_messages_created:
            logger.info('✅ Таблица user_messages готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей user_messages')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ PINNED_MESSAGES ===')
        pinned_messages_created = await create_pinned_messages_table()
        if pinned_messages_created:
            logger.info('✅ Таблица pinned_messages готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей pinned_messages')

        logger.info('=== СОЗДАНИЕ/ОБНОВЛЕНИЕ ТАБЛИЦЫ WELCOME_TEXTS ===')
        welcome_texts_created = await create_welcome_texts_table()
        if welcome_texts_created:
            logger.info('✅ Таблица welcome_texts готова с полем is_enabled')
        else:
            logger.warning('⚠️ Проблемы с таблицей welcome_texts')

        logger.info('=== ОБНОВЛЕНИЕ СХЕМЫ PINNED_MESSAGES ===')
        pinned_media_ready = await ensure_pinned_message_media_columns()
        if pinned_media_ready:
            logger.info('✅ Медиа поля для pinned_messages готовы')
        else:
            logger.warning('⚠️ Проблемы с медиа полями pinned_messages')

        logger.info('=== ДОБАВЛЕНИЕ СЛЕДА ОТПРАВКИ ЗАКРЕПА ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ===')
        last_pinned_ready = await ensure_user_last_pinned_column()
        if last_pinned_ready:
            logger.info('✅ Колонка last_pinned_message_id добавлена')
        else:
            logger.warning('⚠️ Не удалось обновить колонку last_pinned_message_id')

        logger.info('=== ДОБАВЛЕНИЕ МЕДИА ПОЛЕЙ В BROADCAST_HISTORY ===')
        media_fields_added = await add_media_fields_to_broadcast_history()
        if media_fields_added:
            logger.info('✅ Медиа поля в broadcast_history готовы')
        else:
            logger.warning('⚠️ Проблемы с добавлением медиа полей')

        logger.info('=== ДОБАВЛЕНИЕ EMAIL ПОЛЕЙ В BROADCAST_HISTORY ===')
        email_fields_added = await add_email_fields_to_broadcast_history()
        if email_fields_added:
            logger.info('✅ Email поля в broadcast_history готовы')
        else:
            logger.warning('⚠️ Проблемы с добавлением email полей')

        logger.info('=== ДОБАВЛЕНИЕ ПОЛЕЙ БЛОКИРОВКИ В TICKETS ===')
        tickets_block_cols_added = await add_ticket_reply_block_columns()
        if tickets_block_cols_added:
            logger.info('✅ Поля блокировок в tickets готовы')
        else:
            logger.warning('⚠️ Проблемы с добавлением полей блокировок в tickets')

        logger.info('=== ДОБАВЛЕНИЕ ПОЛЕЙ SLA В TICKETS ===')
        sla_cols_added = await add_ticket_sla_columns()
        if sla_cols_added:
            logger.info('✅ Поля SLA в tickets готовы')
        else:
            logger.warning('⚠️ Проблемы с добавлением полей SLA в tickets')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНКИ CRYPTO LINK ДЛЯ ПОДПИСОК ===')
        crypto_link_added = await add_subscription_crypto_link_column()
        if crypto_link_added:
            logger.info('✅ Колонка subscription_crypto_link готова')
        else:
            logger.warning('⚠️ Проблемы с добавлением колонки subscription_crypto_link')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНКИ MODEM_ENABLED ДЛЯ ПОДПИСОК ===')
        modem_enabled_added = await add_subscription_modem_enabled_column()
        if modem_enabled_added:
            logger.info('✅ Колонка modem_enabled готова')
        else:
            logger.warning('⚠️ Проблемы с добавлением колонки modem_enabled')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНКИ PURCHASED_TRAFFIC_GB ДЛЯ ПОДПИСОК ===')
        purchased_traffic_added = await add_subscription_purchased_traffic_column()
        if purchased_traffic_added:
            logger.info('✅ Колонка purchased_traffic_gb готова')
        else:
            logger.warning('⚠️ Проблемы с добавлением колонки purchased_traffic_gb')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНОК ОГРАНИЧЕНИЙ ПОЛЬЗОВАТЕЛЕЙ ===')
        restrictions_added = await add_user_restriction_columns()
        if restrictions_added:
            logger.info('✅ Колонки ограничений пользователей готовы')
        else:
            logger.warning('⚠️ Проблемы с добавлением колонок ограничений пользователей')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНОК ЛИЧНОГО КАБИНЕТА ===')
        cabinet_added = await add_user_cabinet_columns()
        if cabinet_added:
            logger.info('✅ Колонки личного кабинета готовы')
        else:
            logger.warning('⚠️ Проблемы с добавлением колонок личного кабинета')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ АУДИТА ПОДДЕРЖКИ ===')
        try:
            async with engine.begin() as conn:
                db_type = await get_database_type()
                if not await check_table_exists('support_audit_logs'):
                    if db_type == 'sqlite':
                        create_sql = """
                        CREATE TABLE support_audit_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            actor_user_id INTEGER NULL,
                            actor_telegram_id BIGINT NOT NULL,
                            is_moderator BOOLEAN NOT NULL DEFAULT 0,
                            action VARCHAR(50) NOT NULL,
                            ticket_id INTEGER NULL,
                            target_user_id INTEGER NULL,
                            details JSON NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (actor_user_id) REFERENCES users(id),
                            FOREIGN KEY (ticket_id) REFERENCES tickets(id),
                            FOREIGN KEY (target_user_id) REFERENCES users(id)
                        );
                        CREATE INDEX idx_support_audit_logs_ticket ON support_audit_logs(ticket_id);
                        CREATE INDEX idx_support_audit_logs_actor ON support_audit_logs(actor_telegram_id);
                        CREATE INDEX idx_support_audit_logs_action ON support_audit_logs(action);
                        """
                    elif db_type == 'postgresql':
                        create_sql = """
                        CREATE TABLE support_audit_logs (
                            id SERIAL PRIMARY KEY,
                            actor_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                            actor_telegram_id BIGINT NOT NULL,
                            is_moderator BOOLEAN NOT NULL DEFAULT FALSE,
                            action VARCHAR(50) NOT NULL,
                            ticket_id INTEGER NULL REFERENCES tickets(id) ON DELETE SET NULL,
                            target_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                            details JSON NULL,
                            created_at TIMESTAMP DEFAULT NOW()
                        );
                        CREATE INDEX idx_support_audit_logs_ticket ON support_audit_logs(ticket_id);
                        CREATE INDEX idx_support_audit_logs_actor ON support_audit_logs(actor_telegram_id);
                        CREATE INDEX idx_support_audit_logs_action ON support_audit_logs(action);
                        """
                    else:
                        create_sql = """
                        CREATE TABLE support_audit_logs (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            actor_user_id INT NULL,
                            actor_telegram_id BIGINT NOT NULL,
                            is_moderator BOOLEAN NOT NULL DEFAULT 0,
                            action VARCHAR(50) NOT NULL,
                            ticket_id INT NULL,
                            target_user_id INT NULL,
                            details JSON NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE INDEX idx_support_audit_logs_ticket ON support_audit_logs(ticket_id);
                        CREATE INDEX idx_support_audit_logs_actor ON support_audit_logs(actor_telegram_id);
                        CREATE INDEX idx_support_audit_logs_action ON support_audit_logs(action);
                        """
                    await conn.execute(text(create_sql))
                    logger.info('✅ Таблица support_audit_logs создана')
                else:
                    logger.info('ℹ️ Таблица support_audit_logs уже существует')
        except Exception as e:
            logger.warning(f'⚠️ Проблемы с созданием таблицы support_audit_logs: {e}')

        logger.info('=== НАСТРОЙКА ПРОМО ГРУПП ===')
        promo_groups_ready = await ensure_promo_groups_setup()
        if promo_groups_ready:
            logger.info('✅ Промо группы готовы')
        else:
            logger.warning('⚠️ Проблемы с настройкой промо групп')

        server_promo_groups_ready = await ensure_server_promo_groups_setup()
        if server_promo_groups_ready:
            logger.info('✅ Доступ серверов по промогруппам настроен')
        else:
            logger.warning('⚠️ Проблемы с настройкой доступа серверов к промогруппам')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ ДОКУПОК ТРАФИКА ===')
        traffic_purchases_ready = await create_traffic_purchases_table()
        if traffic_purchases_ready:
            logger.info('✅ Таблица traffic_purchases готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей traffic_purchases')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦ ДЛЯ РЕЖИМА ТАРИФОВ ===')
        tariffs_table_ready = await create_tariffs_table()
        if tariffs_table_ready:
            logger.info('✅ Таблица tariffs готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей tariffs')

        tariff_promo_groups_ready = await create_tariff_promo_groups_table()
        if tariff_promo_groups_ready:
            logger.info('✅ Таблица tariff_promo_groups готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей tariff_promo_groups')

        tariff_id_column_ready = await add_subscription_tariff_id_column()
        if tariff_id_column_ready:
            logger.info('✅ Колонка tariff_id в subscriptions готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой tariff_id в subscriptions')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНОК ТАРИФОВ В РЕКЛАМНЫЕ КАМПАНИИ ===')
        campaign_tariff_columns_ready = await add_campaign_tariff_columns()
        if campaign_tariff_columns_ready:
            logger.info('✅ Колонки tariff в рекламных кампаниях готовы')
        else:
            logger.warning('⚠️ Проблемы с колонками tariff в рекламных кампаниях')

        device_price_column_ready = await add_tariff_device_price_column()
        if device_price_column_ready:
            logger.info('✅ Колонка device_price_kopeks в tariffs готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой device_price_kopeks в tariffs')

        max_device_limit_ready = await ensure_tariff_max_device_limit_column()
        if max_device_limit_ready:
            logger.info('✅ Колонка max_device_limit в tariffs готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой max_device_limit в tariffs')

        server_traffic_limits_ready = await add_tariff_server_traffic_limits_column()
        if server_traffic_limits_ready:
            logger.info('✅ Колонка server_traffic_limits в tariffs готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой server_traffic_limits в tariffs')

        allow_traffic_topup_ready = await add_tariff_allow_traffic_topup_column()
        if allow_traffic_topup_ready:
            logger.info('✅ Колонка allow_traffic_topup в tariffs готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой allow_traffic_topup в tariffs')

        traffic_topup_columns_ready = await add_tariff_traffic_topup_columns()
        if traffic_topup_columns_ready:
            logger.info('✅ Колонки докупки трафика в tariffs готовы')
        else:
            logger.warning('⚠️ Проблемы с колонками докупки трафика в tariffs')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНОК СУТОЧНЫХ ТАРИФОВ ===')
        daily_tariff_columns_ready = await add_tariff_daily_columns()
        if daily_tariff_columns_ready:
            logger.info('✅ Колонки суточных тарифов в tariffs готовы')
        else:
            logger.warning('⚠️ Проблемы с колонками суточных тарифов в tariffs')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНОК ПРОИЗВОЛЬНЫХ ДНЕЙ/ТРАФИКА ===')
        custom_days_traffic_ready = await add_tariff_custom_days_traffic_columns()
        if custom_days_traffic_ready:
            logger.info('✅ Колонки произвольных дней/трафика в tariffs готовы')
        else:
            logger.warning('⚠️ Проблемы с колонками произвольных дней/трафика в tariffs')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНКИ РЕЖИМА СБРОСА ТРАФИКА В ТАРИФАХ ===')
        traffic_reset_mode_ready = await add_tariff_traffic_reset_mode_column()
        if traffic_reset_mode_ready:
            logger.info('✅ Колонка traffic_reset_mode в tariffs готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой traffic_reset_mode в tariffs')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНОК СУТОЧНЫХ ПОДПИСОК ===')
        daily_subscription_columns_ready = await add_subscription_daily_columns()
        if daily_subscription_columns_ready:
            logger.info('✅ Колонки суточных подписок в subscriptions готовы')
        else:
            logger.warning('⚠️ Проблемы с колонками суточных подписок в subscriptions')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНКИ СБРОСА ТРАФИКА ===')
        traffic_reset_column_ready = await add_subscription_traffic_reset_at_column()
        if traffic_reset_column_ready:
            logger.info('✅ Колонка traffic_reset_at в subscriptions готова')
        else:
            logger.warning('⚠️ Проблемы с колонкой traffic_reset_at в subscriptions')

        logger.info('=== ОБНОВЛЕНИЕ ВНЕШНИХ КЛЮЧЕЙ ===')
        fk_updated = await fix_foreign_keys_for_user_deletion()
        if fk_updated:
            logger.info('✅ Внешние ключи обновлены')
        else:
            logger.warning('⚠️ Проблемы с обновлением внешних ключей')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ КОНВЕРСИЙ ПОДПИСОК ===')
        conversions_created = await create_subscription_conversions_table()
        if conversions_created:
            logger.info('✅ Таблица subscription_conversions готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей subscription_conversions')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ SUBSCRIPTION_EVENTS ===')
        events_created = await create_subscription_events_table()
        if events_created:
            logger.info('✅ Таблица subscription_events готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей subscription_events')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНОК ЧЕКОВ В TRANSACTIONS ===')
        receipt_columns_ready = await add_transaction_receipt_columns()
        if receipt_columns_ready:
            logger.info('✅ Колонки receipt_uuid и receipt_created_at готовы')
        else:
            logger.warning('⚠️ Проблемы с колонками чеков в transactions')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ WITHDRAWAL_REQUESTS ===')
        withdrawal_requests_ready = await create_withdrawal_requests_table()
        if withdrawal_requests_ready:
            logger.info('✅ Таблица withdrawal_requests готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей withdrawal_requests')

        logger.info('=== НАСТРОЙКА EMAIL АУТЕНТИФИКАЦИИ ===')
        email_auth_ready = await add_user_email_auth_columns()
        if email_auth_ready:
            logger.info('✅ Колонки для email-аутентификации готовы')
        else:
            logger.warning('⚠️ Проблемы с настройкой email-аутентификации')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦ КОЛЕСА УДАЧИ ===')
        wheel_tables_ready = await create_wheel_tables()
        if wheel_tables_ready:
            logger.info('✅ Таблицы колеса удачи готовы')
        else:
            logger.warning('⚠️ Проблемы с таблицами колеса удачи')

        logger.info('=== СОЗДАНИЕ ТАБЛИЦЫ EMAIL_TEMPLATES ===')
        email_templates_ready = await create_email_templates_table()
        if email_templates_ready:
            logger.info('✅ Таблица email_templates готова')
        else:
            logger.warning('⚠️ Проблемы с таблицей email_templates')

        logger.info('=== МИГРАЦИЯ CLOUDPAYMENTS TRANSACTION_ID НА BIGINT ===')
        cloudpayments_bigint_ready = await migrate_cloudpayments_transaction_id_to_bigint()
        if cloudpayments_bigint_ready:
            logger.info('✅ Колонка transaction_id_cp в cloudpayments_payments обновлена до BIGINT')
        else:
            logger.warning('⚠️ Проблемы с миграцией transaction_id_cp')

        logger.info('=== ДОБАВЛЕНИЕ КОЛОНОК OAUTH ПРОВАЙДЕРОВ ===')
        oauth_columns_ready = await add_oauth_provider_columns()
        if oauth_columns_ready:
            logger.info('✅ Колонки OAuth провайдеров (google_id, yandex_id, discord_id, vk_id) готовы')
        else:
            logger.warning('⚠️ Проблемы с колонками OAuth провайдеров')

        async with engine.begin() as conn:
            total_subs = await conn.execute(text('SELECT COUNT(*) FROM subscriptions'))
            unique_users = await conn.execute(text('SELECT COUNT(DISTINCT user_id) FROM subscriptions'))

            total_count = total_subs.fetchone()[0]
            unique_count = unique_users.fetchone()[0]

            logger.info(f'Всего подписок: {total_count}')
            logger.info(f'Уникальных пользователей: {unique_count}')

            if total_count == unique_count:
                logger.info('База данных уже в корректном состоянии')
                logger.info('=== МИГРАЦИЯ ЗАВЕРШЕНА УСПЕШНО ===')
                return True

        await fix_subscription_duplicates_universal()

        async with engine.begin() as conn:
            final_check = await conn.execute(
                text("""
                SELECT user_id, COUNT(*) as count
                FROM subscriptions
                GROUP BY user_id
                HAVING COUNT(*) > 1
            """)
            )

            remaining_duplicates = final_check.fetchall()

            if remaining_duplicates:
                logger.warning(f'Остались дубликаты у {len(remaining_duplicates)} пользователей')
                return False
            logger.info('=== МИГРАЦИЯ ЗАВЕРШЕНА УСПЕШНО ===')
            logger.info('✅ Реферальная система обновлена')
            logger.info('✅ CryptoBot таблица готова')
            logger.info('✅ Heleket таблица готова')
            logger.info('✅ Таблица конверсий подписок создана')
            logger.info('✅ Таблица событий подписок создана')
            logger.info('✅ Таблица welcome_texts с полем is_enabled готова')
            logger.info('✅ Медиа поля в broadcast_history добавлены')
            logger.info('✅ Дубликаты подписок исправлены')
            return True

    except Exception as e:
        logger.error(f'=== ОШИБКА ВЫПОЛНЕНИЯ МИГРАЦИИ: {e} ===')
        return False


async def check_migration_status():
    logger.info('=== ПРОВЕРКА СТАТУСА МИГРАЦИЙ ===')

    try:
        status = {
            'has_made_first_topup_column': False,
            'cryptobot_table': False,
            'heleket_table': False,
            'user_messages_table': False,
            'pinned_messages_table': False,
            'welcome_texts_table': False,
            'welcome_texts_is_enabled_column': False,
            'pinned_messages_media_columns': False,
            'pinned_messages_position_column': False,
            'pinned_messages_start_mode_column': False,
            'users_last_pinned_column': False,
            'broadcast_history_media_fields': False,
            'broadcast_history_email_fields': False,
            'subscription_duplicates': False,
            'subscription_conversions_table': False,
            'subscription_events_table': False,
            'promo_groups_table': False,
            'server_promo_groups_table': False,
            'server_squads_trial_column': False,
            'privacy_policies_table': False,
            'public_offers_table': False,
            'users_promo_group_column': False,
            'promo_groups_period_discounts_column': False,
            'promo_groups_auto_assign_column': False,
            'promo_groups_addon_discount_column': False,
            'users_auto_promo_group_assigned_column': False,
            'users_auto_promo_group_threshold_column': False,
            'users_promo_offer_discount_percent_column': False,
            'users_promo_offer_discount_source_column': False,
            'users_promo_offer_discount_expires_column': False,
            'users_referral_commission_percent_column': False,
            'users_notification_settings_column': False,
            'subscription_crypto_link_column': False,
            'subscription_modem_enabled_column': False,
            'subscription_purchased_traffic_column': False,
            'users_restriction_topup_column': False,
            'users_restriction_subscription_column': False,
            'users_restriction_reason_column': False,
            'contest_templates_prize_type_column': False,
            'contest_templates_prize_value_column': False,
            'discount_offers_table': False,
            'discount_offers_effect_column': False,
            'discount_offers_extra_column': False,
            'referral_contests_table': False,
            'referral_contest_events_table': False,
            'referral_contest_type_column': False,
            'referral_contest_summary_times_column': False,
            'referral_contest_last_summary_at_column': False,
            'contest_templates_table': False,
            'contest_rounds_table': False,
            'contest_attempts_table': False,
            'promo_offer_templates_table': False,
            'promo_offer_templates_active_discount_column': False,
            'promo_offer_logs_table': False,
            'subscription_temporary_access_table': False,
            'campaign_tariff_id_column': False,
            'campaign_tariff_duration_days_column': False,
            'campaign_registration_tariff_id_column': False,
            'campaign_registration_tariff_duration_days_column': False,
            'users_google_id_column': False,
            'users_yandex_id_column': False,
            'users_discord_id_column': False,
            'users_vk_id_column': False,
        }

        status['has_made_first_topup_column'] = await check_column_exists('users', 'has_made_first_topup')

        status['cryptobot_table'] = await check_table_exists('cryptobot_payments')
        status['heleket_table'] = await check_table_exists('heleket_payments')
        status['user_messages_table'] = await check_table_exists('user_messages')
        status['pinned_messages_table'] = await check_table_exists('pinned_messages')
        status['welcome_texts_table'] = await check_table_exists('welcome_texts')
        status['privacy_policies_table'] = await check_table_exists('privacy_policies')
        status['public_offers_table'] = await check_table_exists('public_offers')
        status['subscription_conversions_table'] = await check_table_exists('subscription_conversions')
        status['subscription_events_table'] = await check_table_exists('subscription_events')
        status['promo_groups_table'] = await check_table_exists('promo_groups')
        status['server_promo_groups_table'] = await check_table_exists('server_squad_promo_groups')
        status['server_squads_trial_column'] = await check_column_exists('server_squads', 'is_trial_eligible')

        status['discount_offers_table'] = await check_table_exists('discount_offers')
        status['discount_offers_effect_column'] = await check_column_exists('discount_offers', 'effect_type')
        status['discount_offers_extra_column'] = await check_column_exists('discount_offers', 'extra_data')
        status['referral_contests_table'] = await check_table_exists('referral_contests')
        status['referral_contest_events_table'] = await check_table_exists('referral_contest_events')
        status['referral_contest_type_column'] = await check_column_exists('referral_contests', 'contest_type')
        status['referral_contest_summary_times_column'] = await check_column_exists(
            'referral_contests', 'daily_summary_times'
        )
        status['referral_contest_last_summary_at_column'] = await check_column_exists(
            'referral_contests', 'last_daily_summary_at'
        )
        status['contest_templates_table'] = await check_table_exists('contest_templates')
        status['contest_rounds_table'] = await check_table_exists('contest_rounds')
        status['contest_attempts_table'] = await check_table_exists('contest_attempts')
        status['promo_offer_templates_table'] = await check_table_exists('promo_offer_templates')
        status['promo_offer_templates_active_discount_column'] = await check_column_exists(
            'promo_offer_templates', 'active_discount_hours'
        )
        status['promo_offer_logs_table'] = await check_table_exists('promo_offer_logs')
        status['subscription_temporary_access_table'] = await check_table_exists('subscription_temporary_access')

        # Проверяем колонки tariff в рекламных кампаниях
        status['campaign_tariff_id_column'] = await check_column_exists('advertising_campaigns', 'tariff_id')
        status['campaign_tariff_duration_days_column'] = await check_column_exists(
            'advertising_campaigns', 'tariff_duration_days'
        )
        status['campaign_registration_tariff_id_column'] = await check_column_exists(
            'advertising_campaign_registrations', 'tariff_id'
        )
        status['campaign_registration_tariff_duration_days_column'] = await check_column_exists(
            'advertising_campaign_registrations', 'tariff_duration_days'
        )

        status['welcome_texts_is_enabled_column'] = await check_column_exists('welcome_texts', 'is_enabled')
        status['users_promo_group_column'] = await check_column_exists('users', 'promo_group_id')
        status['promo_groups_period_discounts_column'] = await check_column_exists('promo_groups', 'period_discounts')
        status['promo_groups_auto_assign_column'] = await check_column_exists(
            'promo_groups', 'auto_assign_total_spent_kopeks'
        )
        status['promo_groups_addon_discount_column'] = await check_column_exists(
            'promo_groups', 'apply_discounts_to_addons'
        )
        status['users_auto_promo_group_assigned_column'] = await check_column_exists(
            'users', 'auto_promo_group_assigned'
        )
        status['users_auto_promo_group_threshold_column'] = await check_column_exists(
            'users', 'auto_promo_group_threshold_kopeks'
        )
        status['users_promo_offer_discount_percent_column'] = await check_column_exists(
            'users', 'promo_offer_discount_percent'
        )
        status['users_promo_offer_discount_source_column'] = await check_column_exists(
            'users', 'promo_offer_discount_source'
        )
        status['users_promo_offer_discount_expires_column'] = await check_column_exists(
            'users', 'promo_offer_discount_expires_at'
        )
        status['users_referral_commission_percent_column'] = await check_column_exists(
            'users', 'referral_commission_percent'
        )
        status['users_notification_settings_column'] = await check_column_exists('users', 'notification_settings')
        status['users_auth_type_column'] = await check_column_exists('users', 'auth_type')
        status['subscription_crypto_link_column'] = await check_column_exists(
            'subscriptions', 'subscription_crypto_link'
        )
        status['subscription_modem_enabled_column'] = await check_column_exists('subscriptions', 'modem_enabled')
        status['subscription_purchased_traffic_column'] = await check_column_exists(
            'subscriptions', 'purchased_traffic_gb'
        )
        status['users_restriction_topup_column'] = await check_column_exists('users', 'restriction_topup')
        status['users_restriction_subscription_column'] = await check_column_exists('users', 'restriction_subscription')
        status['users_restriction_reason_column'] = await check_column_exists('users', 'restriction_reason')
        status['contest_templates_prize_type_column'] = await check_column_exists('contest_templates', 'prize_type')
        status['contest_templates_prize_value_column'] = await check_column_exists('contest_templates', 'prize_value')

        media_fields_exist = (
            await check_column_exists('broadcast_history', 'has_media')
            and await check_column_exists('broadcast_history', 'media_type')
            and await check_column_exists('broadcast_history', 'media_file_id')
            and await check_column_exists('broadcast_history', 'media_caption')
        )
        status['broadcast_history_media_fields'] = media_fields_exist

        email_fields_exist = (
            await check_column_exists('broadcast_history', 'channel')
            and await check_column_exists('broadcast_history', 'email_subject')
            and await check_column_exists('broadcast_history', 'email_html_content')
        )
        status['broadcast_history_email_fields'] = email_fields_exist

        pinned_media_columns_exist = (
            status['pinned_messages_table']
            and await check_column_exists('pinned_messages', 'media_type')
            and await check_column_exists('pinned_messages', 'media_file_id')
        )
        status['pinned_messages_media_columns'] = pinned_media_columns_exist

        status['pinned_messages_position_column'] = status['pinned_messages_table'] and await check_column_exists(
            'pinned_messages', 'send_before_menu'
        )

        status['pinned_messages_start_mode_column'] = status['pinned_messages_table'] and await check_column_exists(
            'pinned_messages', 'send_on_every_start'
        )

        status['users_last_pinned_column'] = await check_column_exists('users', 'last_pinned_message_id')

        # Колонки чеков в transactions
        status['transactions_receipt_uuid_column'] = await check_column_exists('transactions', 'receipt_uuid')
        status['transactions_receipt_created_at_column'] = await check_column_exists(
            'transactions', 'receipt_created_at'
        )

        # Колонки OAuth провайдеров в users
        status['users_google_id_column'] = await check_column_exists('users', 'google_id')
        status['users_yandex_id_column'] = await check_column_exists('users', 'yandex_id')
        status['users_discord_id_column'] = await check_column_exists('users', 'discord_id')
        status['users_vk_id_column'] = await check_column_exists('users', 'vk_id')

        async with engine.begin() as conn:
            duplicates_check = await conn.execute(
                text("""
                SELECT COUNT(*) FROM (
                    SELECT user_id, COUNT(*) as count
                    FROM subscriptions
                    GROUP BY user_id
                    HAVING COUNT(*) > 1
                ) as dups
            """)
            )
            duplicates_count = duplicates_check.fetchone()[0]
            status['subscription_duplicates'] = duplicates_count == 0

        check_names = {
            'has_made_first_topup_column': 'Колонка реферальной системы',
            'cryptobot_table': 'Таблица CryptoBot payments',
            'heleket_table': 'Таблица Heleket payments',
            'user_messages_table': 'Таблица пользовательских сообщений',
            'pinned_messages_table': 'Таблица закреплённых сообщений',
            'welcome_texts_table': 'Таблица приветственных текстов',
            'privacy_policies_table': 'Таблица политик конфиденциальности',
            'public_offers_table': 'Таблица публичных оферт',
            'welcome_texts_is_enabled_column': 'Поле is_enabled в welcome_texts',
            'pinned_messages_media_columns': 'Медиа поля в pinned_messages',
            'pinned_messages_position_column': 'Позиция закрепа (до/после меню)',
            'pinned_messages_start_mode_column': 'Режим отправки закрепа при /start',
            'users_last_pinned_column': 'Колонка last_pinned_message_id у пользователей',
            'broadcast_history_media_fields': 'Медиа поля в broadcast_history',
            'broadcast_history_email_fields': 'Email поля в broadcast_history',
            'subscription_conversions_table': 'Таблица конверсий подписок',
            'subscription_events_table': 'Таблица событий подписок',
            'subscription_duplicates': 'Отсутствие дубликатов подписок',
            'promo_groups_table': 'Таблица промо-групп',
            'server_promo_groups_table': 'Связи серверов и промогрупп',
            'server_squads_trial_column': 'Колонка триального назначения у серверов',
            'users_promo_group_column': 'Колонка promo_group_id у пользователей',
            'promo_groups_period_discounts_column': 'Колонка period_discounts у промо-групп',
            'promo_groups_auto_assign_column': 'Колонка auto_assign_total_spent_kopeks у промо-групп',
            'promo_groups_addon_discount_column': 'Колонка apply_discounts_to_addons у промо-групп',
            'users_auto_promo_group_assigned_column': 'Флаг автоназначения промогруппы у пользователей',
            'users_auto_promo_group_threshold_column': 'Порог последней авто-промогруппы у пользователей',
            'users_promo_offer_discount_percent_column': 'Колонка процента промо-скидки у пользователей',
            'users_promo_offer_discount_source_column': 'Колонка источника промо-скидки у пользователей',
            'users_promo_offer_discount_expires_column': 'Колонка срока действия промо-скидки у пользователей',
            'users_referral_commission_percent_column': 'Колонка процента реферальной комиссии у пользователей',
            'users_notification_settings_column': 'Колонка notification_settings у пользователей',
            'users_auth_type_column': 'Колонка auth_type у пользователей (email-регистрация)',
            'subscription_crypto_link_column': 'Колонка subscription_crypto_link в subscriptions',
            'subscription_modem_enabled_column': 'Колонка modem_enabled в subscriptions',
            'subscription_purchased_traffic_column': 'Колонка purchased_traffic_gb в subscriptions',
            'contest_templates_prize_type_column': 'Колонка prize_type в contest_templates',
            'contest_templates_prize_value_column': 'Колонка prize_value в contest_templates',
            'discount_offers_table': 'Таблица discount_offers',
            'discount_offers_effect_column': 'Колонка effect_type в discount_offers',
            'discount_offers_extra_column': 'Колонка extra_data в discount_offers',
            'referral_contests_table': 'Таблица referral_contests',
            'referral_contest_events_table': 'Таблица referral_contest_events',
            'referral_contest_type_column': 'Колонка contest_type в referral_contests',
            'referral_contest_summary_times_column': 'Колонка daily_summary_times в referral_contests',
            'referral_contest_last_summary_at_column': 'Колонка last_daily_summary_at в referral_contests',
            'contest_templates_table': 'Таблица contest_templates',
            'contest_rounds_table': 'Таблица contest_rounds',
            'contest_attempts_table': 'Таблица contest_attempts',
            'promo_offer_templates_table': 'Таблица promo_offer_templates',
            'promo_offer_templates_active_discount_column': 'Колонка active_discount_hours в promo_offer_templates',
            'promo_offer_logs_table': 'Таблица promo_offer_logs',
            'subscription_temporary_access_table': 'Таблица subscription_temporary_access',
            'transactions_receipt_uuid_column': 'Колонка receipt_uuid в transactions',
            'transactions_receipt_created_at_column': 'Колонка receipt_created_at в transactions',
            'users_google_id_column': 'Колонка google_id в users',
            'users_yandex_id_column': 'Колонка yandex_id в users',
            'users_discord_id_column': 'Колонка discord_id в users',
            'users_vk_id_column': 'Колонка vk_id в users',
        }

        for check_key, check_status in status.items():
            check_name = check_names.get(check_key, check_key)
            icon = '✅' if check_status else '❌'
            logger.info(f'{icon} {check_name}: {"OK" if check_status else "ТРЕБУЕТ ВНИМАНИЯ"}')

        all_good = all(status.values())
        if all_good:
            logger.info('🎉 Все миграции выполнены успешно!')

            try:
                async with engine.begin() as conn:
                    conversions_count = await conn.execute(text('SELECT COUNT(*) FROM subscription_conversions'))
                    users_count = await conn.execute(text('SELECT COUNT(*) FROM users'))
                    welcome_texts_count = await conn.execute(text('SELECT COUNT(*) FROM welcome_texts'))
                    broadcasts_count = await conn.execute(text('SELECT COUNT(*) FROM broadcast_history'))

                    conv_count = conversions_count.fetchone()[0]
                    usr_count = users_count.fetchone()[0]
                    welcome_count = welcome_texts_count.fetchone()[0]
                    broadcast_count = broadcasts_count.fetchone()[0]

                    logger.info(
                        f'📊 Статистика: {usr_count} пользователей, {conv_count} конверсий, {welcome_count} приветственных текстов, {broadcast_count} рассылок'
                    )
            except Exception as stats_error:
                logger.debug(f'Не удалось получить дополнительную статистику: {stats_error}')

        else:
            logger.warning('⚠️ Некоторые миграции требуют внимания')
            missing_migrations = [check_names[k] for k, v in status.items() if not v]
            logger.warning(f'Требуют выполнения: {", ".join(missing_migrations)}')

        return status

    except Exception as e:
        logger.error(f'Ошибка проверки статуса миграций: {e}')
        return None
