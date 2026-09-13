"""Security regressions M13: полномочия роли и недоверенная staging schema."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import AdaptedConnection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry, NullPool
from tests.fakes.staging import StagingCase, StagingClock
from tests.integration.database.test_postgresql_staging import Targets
from tests.integration.database.test_postgresql_staging import case as case
from tests.integration.database.test_postgresql_staging import clock as clock
from tests.integration.database.test_postgresql_staging import policy as policy
from tests.integration.database.test_postgresql_staging import store as store
from tests.integration.database.test_postgresql_staging import targets as targets

from structuraguard.contracts.staging import StagingLimits, StagingRetentionPolicy
from structuraguard.exceptions import StagingError
from structuraguard.stores import PostgreSQLStagingStore
from structuraguard.stores import postgresql as staging_pg

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.integration,
    pytest.mark.database_integration,
]


@pytest.fixture
async def admin(targets: Targets) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        targets.admin.dsn.get_secret_value(), poolclass=NullPool
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "privilege", ["function_owner", "noinherit_owner", "server_file_role"]
)
async def test_staging_rejects_ddl_and_privileged_role_memberships(
    admin: AsyncEngine,
    targets: Targets,
    store: PostgreSQLStagingStore,
    case: StagingCase,
    privilege: str,
) -> None:
    schema = targets.admin.schema_name
    owner = schema + "_owner"
    try:
        async with admin.begin() as connection:
            if privilege == "function_owner":
                await connection.exec_driver_sql(
                    f'CREATE FUNCTION "{schema}".owned() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$'
                )
                await connection.exec_driver_sql(
                    f'ALTER FUNCTION "{schema}".owned() OWNER TO staging_writer'
                )
            elif privilege == "noinherit_owner":
                await connection.exec_driver_sql(f'CREATE ROLE "{owner}" NOLOGIN')
                await connection.exec_driver_sql("ALTER ROLE staging_writer NOINHERIT")
                await connection.exec_driver_sql(f'GRANT "{owner}" TO staging_writer')
                await connection.exec_driver_sql(
                    f'CREATE TABLE "{schema}".owned(id integer)'
                )
                await connection.exec_driver_sql(
                    f'ALTER TABLE "{schema}".owned OWNER TO "{owner}"'
                )
            else:
                await connection.exec_driver_sql(
                    "GRANT pg_read_server_files TO staging_writer"
                )
        with pytest.raises(StagingError, match="STAGING_PRINCIPAL_FORBIDDEN"):
            await store.begin(case.spec)
        async with admin.connect() as connection:
            assert (
                await connection.exec_driver_sql(
                    f'SELECT count(*) FROM "{schema}".runs'
                )
            ).scalar_one() == 0
    finally:
        async with admin.begin() as connection:
            if privilege == "noinherit_owner":
                await connection.exec_driver_sql(
                    f'DROP TABLE IF EXISTS "{schema}".owned'
                )
                await connection.exec_driver_sql(
                    f'REVOKE "{owner}" FROM staging_writer'
                )
                await connection.exec_driver_sql(f'DROP ROLE "{owner}"')
                await connection.exec_driver_sql("ALTER ROLE staging_writer INHERIT")
            elif privilege == "server_file_role":
                await connection.exec_driver_sql(
                    "REVOKE pg_read_server_files FROM staging_writer"
                )


async def test_staging_rejects_custom_index_code_before_executing_it(
    admin: AsyncEngine,
    targets: Targets,
    store: PostgreSQLStagingStore,
    case: StagingCase,
) -> None:
    schema = targets.admin.schema_name
    await store.begin(case.spec)
    another = case.spec.model_copy(
        update={
            "context": case.spec.context.model_copy(update={"run_id": "second-run"})
        }
    )
    async with admin.begin() as connection:
        await connection.exec_driver_sql(
            f'CREATE TABLE "{schema}".executed(secret text)'
        )
        await connection.exec_driver_sql(f'''CREATE FUNCTION "{schema}".compare(text,text) RETURNS integer
            LANGUAGE plpgsql VOLATILE SECURITY DEFINER AS $$ BEGIN
            INSERT INTO "{schema}".executed VALUES ('untrusted-index-executed');
            RETURN pg_catalog.bttextcmp($1,$2); END $$''')
        # Btree support function запускается сервером при обычном INSERT.
        await connection.exec_driver_sql(f'''CREATE OPERATOR CLASS "{schema}".hostile_ops FOR TYPE text USING btree AS
            OPERATOR 1 pg_catalog.< (text,text), OPERATOR 2 pg_catalog.<= (text,text),
            OPERATOR 3 pg_catalog.= (text,text), OPERATOR 4 pg_catalog.>= (text,text),
            OPERATOR 5 pg_catalog.> (text,text), FUNCTION 1 "{schema}".compare(text,text)''')
        await connection.exec_driver_sql(
            f'CREATE INDEX hostile ON "{schema}".runs (run_id "{schema}".hostile_ops)'
        )
    code = None
    try:
        await store.begin(another)
    except StagingError as error:
        code = error.error_code
    async with admin.connect() as connection:
        assert (
            await connection.exec_driver_sql(
                f'SELECT count(*) FROM "{schema}".executed'
            )
        ).scalar_one() == 0
        assert (
            await connection.exec_driver_sql(f'SELECT count(*) FROM "{schema}".runs')
        ).scalar_one() == 1
    assert code == "STAGING_SCHEMA_UNAVAILABLE"


async def test_staging_rejects_nondeterministic_namespace_collation(
    admin: AsyncEngine,
    targets: Targets,
    store: PostgreSQLStagingStore,
    case: StagingCase,
    clock: StagingClock,
    policy: StagingRetentionPolicy,
) -> None:
    schema = targets.admin.schema_name
    await store.begin(case.spec)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(
            f"CREATE COLLATION \"{schema}\".fold (provider=icu, locale='und-u-ks-level2', deterministic=false)"
        )
        for name in ("records", "batches"):
            await connection.exec_driver_sql(
                f'ALTER TABLE "{schema}".{name} DROP CONSTRAINT {name}_namespace_target_id_run_id_fkey'
            )
        for name in ("runs", "records", "batches"):
            await connection.exec_driver_sql(
                f'ALTER TABLE "{schema}".{name} ALTER COLUMN namespace TYPE text COLLATE "{schema}".fold'
            )
        for name in ("records", "batches"):
            await connection.exec_driver_sql(
                f'ALTER TABLE "{schema}".{name} ADD FOREIGN KEY (namespace,target_id,run_id) REFERENCES "{schema}".runs(namespace,target_id,run_id)'
            )
    other = PostgreSQLStagingStore(
        targets.writer.model_copy(update={"namespace": "APP-1"}),
        retention=policy,
        clock=clock,
    )
    with pytest.raises(StagingError, match="STAGING_SCHEMA_UNAVAILABLE"):
        await other.get_run(case.spec.context)


def observe_decoded_text(engine: AsyncEngine, sizes: list[int]) -> None:
    """Измерить реальные строки PG wire до model parsing, без проверки SQL spelling."""

    def connect(connection: AdaptedConnection, _: ConnectionPoolEntry) -> None:
        async def configure(driver: object) -> None:
            from asyncpg import Connection

            assert isinstance(driver, Connection)

            def decode(value: str) -> str:
                sizes.append(len(value.encode()))
                return value

            await driver.set_type_codec(
                "text", schema="pg_catalog", encoder=str, decoder=decode, format="text"
            )

        connection.run_async(configure)

    event.listen(engine.sync_engine, "connect", connect)


@pytest.mark.parametrize(
    "table,column",
    [("runs", "fingerprint"), ("schema_info", "fingerprint"), ("runs", "payload")],
)
async def test_persisted_metadata_text_is_bounded_before_transfer_to_python(
    admin: AsyncEngine,
    targets: Targets,
    case: StagingCase,
    clock: StagingClock,
    policy: StagingRetentionPolicy,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    column: str,
) -> None:
    limits = StagingLimits(max_input_bytes=65536)
    reader = PostgreSQLStagingStore(
        targets.writer, retention=policy, limits=limits, clock=clock
    )
    await reader.begin(case.spec)
    async with admin.begin() as connection:
        await connection.execute(
            text(
                f'UPDATE "{targets.admin.schema_name}"."{table}" SET "{column}"=:value'
            ),
            {
                "value": "🔐" * 65536
                if column == "payload"
                else "sensitive-fingerprint-" * 10000
            },
        )
    sizes: list[int] = []
    original = staging_pg._engine

    def engine(
        target: staging_pg.PostgreSQLStagingTarget, budget: StagingLimits
    ) -> AsyncEngine:
        result = original(target, budget)
        observe_decoded_text(result, sizes)
        return result

    monkeypatch.setattr(staging_pg, "_engine", engine)
    with pytest.raises(
        StagingError,
        match="STAGING_LIMIT_EXCEEDED"
        if column == "payload"
        else "STAGING_CONTENT_MISMATCH"
        if table == "runs"
        else "STAGING_SCHEMA_UNAVAILABLE",
    ):
        await reader.get_run(case.spec.context)
    assert sizes and max(sizes) <= limits.max_input_bytes
