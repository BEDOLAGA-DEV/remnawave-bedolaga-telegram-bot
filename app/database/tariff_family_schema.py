"""PostgreSQL DDL для инварианта: одно живое entitlement на семейство.

Идентификатор семейства находится в Tariff, а не в Subscription, поэтому
обычный UNIQUE INDEX по двум таблицам невозможен. Advisory xact lock
сериализует конкурирующие записи по (user_id, family); после получения
блокировки триггер перечитывает актуальные строки в READ COMMITTED.

Эти DDL-строки — неизменяемый контракт миграции 0133 и create_all.
"""

PG_FAMILY_DDL = (
    """
    CREATE OR REPLACE FUNCTION bedolaga_guard_subscription_family_v1()
    RETURNS trigger LANGUAGE plpgsql AS $body$
    DECLARE
        family_key varchar(64);
    BEGIN
        IF NEW.tariff_id IS NULL OR NEW.status NOT IN ('active', 'trial', 'limited') THEN
            RETURN NEW;
        END IF;

        -- Блокировка тарифа синхронизирует INSERT с изменением его семейства.
        SELECT t.entitlement_family INTO family_key
        FROM tariffs AS t WHERE t.id = NEW.tariff_id FOR SHARE;
        IF family_key IS NULL THEN
            RETURN NEW;
        END IF;

        -- Коллизии hashtext ведут лишь к избыточному ожиданию, не к отказу.
        PERFORM pg_advisory_xact_lock(NEW.user_id, hashtext(family_key));
        IF EXISTS (
            SELECT 1
            FROM subscriptions AS s
            JOIN tariffs AS t ON t.id = s.tariff_id
            WHERE s.user_id = NEW.user_id
              AND s.status IN ('active', 'trial', 'limited')
              AND t.entitlement_family = family_key
              AND s.id IS DISTINCT FROM NEW.id
        ) THEN
            RAISE EXCEPTION 'active entitlement family conflict'
                USING ERRCODE = '23505',
                      CONSTRAINT = 'uq_subscriptions_user_family_live';
        END IF;
        RETURN NEW;
    END
    $body$;
    """,
    """
    CREATE TRIGGER trg_bedolaga_subscription_family_v1
    BEFORE INSERT OR UPDATE OF user_id, tariff_id, status ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION bedolaga_guard_subscription_family_v1();
    """,
    """
    CREATE OR REPLACE FUNCTION bedolaga_guard_tariff_family_v1()
    RETURNS trigger LANGUAGE plpgsql AS $body$
    DECLARE
        account_id integer;
        family_count integer;
    BEGIN
        IF NEW.entitlement_family IS NULL OR
           NEW.entitlement_family IS NOT DISTINCT FROM OLD.entitlement_family THEN
            RETURN NEW;
        END IF;

        -- Порядок user_id исключает перевёрнутый порядок advisory locks.
        FOR account_id IN
            SELECT DISTINCT s.user_id FROM subscriptions AS s
            WHERE s.tariff_id = NEW.id
              AND s.status IN ('active', 'trial', 'limited')
            ORDER BY s.user_id
        LOOP
            PERFORM pg_advisory_xact_lock(account_id, hashtext(NEW.entitlement_family));
            SELECT count(*) INTO family_count
            FROM subscriptions AS s
            JOIN tariffs AS t ON t.id = s.tariff_id
            WHERE s.user_id = account_id
              AND s.status IN ('active', 'trial', 'limited')
              AND t.entitlement_family = NEW.entitlement_family;
            IF family_count > 1 THEN
                RAISE EXCEPTION 'active entitlement family conflict on tariff update'
                    USING ERRCODE = '23505',
                          CONSTRAINT = 'uq_subscriptions_user_family_live';
            END IF;
        END LOOP;
        RETURN NEW;
    END
    $body$;
    """,
    """
    CREATE TRIGGER trg_bedolaga_tariff_family_v1
    AFTER UPDATE OF entitlement_family ON tariffs
    FOR EACH ROW EXECUTE FUNCTION bedolaga_guard_tariff_family_v1();
    """,
)

PG_FAMILY_DROP_DDL = (
    'DROP TRIGGER IF EXISTS trg_bedolaga_tariff_family_v1 ON tariffs',
    'DROP TRIGGER IF EXISTS trg_bedolaga_subscription_family_v1 ON subscriptions',
    'DROP FUNCTION IF EXISTS bedolaga_guard_tariff_family_v1()',
    'DROP FUNCTION IF EXISTS bedolaga_guard_subscription_family_v1()',
)
