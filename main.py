import asyncio
import inspect
import logging
import os
import typing
import uuid

import opentelemetry.instrumentation.asgi
import quart
import quart.sessions
import quart_session

from app import (AppConstants, AppDatabase, AppESI, AppFunctions, AppRequest, AppSSO, AppTables,
                 AppTask, AppTemplates, AppMoonMiningHistory)
from support.telemetry import otel, otel_initialize
from tasks import (AppAccessControlTask, AppMoonYieldTask,
                   AppStructureNotificationTask, AppStructurePollingTask,
                   ESIAllianceBackfillTask,
                   ESINPCorporationBackfillTask,
                   ESIUniverseConstellationsBackfillTask,
                   ESIUniverseRegionsBackfillTask,
                   ESIUniverseSystemsBackfillTask, 
                   AppMarketHistoryTask)

BASEDIR: typing.Final = os.path.dirname(os.path.realpath(__file__))

app: typing.Final = quart.Quart(__name__)

app.config.from_mapping({
    "DEBUG": False,
    "PORT": 5050,
    "SECRET_KEY": uuid.uuid4().hex,

    "SESSION_TYPE": "redis",
    "SESSION_REVERSE_PROXY": True,
    "SESSION_PERMANENT": True,
    # "SESSION_PROTECTION": True,
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_SECURE": True,

    "TEMPLATES_AUTO_RELOAD": True,
    "SEND_FILE_MAX_AGE_DEFAULT": 300,
    "MAX_CONTENT_LENGTH": 512 * 1024,
    "BODY_TIMEOUT": 15,
    "RESPONSE_TIMEOUT": 15,
})

evesso_config: typing.Final = {
    "client_id": os.getenv("EVEONLINE_CLIENT_ID", ""),
    "scopes": [
        "publicData",
        "esi-corporations.read_structures.v1",
        "esi-characters.read_corporation_roles.v1",
        "esi-industry.read_corporation_mining.v1",
    ]
}

app.logger.setLevel(logging.INFO)

quart_session.Session(app)

esi: typing.Final = AppESI.factory(app.logger)
db: typing.Final = AppDatabase(
    os.getenv("SQLALCHEMY_DB_URL", ""),
)
eveevents: typing.Final = asyncio.Queue()
sso: typing.Final = AppSSO(app, esi, db, eveevents, **evesso_config)
evesession: typing.Final = app.session_interface.session_class(sid="global", permanent=False)
evesession[AppTask.CONFIGDIR] = os.path.abspath(os.path.join(BASEDIR, "data"))


@app.before_serving
@otel
async def _before_serving() -> None:
    if not bool(evesession.get("setup_tasks_started", False)):
        evesession["setup_tasks_started"] = True

        AppStructureNotificationTask(evesession, esi, db, eveevents, app.logger)

        ESIUniverseRegionsBackfillTask(evesession, esi, db, eveevents, app.logger)
        ESIUniverseConstellationsBackfillTask(evesession, esi, db, eveevents, app.logger)
        ESIUniverseSystemsBackfillTask(evesession, esi, db, eveevents, app.logger)
        ESIAllianceBackfillTask(evesession, esi, db, eveevents, app.logger)
        ESINPCorporationBackfillTask(evesession, esi, db, eveevents, app.logger)

        AppMarketHistoryTask(evesession, esi, db, eveevents, app.logger)
        AppAccessControlTask(evesession, esi, db, eveevents, app.logger)
        AppMoonYieldTask(evesession, esi, db, eveevents, app.logger)

        AppStructurePollingTask(evesession, esi, db, eveevents, app.logger)




@app.route('/robots.txt', methods=["GET"])
@otel
async def _robots() -> quart.ResponseReturnValue:
    return await app.send_static_file('robots.txt')


@app.route("/usage/", methods=["GET"])
@otel
async def _usage() -> quart.ResponseReturnValue:

    ar: typing.Final[AppRequest] = await AppFunctions.get_app_request(db, quart.session, quart.request)
    if ar.character_id > 0 and ar.suspect:
        quart.session.clear()

    elif ar.character_id > 0 and ar.permitted and ar.contributor:

        permitted_data: typing.Final = list()
        denied_data: typing.Final = list()

        try:
            async with await db.sessionmaker() as session:
                permitted_data.extend(await AppFunctions.get_usage(session, True, ar.ts))
                denied_data.extend(await AppFunctions.get_usage(session, False, ar.ts))

        except Exception as ex:
            app.logger.error(f"{inspect.currentframe().f_code.co_name}: {ex=}")

        return await quart.render_template(
            "usage.html",
            character_id=ar.character_id,
            is_contributor_character=ar.contributor,
            is_magic_character=ar.magic_character,
            permitted_usage=permitted_data,
            denied_usage=denied_data
        )

    return quart.redirect("/about/")


