import collections
import dataclasses
import datetime
import functools
import hashlib
import inspect
import typing
import urllib.parse

import quart
import quart.sessions
import sqlalchemy
import sqlalchemy.ext.asyncio
import sqlalchemy.ext.asyncio.engine
import sqlalchemy.orm
import sqlalchemy.sql
import sqlalchemy.sql.functions

from support.telemetry import otel, otel_add_event

from .constants import AppConstants
from .db import AppAccessType, AppDatabase, AppTables
from .sso import AppSSO


@dataclasses.dataclass(frozen=True)
class AppRequest:
    ts: datetime.datetime = dataclasses.field(default_factory=functools.partial(datetime.datetime.now, tz=datetime.timezone.utc))
    session: quart.sessions.SessionMixin = quart.session
    character_id: int = 0
    corpporation_id: int = 0
    alliance_id: int = 0
    permitted: bool = False
    trusted: bool = False
    contributor: bool = False
    suspect: bool = False
    magic_character: bool = False


class AppFunctions:

    @staticmethod
    @otel
    async def get_app_request(evedb: AppDatabase, session: quart.sessions.SessionMixin, request: quart.Request) -> AppRequest:

        character_id: typing.Final = session.get(AppSSO.ESI_CHARACTER_ID, 0)
        corpporation_id: typing.Final = session.get(AppSSO.ESI_CORPORATION_ID, 0)
        alliance_id: typing.Final = session.get(AppSSO.ESI_ALLIANCE_ID, 0)

        permitted: typing.Final = await AppFunctions.is_permitted(evedb, character_id, corpporation_id, alliance_id)
        trusted: typing.Final = await AppFunctions.is_trusted(evedb, character_id, corpporation_id, alliance_id)
        contributor: typing.Final = await AppFunctions.is_contributor(evedb, character_id, corpporation_id, alliance_id)
        suspect: typing.Final = character_id in AppConstants.MAGIC_SUSPECTS
        magic_character: typing.Final = character_id in AppConstants.MAGIC_ADMINS | AppConstants.MAGIC_CONTRIBUTORS


        session[AppSSO.REQUEST_PATH] = quart.request.path

        ar = AppRequest(session=session,
                        character_id=character_id,
                        corpporation_id=corpporation_id,
                        alliance_id=alliance_id,
                        permitted=permitted,
                        trusted=trusted,
                        contributor=contributor,
                        suspect=suspect,
                        magic_character=magic_character)

        if ar.character_id > 0:
            try:
                async with await evedb.sessionmaker() as db_session, db_session.begin():
                    db_session: sqlalchemy.ext.asyncio.AsyncSession
                    db_session.add(AppTables.AccessHistory(character_id=ar.character_id, permitted=bool(ar.permitted), path=request.path))
                    await db_session.commit()
            except Exception:
                pass

        if any([character_id > 0, corpporation_id > 0, alliance_id > 0]):
            otel_add_event(str(inspect.currentframe().f_code.co_name), {k: v for k, v in ar.__dict__.items() if type(v).__name__ in ['bool', 'str', 'bytes', 'int', 'float']})

        return ar

    @staticmethod
    @otel
    async def get_active_timers(session: sqlalchemy.ext.asyncio.AsyncSession, now: datetime.datetime) -> list[AppTables.Structure]:

        timer_window: typing.Final = datetime.timedelta(minutes=15)
        query = (
            sqlalchemy.select(AppTables.Structure)
            .where(
                AppTables.Structure.state_timer_end > now - timer_window,
            )
            .join(AppTables.Structure.system)
            .join(AppTables.Structure.corporation)
            .order_by(AppTables.Structure.state_timer_end)
            .options(sqlalchemy.orm.selectinload(AppTables.Structure.system))
            .options(sqlalchemy.orm.selectinload(AppTables.Structure.corporation))
        )

        return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def get_completed_extractions(session: sqlalchemy.ext.asyncio.AsyncSession, now: datetime.datetime) -> list[AppTables.CompletedExtraction]:

        query = (
            sqlalchemy.select(AppTables.CompletedExtraction)
            .where(
                sqlalchemy.and_(
                    AppTables.CompletedExtraction.belt_decay_time > now,
                    AppTables.CompletedExtraction.chunk_arrival_time <= now,
                )
            )
            .order_by(AppTables.CompletedExtraction.chunk_arrival_time)
            .options(sqlalchemy.orm.selectinload(AppTables.CompletedExtraction.structure))
            .options(sqlalchemy.orm.selectinload(AppTables.CompletedExtraction.corporation))
            .options(sqlalchemy.orm.selectinload(AppTables.CompletedExtraction.moon))
        )

        return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def get_scheduled_extractions(session: sqlalchemy.ext.asyncio.AsyncSession, now: datetime.datetime) -> list[AppTables.ScheduledExtraction]:

        query = (
            sqlalchemy.select(AppTables.ScheduledExtraction)
            .where(
                sqlalchemy.and_(
                    AppTables.ScheduledExtraction.natural_decay_time > now,
                    AppTables.ScheduledExtraction.extraction_start_time <= now,
                )
            )
            .order_by(AppTables.ScheduledExtraction.chunk_arrival_time)
            .options(sqlalchemy.orm.selectinload(AppTables.ScheduledExtraction.structure))
            .options(sqlalchemy.orm.selectinload(AppTables.ScheduledExtraction.corporation))
            .options(sqlalchemy.orm.selectinload(AppTables.ScheduledExtraction.moon))
        )

        return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def get_unscheduled_structures(session: sqlalchemy.ext.asyncio.AsyncSession, now: datetime.datetime) -> list[AppTables.CompletedExtraction]:

        query = (
            sqlalchemy.select(AppTables.CompletedExtraction)
            .where(
                AppTables.CompletedExtraction.structure_id.in_(
                    sqlalchemy.select(AppTables.Structure.structure_id)
                    .where(
                        sqlalchemy.and_(
                            AppTables.Structure.has_moon_drill == sqlalchemy.sql.expression.true(),
                            AppTables.Structure.structure_id.notin_(
                                sqlalchemy.select(AppTables.ScheduledExtraction.structure_id)
                                .where(now <= AppTables.ScheduledExtraction.natural_decay_time)
                            ),
                        )
                    )
                )
            )
            .order_by(AppTables.CompletedExtraction.natural_decay_time)
            .options(sqlalchemy.orm.selectinload(AppTables.CompletedExtraction.structure))
            .options(sqlalchemy.orm.selectinload(AppTables.CompletedExtraction.corporation))
            .options(sqlalchemy.orm.selectinload(AppTables.CompletedExtraction.moon))
        )

        return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def get_structure_fuel_expiries(session: sqlalchemy.ext.asyncio.AsyncSession, now: datetime.datetime) -> list[AppTables.Structure]:

        query = (
            sqlalchemy.select(AppTables.Structure)
            .where(
                sqlalchemy.and_(
                    AppTables.Structure.fuel_expires != sqlalchemy.sql.expression.null(),
                    AppTables.Structure.fuel_expires > now,
                )
            )
            .join(AppTables.Structure.system)
            .join(AppTables.Structure.corporation)
            .order_by(AppTables.Structure.fuel_expires)
            .options(sqlalchemy.orm.selectinload(AppTables.Structure.system))
            .options(sqlalchemy.orm.selectinload(AppTables.Structure.corporation))
        )

        return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def get_structures_without_fuel(session: sqlalchemy.ext.asyncio.AsyncSession, now: datetime.datetime) -> list[AppTables.Structure]:

        query = (
            sqlalchemy.select(AppTables.Structure)
            .where(
                sqlalchemy.and_(
                    AppTables.Structure.fuel_expires == sqlalchemy.sql.expression.null(),
                    AppTables.Structure.state.not_in(["anchoring"])
                )
            )
            .join(AppTables.Structure.system)
            .join(AppTables.Structure.corporation)
            .order_by(AppTables.Structure.corporation_id, sqlalchemy.desc(AppTables.Structure.timestamp))
            .options(sqlalchemy.orm.selectinload(AppTables.Structure.system))
            .options(sqlalchemy.orm.selectinload(AppTables.Structure.corporation))
        )

        return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def get_structure_counts(session: sqlalchemy.ext.asyncio.AsyncSession, now: datetime.datetime) -> dict[int, int]:
        query = (
            sqlalchemy.select(AppTables.Structure)
            .where(
                sqlalchemy.and_(
                    AppTables.Structure.fuel_expires != sqlalchemy.sql.expression.null(),
                    AppTables.Structure.fuel_expires > now,
                )
            )
        )

        results: dict[int, int] = collections.defaultdict(int)
        async for x in await session.stream_scalars(query):
            x: AppTables.Structure
            results[x.corporation_id] += 1

        return results

    @staticmethod
    @otel
    async def get_refresh_times(session: sqlalchemy.ext.asyncio.AsyncSession, now: datetime.datetime) -> list[AppTables.PeriodicTaskTimestamp]:

        start_time = now - datetime.timedelta(days=6)
        query = (
            sqlalchemy.select(AppTables.PeriodicTaskTimestamp)
            .where(
                sqlalchemy.and_(
                    AppTables.PeriodicTaskTimestamp.timestamp > start_time,
                    AppTables.PeriodicTaskTimestamp.corporation_id.in_(
                        sqlalchemy.select(sqlalchemy.distinct(AppTables.PeriodicCredentials.corporation_id))
                        .where(AppTables.PeriodicCredentials.is_enabled.is_(True))
                    )
                )
            )
            .order_by(sqlalchemy.desc(AppTables.PeriodicTaskTimestamp.timestamp))
        )

        return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def get_moon_yield(session: sqlalchemy.ext.asyncio.AsyncSession, moon_id: int, now: datetime.datetime) -> list[AppTables.MoonYield]:

        query = (
            sqlalchemy.select(AppTables.MoonYield)
            .where(AppTables.MoonYield.moon_id == moon_id)
            .order_by(sqlalchemy.desc(AppTables.MoonYield.yield_percent))
        )

        return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def get_moon_history(session: sqlalchemy.ext.asyncio.AsyncSession, moon_id: int, now: datetime.datetime) -> list[AppTables.ExtractionHistory]:

        query: typing.Final = (
            sqlalchemy.select(AppTables.ExtractionHistory)
            .where(
                sqlalchemy.and_(
                    AppTables.ExtractionHistory.exists == sqlalchemy.sql.expression.true(),
                    AppTables.ExtractionHistory.moon_id == moon_id,
                    AppTables.ExtractionHistory.chunk_arrival_time <= now,
                )
            )
            .order_by(sqlalchemy.desc(AppTables.ExtractionHistory.chunk_arrival_time))
            # .options(sqlalchemy.orm.selectinload(EveTables.ExtractionHistory.corporation))
            # .options(sqlalchemy.orm.selectinload(EveTables.ExtractionHistory.moon))
            .limit(10)
        )

        results = list()
        async for x in await session.stream_scalars(query):
            x: AppTables.ExtractionHistory
            cat: datetime.datetime = x.chunk_arrival_time
            results.append({
                'extraction': x,
                'dow': cat.isoweekday() - 1,
                'tod': cat.hour // 3,
            })
        return results

    @staticmethod
    @otel
    async def get_moon_structure(session: sqlalchemy.ext.asyncio.AsyncSession, moon_id: int, now: datetime.datetime) -> AppTables.StructureHistory:

        structure_id = None
        if structure_id is None:
            query = (
                sqlalchemy.select(
                    AppTables.ScheduledExtraction.structure_id
                )
                .where(
                    sqlalchemy.and_(
                        AppTables.ScheduledExtraction.moon_id == moon_id,
                        AppTables.ScheduledExtraction.chunk_arrival_time > now,
                    )
                )
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            structure_id = query_result.scalar_one_or_none()

        if structure_id is None:
            query = (
                sqlalchemy.select(
                    AppTables.ExtractionHistory.structure_id
                )
                .where(
                    sqlalchemy.and_(
                        AppTables.ExtractionHistory.exists == sqlalchemy.sql.expression.true(),
                        AppTables.ExtractionHistory.moon_id == moon_id,
                        AppTables.ExtractionHistory.chunk_arrival_time <= now,
                    )
                )
                .order_by(sqlalchemy.desc(AppTables.ExtractionHistory.chunk_arrival_time))
                .limit(1)
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            structure_id = query_result.scalar_one_or_none()

        if structure_id is None:
            return None

        query = (
            sqlalchemy.select(AppTables.StructureHistory)
            .where(
                sqlalchemy.and_(
                    AppTables.StructureHistory.exists == sqlalchemy.sql.expression.true(),
                    AppTables.StructureHistory.structure_id == structure_id,
                )
            )
            .order_by(sqlalchemy.desc(AppTables.StructureHistory.id))
            .limit(1)
            .options(sqlalchemy.orm.selectinload(AppTables.StructureHistory.corporation))
        )

        query_result: sqlalchemy.engine.Result = await session.execute(query)
        moon_structure = query_result.scalar_one_or_none()
        return moon_structure

    @staticmethod
    @otel
    async def get_moon_mining_history(session: sqlalchemy.ext.asyncio.AsyncSession, moon_id: int, now: datetime.datetime) -> list[AppTables.ExtractionHistory]:

        query = (
            sqlalchemy.select(
                AppTables.ExtractionHistory.structure_id,
                AppTables.ExtractionHistory.corporation_id,
                AppTables.ExtractionHistory.chunk_arrival_time
            )
            .where(
                sqlalchemy.and_(
                    AppTables.ExtractionHistory.exists == sqlalchemy.sql.expression.true(),
                    AppTables.ExtractionHistory.moon_id == moon_id,
                    AppTables.ExtractionHistory.chunk_arrival_time <= now,
                )
            )
            .order_by(sqlalchemy.desc(AppTables.ExtractionHistory.chunk_arrival_time))
            .limit(1)
        )

        query_result: sqlalchemy.engine.Result = await session.execute(query)
        observer_id, corporation_id, chunk_arrival_time = query_result.one_or_none()

        # XXX Don't report this corp's history
        if corporation_id in [98540393]:
            return list()

        previous_chunk_arrival_date = None
        if isinstance(chunk_arrival_time, datetime.datetime):
            previous_chunk_arrival_date = chunk_arrival_time.date()

        if observer_id is None or previous_chunk_arrival_date is None:
            return list()

        observer_history_id_query = (
            sqlalchemy.select(sqlalchemy.sql.functions.max(AppTables.ObserverHistory.id))
            .where(
                sqlalchemy.and_(
                    AppTables.ObserverHistory.observer_id == observer_id,
                    AppTables.ObserverHistory.last_updated >= previous_chunk_arrival_date
                )
            )
        )

        max_id_sq_result: sqlalchemy.engine.Result = await session.execute(observer_history_id_query)
        observer_history_id = max_id_sq_result.scalar_one_or_none()

        if observer_id is None or previous_chunk_arrival_date is None or observer_history_id is None:
            return list()

        # print(f"{observer_id=}, {previous_chunk_arrival_date=}, {observer_history_id=}")

        q = (
            sqlalchemy.select(
                AppTables.ObserverRecordHistory.observer_id,
                AppTables.ObserverRecordHistory.character_id,
                AppTables.ObserverRecordHistory.type_id,
                sqlalchemy.sql.functions.sum(AppTables.ObserverRecordHistory.quantity).label("quantity"),
            )
            .where(
                sqlalchemy.and_(
                    AppTables.ObserverRecordHistory.observer_id == observer_id,
                    AppTables.ObserverRecordHistory.observer_history_id == observer_history_id,
                    AppTables.ObserverRecordHistory.last_updated >= previous_chunk_arrival_date,
                )
            )
            .group_by(AppTables.ObserverRecordHistory.observer_id, AppTables.ObserverRecordHistory.character_id, AppTables.ObserverRecordHistory.type_id)
        )

        character_results = collections.defaultdict(functools.partial(collections.defaultdict, int))
        character_totals = collections.defaultdict(int)
        async for observer_id, character_id, type_id, quantity in await session.stream(q):
            character_results[character_id][type_id] = quantity
            character_totals[character_id] += quantity

        return [(x, dict(character_results[x])) for x in sorted(character_totals, key=character_totals.get, reverse=True)]

    @staticmethod
    @otel
    async def get_usage(session: sqlalchemy.ext.asyncio.AsyncSession, permitted: bool, now: datetime.datetime) -> list[dict]:

        min_timestamp = datetime.datetime(2000, 1, 1, 0, 0, 0)
        if not permitted:
            min_timestamp = now - datetime.timedelta(days=14)

        query = (
            sqlalchemy.select(AppTables.Character.character_id, sqlalchemy.func.count(AppTables.AccessHistory.timestamp).label("count"), sqlalchemy.func.max(AppTables.AccessHistory.timestamp).label("last"))
            .join(AppTables.Character, AppTables.AccessHistory.character_id == AppTables.Character.character_id)
            .where(
                sqlalchemy.and_(
                    AppTables.AccessHistory.permitted.is_(permitted),
                    AppTables.AccessHistory.timestamp >= min_timestamp
                )
            )
            .group_by(AppTables.Character.character_id)
            .order_by(sqlalchemy.desc(sqlalchemy.func.max(AppTables.AccessHistory.timestamp)))
            .limit(20)
        )

        colnames: typing.Final = ["id", "count", "last"]
        return [dict(zip(colnames, x)) async for x in await session.stream(query)]

    @staticmethod
    @otel
    async def get_character_name(evedb: AppDatabase, character_id: int) -> str:
        async with await evedb.sessionmaker() as session:
            query = (
                sqlalchemy.select(AppTables.Character.name)
                .where(AppTables.Character.character_id == character_id)
                .limit(1)
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            return query_result.scalar_one_or_none()

    @staticmethod
    @otel
    async def get_corporation_name(evedb: AppDatabase, corporation_id: int) -> str:
        async with await evedb.sessionmaker() as session:
            query = (
                sqlalchemy.select(AppTables.Corporation.name)
                .where(AppTables.Corporation.corporation_id == corporation_id)
                .limit(1)
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            return query_result.scalar_one_or_none()

    @staticmethod
    @otel
    async def get_structure_name(evedb: AppDatabase, structure_id: int) -> str:
        async with await evedb.sessionmaker() as session:
            query = (
                sqlalchemy.select(AppTables.StructureHistory.name)
                .where(AppTables.StructureHistory.structure_id == structure_id)
                .order_by(sqlalchemy.desc(AppTables.StructureHistory.timestamp))
                .limit(1)
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            return query_result.scalar_one_or_none()

    @staticmethod
    @otel
    async def get_system_name(evedb: AppDatabase, system_id: int) -> str:
        async with await evedb.sessionmaker() as session:
            query = (
                sqlalchemy.select(AppTables.UniverseSystem.name)
                .where(AppTables.UniverseSystem.system_id == system_id)
                .limit(1)
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            return query_result.scalar_one_or_none()

    @staticmethod
    @otel
    async def get_moon_name(evedb: AppDatabase, moon_id: int) -> str:
        async with await evedb.sessionmaker() as session:
            query = (
                sqlalchemy.select(AppTables.UniverseMoon.name)
                .where(AppTables.UniverseMoon.moon_id == moon_id)
                .limit(1)
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            return query_result.scalar_one_or_none()

    @staticmethod
    @otel
    async def get_type_name(evedb: AppDatabase, type_id: int) -> str:
        async with await evedb.sessionmaker() as session:
            query = (
                sqlalchemy.select(AppTables.UniverseType.name)
                .where(AppTables.UniverseType.type_id == type_id)
                .limit(1)
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            return query_result.scalar_one_or_none()

    @staticmethod
    @otel
    async def get_configuration(evedb: AppDatabase, key: str) -> str:
        async with await evedb.sessionmaker() as session:
            query = (
                sqlalchemy.select(AppTables.Configuration.value)
                .where(AppTables.Configuration.key == key)
            )

            query_result: sqlalchemy.engine.Result = await session.execute(query)
            return query_result.scalar_one_or_none()
            # return [x async for x in await session.stream_scalars(query)]

    @staticmethod
    @otel
    async def is_permitted(evedb: AppDatabase, character_id: int, corpporation_id: int, alliance_id: int, check_trust: bool = False) -> bool:
        acl_pass = False

        async with await evedb.sessionmaker() as session:
            session: sqlalchemy.ext.asyncio.AsyncSession

            acl_query = sqlalchemy.select(AppTables.AccessControls)
            acl_set: typing.Final = {acl async for acl in await session.stream_scalars(acl_query)}

            acl_pass = False
            acl_evaluations = [
                (AppAccessType.ALLIANCE, alliance_id),
                (AppAccessType.CORPORATION, corpporation_id),
                (AppAccessType.CHARACTER, character_id),
            ]

            for acl_type, acl_id in acl_evaluations:
                if not acl_id > 0:
                    continue
                for acl in filter(lambda x: x.type == acl_type, acl_set):
                    if not isinstance(acl, AppTables.AccessControls):
                        continue
                    if acl_id == acl.id:
                        acl_pass = acl.permit
                        if check_trust:
                            acl_pass = acl_pass and acl.trust

        return acl_pass

    @staticmethod
    @otel
    async def is_contributor(evedb: AppDatabase, character_id: int, corpporation_id: int, alliance_id: int) -> bool:
        if character_id in AppConstants.MAGIC_ADMINS | AppConstants.MAGIC_CONTRIBUTORS:
            return True

        is_permitted = await AppFunctions.is_permitted(evedb, character_id, corpporation_id, alliance_id, check_trust=False)
        if is_permitted:
            async with await evedb.sessionmaker() as session:

                query = (
                    sqlalchemy.select(AppTables.PeriodicCredentials)
                    .where(
                        sqlalchemy.and_(
                            AppTables.PeriodicCredentials.is_enabled.is_(True),
                            AppTables.PeriodicCredentials.character_id == character_id,
                        )
                    )
                )

                query_result: sqlalchemy.engine.Result = await session.execute(query)
                obj = query_result.scalar_one_or_none()
                if obj is not None:
                    return True
        return False

    @staticmethod
    @otel
    async def is_trusted(evedb: AppDatabase, character_id: int, corpporation_id: int, alliance_id: int) -> bool:
        is_permitted = await AppFunctions.is_permitted(evedb, character_id, corpporation_id, alliance_id, check_trust=False)
        is_trusted = await AppFunctions.is_permitted(evedb, character_id, corpporation_id, alliance_id, check_trust=True)

        if is_permitted and not is_trusted:
            async with await evedb.sessionmaker() as session:

                query = (
                    sqlalchemy.select(AppTables.PeriodicCredentials)
                    .where(
                        sqlalchemy.and_(
                            AppTables.PeriodicCredentials.is_enabled.is_(True),
                            AppTables.PeriodicCredentials.character_id == character_id,
                        )
                    )
                )

                query_result: sqlalchemy.engine.Result = await session.execute(query)
                obj = query_result.scalar_one_or_none()
                if obj is not None:
                    is_trusted = True

        return is_trusted