@app.route("/about/", methods=["GET"])
@otel
async def _about() -> quart.ResponseReturnValue:

    ar: typing.Final = await AppFunctions.get_app_request(db, quart.session, quart.request)

    return await quart.render_template(
        "about.html",
        character_id=ar.character_id,
        is_contributor_character=ar.contributor,
        is_magic_character=ar.magic_character,
        is_about_page=True
    )


@app.route('/top', defaults={'top_type': 'characters'}, methods=["GET"])
@app.route('/top/<string:top_type>', methods=["GET"])
@otel
async def _top(top_type: str) -> quart.ResponseReturnValue:

    ar: typing.Final[AppRequest] = await AppFunctions.get_app_request(db, quart.session, quart.request)
    if ar.character_id > 0 and ar.suspect:
        quart.session.clear()

    elif ar.character_id > 0 and ar.permitted:
    
        if any([ar.contributor, ar.magic_character]):
 
            history = None

            try:
                async with await db.sessionmaker() as session:
                    history = await AppFunctions.get_moon_mining_top(session, ar.ts)

            except Exception as ex:
                app.logger.error(f"{inspect.currentframe().f_code.co_name}: {ex=}")

            top_observers: typing.Final = list()
            top_observers_isk_dict: typing.Final = dict()
            for (observer_id, isk) in history.observers:
                top_observers.append(observer_id)
                top_observers_isk_dict[observer_id] = isk

            top_characters: typing.Final = list()
            top_characters_isk_dict: typing.Final = dict()
            for (character_id, isk) in history.characters:
                top_characters.append(character_id)
                top_characters_isk_dict[character_id] = isk

            return await quart.render_template(
                "mining_rankings.html",
                character_id=ar.character_id,
                is_contributor_character=ar.contributor,
                is_magic_character=ar.magic_character,
                top_period_start=history.start_date,
                top_period_end=history.end_date,
                top_characters=top_characters,
                top_characters_isk=top_characters_isk_dict,
                top_observers=top_observers,
                top_observers_isk=top_observers_isk_dict,
                observer_timestamps=history.observer_timestamps,
                observer_names=history.observer_names)

        return quart.redirect("/")

    return quart.redirect(sso.login_route)


@app.route('/moon', defaults={'moon_id': 0}, methods=["GET"])
@app.route('/moon/<int:moon_id>', methods=["GET"])
@otel
async def _moon(moon_id: int) -> quart.ResponseReturnValue:

    ar: typing.Final[AppRequest] = await AppFunctions.get_app_request(db, quart.session, quart.request)
    if ar.character_id > 0 and ar.suspect:
        quart.session.clear()

    elif ar.character_id > 0 and ar.permitted:

        moon_extraction_history: typing.Final = list()
        moon_mining_history: typing.Final = list()
        moon_mining_history_timestamp = None
        moon_yield: typing.Final = list()
        structure = None

        try:
            async with await db.sessionmaker() as session:
                structure = await AppFunctions.get_moon_structure(session, moon_id, ar.ts)
                moon_yield.extend(await AppFunctions.get_moon_yield(session, moon_id, ar.ts))
                moon_extraction_history.extend(await AppFunctions.get_moon_extraction_history(session, moon_id, ar.ts))
                moon_mining_history_timestamp, mm_history, mm_isk = await AppFunctions.get_moon_mining_history(session, moon_id, ar.ts)
                moon_mining_history.extend(mm_history)

        except Exception as ex:
            app.logger.error(f"{inspect.currentframe().f_code.co_name}: {ex=}")

        mined_types_set = set()
        miner_list = list()
        mined_quantity_dict = dict()
        if any([ar.contributor, ar.magic_character]):
            for character_id, mining_history_dict in moon_mining_history:
                for type_id in mining_history_dict.keys():
                    mined_types_set.add(type_id)
                miner_list.append(character_id)
                mined_quantity_dict[character_id] = mining_history_dict

        time_chunking = 3
        return await quart.render_template(
            "moon.html",
            character_id=ar.character_id,
            is_contributor_character=ar.contributor,
            is_magic_character=ar.magic_character,
            moon_id=moon_id,
            structure=structure,
            moon_yield=moon_yield,
            moon_extraction_history=moon_extraction_history,
            miners=miner_list,
            mined_quantity=mined_quantity_dict,
            mined_isk=mm_isk,
            mined_quantity_timestamp=moon_mining_history_timestamp,
            mined_types=sorted(list(mined_types_set)),
            weekday_names=['M', 'T', 'W', 'T', 'F', 'S', 'S'],
            timeofday_names=[f"{(x-time_chunking):02d}-{(x):02d}" for x in range(time_chunking, 24 + time_chunking) if x % time_chunking == 0],
        )

    return quart.redirect(sso.login_route)


@app.route("/", methods=["GET"])
@otel
async def _root() -> quart.ResponseReturnValue:

    ar: typing.Final[AppRequest] = await AppFunctions.get_app_request(db, quart.session, quart.request)
    if ar.character_id > 0 and ar.suspect:
        quart.session.clear()

    elif ar.character_id > 0 and ar.permitted:

        active_timer_results: typing.Final[list[AppTables.Structure]] = list()
        completed_extraction_results: typing.Final = list()
        scheduled_extraction_results: typing.Final = list()
        unscheduled_extraction_results: typing.Final = list()
        structure_fuel_results: typing.Final[list[AppTables.Structure]] = list()
        structures_without_fuel_results: typing.Final[list[AppTables.Structure]] = list()
        last_update_results: typing.Final = list()
        structure_counts: typing.Final = list()

        try:
            async with await db.sessionmaker() as session:
                active_timer_results.extend(await AppFunctions.get_active_timers(session, ar.ts))
                completed_extraction_results.extend(await AppFunctions.get_completed_extractions(session, ar.ts))
                scheduled_extraction_results.extend(await AppFunctions.get_scheduled_extractions(session, ar.ts))
                unscheduled_extraction_results.extend(await AppFunctions.get_unscheduled_structures(session, ar.ts))
                structure_fuel_results.extend(await AppFunctions.get_structure_fuel_expiries(session, ar.ts))
                structures_without_fuel_results.extend(await AppFunctions.get_structures_without_fuel(session, ar.ts))
                last_update_results.extend(await AppFunctions.get_refresh_times(session, ar.ts))
                structure_count_dict: typing.Final = await AppFunctions.get_structure_counts(session, ar.ts)
                for last_update in last_update_results:
                    last_update: AppTables.PeriodicTaskTimestamp
                    structure_counts.append(structure_count_dict.get(last_update.corporation_id, 0))

        except Exception as ex:
            app.logger.error(f"{inspect.currentframe().f_code.co_name}: {ex=}")

        return await quart.render_template(
            "home.html",
            character_id=ar.character_id,
            is_contributor_character=ar.contributor,
            is_magic_character=ar.magic_character,
            active_timers=active_timer_results,
            completed_extractions=completed_extraction_results,
            scheduled_extractions=scheduled_extraction_results,
            structure_fuel_expiries=structure_fuel_results,
            structures_without_fuel=structures_without_fuel_results,
            unscheduled_extractions=unscheduled_extraction_results,
            last_update=last_update_results,
            structure_counts=structure_counts,
            character_trusted=ar.trusted,
            is_homne_page=True
        )

    elif ar.character_id > 0 and not ar.permitted:
        app.logger.warning(f"{ar.character_id} not permitted")
        return await quart.render_template(
            "permission.html",
            character_id=ar.character_id,
            is_contributor_character=ar.contributor,
            is_magic_character=ar.magic_character,
            is_homne_page=True
        )

    return await quart.render_template("login.html")


if __name__ == "__main__":

    otel_initialize()

    AppTemplates.add_templates(app, db)

    app_debug: typing.Final = app.config.get("DEBUG", False)
    app_port: typing.Final = app.config.get("PORT", 5050)
    app_host: typing.Final = app.config.get("HOST", "127.0.0.1")

    # app_log_file: typing.Final = os.path.join(BASEDIR, "logs", "app.log")
    # app_log_dir: typing.Final = os.path.dirname(app_log_file)
    # if not os.path.isdir(app_log_dir):
    #     os.makedirs(app_log_dir, 0o755)

    # logging.basicConfig(level=logging.INFO, filename=app_log_file)

    if app_debug:
        app.run(host=app_host, port=app_port, debug=app_debug)
    else:
        import hypercorn.asyncio
        import hypercorn.config
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app_trusted_hosts: typing.Final = ["127.0.0.1", "::1"]
        app_bind_hosts: typing.Final = [x for x in app_trusted_hosts]

        script_name: typing.Final = os.path.splitext(os.path.basename(__file__))[0]
        with open(os.path.join(BASEDIR, f"{script_name}.pid"), 'w') as ofp:
            ofp.write(f"{os.getpid()}{os.linesep}")

        # XXX: hack for development server.
        development_flag_file = os.path.join(BASEDIR, "development.txt")
        if os.path.exists(development_flag_file):
            with open(development_flag_file) as ifp:
                app_bind_hosts.clear()
                app_bind_hosts.append("0.0.0.0")
                for line in [line.strip() for line in ifp.readlines()]:
                    app_trusted_hosts.append(line)

        config: typing.Final = hypercorn.config.Config()
        config.bind = [f"{host}:{app_port}" for host in app_bind_hosts]
        config.accesslog = "-"

        async def async_main():
            await db._initialize()

            app.asgi_app = opentelemetry.instrumentation.asgi.OpenTelemetryMiddleware(
                app.asgi_app
            )

            app.asgi_app = ProxyHeadersMiddleware(
                app.asgi_app, trusted_hosts=app_trusted_hosts
            )

            await hypercorn.asyncio.serve(app, config)

        asyncio.run(async_main())
